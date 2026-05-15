"""
L3 Attention — 注意力队列
============================

三维评分 + 抢占机制 + 走神检测。

设计：
  - 三维评分：urgency(紧急) / importance(重要) / interest(兴趣)
  - score = 0.5*urgency + 0.3*importance + 0.2*interest
  - 抢占模式：NORMAL / FOCUSED(聚焦) / DEFUSING(扩散)
  - 走神检测：FocusDwell 监测 focus_duration < threshold 的频率
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.L3.attention")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class Priority(Enum):
    CRITICAL = 0
    HIGH     = 1
    MEDIUM   = 2
    LOW      = 3
    BACKGROUND = 4


class PreemptiveMode(Enum):
    NORMAL   = "normal"     # 不抢占
    FOCUSED  = "focused"   # 新高优先级直接抢占
    DEFUSING = "defusing"   # 新高优先级进入队列但不抢占


@dataclass
class AttentionScore:
    urgency: float      # [0, 1] 这个任务有多紧急
    importance: float   # [0, 1] 对 anan/用户有多重要
    interest: float     # [0, 1] anan 对这个任务有多大兴趣
    # 权重固定，不开放配置（保持简单）
    _w_urgency: float = field(default=0.5, repr=False)
    _w_importance: float = field(default=0.3, repr=False)
    _w_interest: float = field(default=0.2, repr=False)

    def total(self) -> float:
        return (
            self._w_urgency    * self.urgency
            + self._w_importance * self.importance
            + self._w_interest  * self.interest
        )

    def to_dict(self) -> dict:
        return {
            "urgency": self.urgency,
            "importance": self.importance,
            "interest": self.interest,
            "total": round(self.total(), 3),
        }


@dataclass
class AttentionItem:
    id: str
    label: str
    source: str
    score: AttentionScore
    priority: Priority
    created_at: float = field(default_factory=time.time)
    ttl_s: float = field(default_factory=lambda: 300.0)   # 存活时间
    suppress_count: int = 0   # 被抢占次数
    max_suppress: int = 3     # 超过这个次数降为 background

    boost: float = 0.0   # 外部加成（驱动力等），由 boost() 累加

    def total_score(self) -> float:
        """最终得分 = 原始分 - 抢占惩罚 + 外部加成"""
        suppress_penalty = self.suppress_count * 0.05
        return max(0.0, self.score.total() - suppress_penalty + self.boost)

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_s


# ---------------------------------------------------------------------------
# AttentionQueue
# ---------------------------------------------------------------------------

class AttentionQueue:
    """L3 — 注意力调度队列

    管理一个优先级队列，决定"现在最值得思考什么"。

    Usage:
        q = AttentionQueue(bus=bus, focus_threshold=0.5)
        item = q.enqueue("task-1", "修bug", "L4",
                         score=AttentionScore(0.8, 0.7, 0.5))
        focused = q.focus()
        q.complete("task-1")
        q.suppress("task-1")  # 降低优先级
    """

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        focus_threshold: float = 0.4,
        max_queue: int = 50,
        default_ttl: float = 300.0,
    ):
        self._bus = bus or get_bus()
        self._focus_threshold = focus_threshold
        self._max_queue = max_queue
        self._default_ttl = default_ttl

        self._items: deque[AttentionItem] = deque(maxlen=max_queue)
        self._focused: Optional[AttentionItem] = None
        self._focused_since: Optional[float] = None
        self._mode = PreemptiveMode.NORMAL
        self._sorted_cache: Optional[list[AttentionItem]] = None

        self._unsubs: list = []

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    async def attach(self) -> None:
        self._unsubs.append(
            self._bus.subscribe("L1.dream.start", self._on_dream_start)
        )
        self._unsubs.append(
            self._bus.subscribe("L1.sleep.start", self._on_sleep_start)
        )
        self._unsubs.append(
            self._bus.subscribe("L5.prediction.upcoming", self._on_prediction)
        )
        self._unsubs.append(
            self._bus.subscribe("L9.self.question", self._on_self_question)
        )
        logger.info("AttentionQueue attached (threshold=%.2f)", self._focus_threshold)

    async def detach(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()

    # ------------------------------------------------------------------
    # Queue API
    # ------------------------------------------------------------------

    def enqueue(
        self,
        item_id: str,
        label: str,
        source: str,
        *,
        score: AttentionScore,
        priority: Priority = Priority.MEDIUM,
        ttl_s: Optional[float] = None,
    ) -> AttentionItem:
        """入队，返回 item。重复 id 会替换。"""
        # 清除已存在的同名
        self._items = deque(
            [i for i in self._items if i.id != item_id],
            maxlen=self._max_queue,
        )
        self._sorted_cache = None

        item = AttentionItem(
            id=item_id,
            label=label,
            source=source,
            score=score,
            priority=priority,
            ttl_s=ttl_s or self._default_ttl,
        )
        self._items.append(item)

        self._bus.publish_sync(Event(
            topic="L3.attention.queued",
            source="L3.attention",
            payload={
                "id": item_id,
                "label": label,
                "source": source,
                "score": score.to_dict(),
                "priority": priority.name,
                "queue_depth": len(self._items),
            },
        ))

        # 检查是否需要抢占
        if self._should_preempt(item):
            asyncio.create_task(self._preempt_to(item))

        return item

    def complete(self, item_id: str) -> bool:
        """完成任务，移除队列。"""
        before = len(self._items)
        self._items = deque(
            [i for i in self._items if i.id != item_id],
            maxlen=self._max_queue,
        )
        removed = len(self._items) < before

        if self._focused and self._focused.id == item_id:
            self._focused = None
            self._focused_since = None
            removed = True
        if removed:
            self._sorted_cache = None
            self._bus.publish_sync(Event(
                topic="L3.attention.completed",
                source="L3.attention",
                payload={"id": item_id},
            ))
        return removed

    def suppress(self, item_id: str) -> bool:
        """记录一次抢占，被抢次数+1，超过 max_suppress 降为 background。"""
        self._sorted_cache = None
        for item in self._items:
            if item.id == item_id:
                item.suppress_count += 1
                if item.suppress_count >= item.max_suppress:
                    item.priority = Priority.BACKGROUND
                self._bus.publish_sync(Event(
                    topic="L3.attention.suppressed",
                    source="L3.attention",
                    payload={"id": item_id, "count": item.suppress_count},
                ))
                return True
        return False

    def boost(self, item_id: str, extra_score: float = 0.1) -> bool:
        """提高注意力项分数（由驱动力等外部信号触发）。返回是否找到。"""
        self._sorted_cache = None
        for item in self._items:
            if item.id == item_id:
                item.boost += extra_score
                # 同时升级优先级
                if item.priority == Priority.BACKGROUND:
                    item.priority = Priority.MEDIUM
                elif item.priority == Priority.MEDIUM:
                    item.priority = Priority.HIGH
                self._bus.publish_sync(Event(
                    topic="L3.attention.boosted",
                    source="L3.attention",
                    payload={"id": item_id, "extra_score": extra_score, "total_boost": item.boost},
                ))
                return True
        return False

    def focus(self) -> Optional[AttentionItem]:
        """返回当前最值得思考的注意力项（不超过 threshold）。"""
        # 清除超时的 item
        self._items = deque(
            [i for i in self._items if not i.is_expired()],
            maxlen=self._max_queue,
        )
        if self._focused and self._focused.is_expired():
            old = self._focused
            self._focused = None
            self._focused_since = None
            self._bus.publish_sync(Event(
                topic="L3.attention.dropped",
                source="L3.attention",
                payload={"id": old.id, "label": old.label, "reason": "expired"},
            ))

        self._sorted_cache = None
        best: Optional[AttentionItem] = None
        best_score = -1.0

        for item in self._items:
            if item.total_score() > best_score:
                best_score = item.total_score()
                best = item

        if best and best_score >= self._focus_threshold:
            if self._focused is None or self._focused.id != best.id:
                old = self._focused
                self._focused = best
                self._focused_since = time.time()
                self._bus.publish_sync(Event(
                    topic="L3.attention.focus",
                    source="L3.attention",
                    payload={
                        "from_id": old.id if old else None,
                        "to_id": best.id,
                        "label": best.label,
                        "score": best.total_score(),
                    },
                ))
            return best
        else:
            return None

    def set_mode(self, mode: PreemptiveMode) -> None:
        """切换抢占模式。"""
        old = self._mode
        self._mode = mode
        logger.info("Attention mode: %s → %s", old.value, mode.value)
        self._bus.publish_sync(Event(
            topic="L3.attention.mode_changed",
            source="L3.attention",
            payload={"from": old.value, "to": mode.value},
        ))

    def current_focus(self) -> Optional[AttentionItem]:
        return self._focused

    def queue_snapshot(self) -> list[dict]:
        """返回队列快照（按得分排序，供调试/UI）。"""
        items = sorted(self._items, key=lambda i: i.total_score(), reverse=True)
        self._sorted_cache = items
        return [
            {
                "id": i.id,
                "label": i.label,
                "source": i.source,
                "priority": i.priority.name,
                "total_score": round(i.total_score(), 3),
                "suppress_count": i.suppress_count,
                "age_s": round(time.time() - i.created_at, 1),
            }
            for i in items
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _should_preempt(self, item: AttentionItem) -> bool:
        if self._mode == PreemptiveMode.NORMAL:
            return False
        if self._mode == PreemptiveMode.FOCUSED:
            return item.priority.value <= Priority.HIGH.value
        if self._mode == PreemptiveMode.DEFUSING:
            return item.priority.value <= Priority.CRITICAL.value
        return False

    async def _preempt_to(self, item: AttentionItem) -> None:
        """执行抢占：旧 focus 发 L3.attention.shift，新 focus 发 L3.attention.focus"""
        old = self._focused
        self._focused = item
        self._focused_since = time.time()
        self._sorted_cache = None

        if old:
            self._bus.publish_sync(Event(
                topic="L3.attention.shift",
                source="L3.attention",
                payload={
                    "from_id": old.id,
                    "from_label": old.label,
                    "to_id": item.id,
                    "to_label": item.label,
                    "reason": "preempt",
                },
            ))

        self._bus.publish_sync(Event(
            topic="L3.attention.focus",
            source="L3.attention",
            payload={
                "from_id": old.id if old else None,
                "to_id": item.id,
                "label": item.label,
                "score": item.total_score(),
                "reason": "preempt",
            },
        ))
        logger.debug("Preempted to %s (score=%.2f)", item.label, item.total_score())

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_dream_start(self, event: Event) -> None:
        self.set_mode(PreemptiveMode.DEFUSING)

    async def _on_sleep_start(self, event: Event) -> None:
        self._items.clear()
        self._focused = None
        self._focused_since = None

    async def _on_prediction(self, event: Event) -> None:
        """L5 预测到来 → 提高对应注意力项的 urgency"""
        p = event.payload or {}
        cause = p.get("cause", "")
        prob = p.get("probability_boost", 1.0)
        if prob < 1.5:
            return
        # 提高所有 source==cause 的项的 urgency
        for item in self._items:
            if item.source == cause:
                item.score.urgency = min(1.0, item.score.urgency + 0.2)
        self._sorted_cache = None

    async def _on_self_question(self, event: Event) -> None:
        """L9 自我追问 → 强制聚焦模式直到回答"""
        self.set_mode(PreemptiveMode.FOCUSED)


# ---------------------------------------------------------------------------
# VigilanceMonitor — 走神检测
# ---------------------------------------------------------------------------

class VigilanceMonitor:
    """L3 — 走神检测器

    监测 focus_duration 是否持续低于 threshold。
    连续多次低于阈值 → 发布 L3.vigilance.low 事件，触发注意力重定向。

    Usage:
        vm = VigilanceMonitor(bus=bus, window_s=60, threshold=3.0, consecutive_trigger=3)
        vm.record_focus_start()
        time.sleep(2)
        vm.record_focus_end()   # 2s focus duration
        result = vm.check()     # 可能返回走神警告
    """

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        window_s: float = 60.0,
        threshold: float = 3.0,
        consecutive_trigger: int = 3,
    ):
        self._bus = bus or get_bus()
        self._window_s = window_s
        self._threshold = threshold
        self._consecutive_trigger = consecutive_trigger

        self._focus_durations: deque[float] = deque(maxlen=100)
        self._focus_start: Optional[float] = None
        self._consecutive_low: int = 0

        self._unsubs: list = []

    async def attach(self) -> None:
        self._unsubs.append(
            self._bus.subscribe("L1.sleep.start", self._on_sleep)
        )
        # 每次心跳都自动检查是否走神（不再被动等待外部调用）
        self._unsubs.append(
            self._bus.subscribe("L0.circadian.tick", self._on_tick)
        )
        # 监听注意力焦点切换，自动记录 focus 时长
        self._unsubs.append(
            self._bus.subscribe("L3.attention.focus", self._on_focus_changed)
        )
        self._unsubs.append(
            self._bus.subscribe("L3.attention.completed", self._on_focus_ended)
        )
        self._unsubs.append(
            self._bus.subscribe("L3.attention.dropped", self._on_focus_ended)
        )
        logger.info("VigilanceMonitor attached (threshold=%.1fs)", self._threshold)

    async def _on_tick(self, event: Event) -> None:
        """每次心跳自动检查走神状态。"""
        self.check()

    async def _on_focus_changed(self, event: Event) -> None:
        """注意力焦点切换：结束上一个焦点的计时，开始新的。"""
        # 结束上一个焦点
        if self._focus_start is not None:
            self.record_focus_end()
        # 开始新的焦点计时
        self.record_focus_start()

    async def _on_focus_ended(self, event: Event) -> None:
        """焦点任务完成/被丢弃：结束计时。"""
        if self._focus_start is not None:
            self.record_focus_end()

    async def detach(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()

    def record_focus_start(self) -> None:
        self._focus_start = time.time()

    def record_focus_end(self) -> None:
        if self._focus_start is None:
            return
        duration = time.time() - self._focus_start
        self._focus_durations.append(
            (time.time(), duration)   # (recorded_at, duration)
        )
        self._focus_start = None

    def check(self) -> Optional[dict]:
        """检查是否走神。返回走神报告或 None。"""
        now = time.time()
        recent = [
            (rec_at, d) for rec_at, d in self._focus_durations
            if now - rec_at < self._window_s
        ]

        if len(recent) < 3:
            return None

        durations = [d for _, d in recent]
        avg = sum(durations) / len(durations)
        below = sum(1 for d in durations if d < self._threshold)
        ratio = below / len(durations)

        if ratio > 0.6:
            self._consecutive_low += 1
        else:
            self._consecutive_low = 0

        if self._consecutive_low >= self._consecutive_trigger:
            self._consecutive_low = 0
            self._bus.publish_sync(Event(
                topic="L3.vigilance.low",
                source="L3.attention",
                payload={
                    "avg_focus_s": round(avg, 2),
                    "recent_count": len(recent),
                    "below_threshold": below,
                    "ratio": round(ratio, 2),
                    "suggestion": "建议切换到扩散模式或短暂休息",
                },
            ))
            return {
                "avg_focus_s": round(avg, 2),
                "ratio": round(ratio, 2),
                "suggestion": "建议切换到扩散模式或短暂休息",
            }
        return None

    async def _on_sleep(self, event: Event) -> None:
        self._focus_durations.clear()
        self._consecutive_low = 0
        self._focus_start = None
