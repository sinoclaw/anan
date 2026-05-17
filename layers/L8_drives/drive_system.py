"""
L8 Drives — 驱动力系统
=======================
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from kernel.event_bus import Event, EventBus, get_bus
from layers.L8_drives.priority_advisor import DrivePriorityAdvisor, DrivePriority

logger = logging.getLogger("anan.L8.drives")


# ---------------------------------------------------------------------------
# Drive Types
# ---------------------------------------------------------------------------

class DriveType(Enum):
    CURIOSITY   = "curiosity"     # 遇到新概念 → 主动学习
    COMPLETION  = "completion"     # 任务未完成 → 优先级提升
    CARE        = "care"          # 用户相关事 → 优先级提升
    AESTHETICS  = "aesthetics"    # 代码/方案丑 → 触发优化
    BOREDOM     = "boredom"      # 重复劳动 → 触发寻找新方法


@dataclass
class Drive:
    """一个驱动力的当前状态"""
    type: DriveType
    strength: float = 0.5      # 激活强度 [0, 1]
    last_triggered: float = field(default_factory=time.time)
    last_satisfied: Optional[float] = None
    active: bool = False
    event_count: int = 0

    def is_stale(self, age_s: float = 3600.0) -> bool:
        """长时间没被触发，驱动可能已经冷却"""
        return time.time() - self.last_triggered > age_s

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "strength": round(self.strength, 3),
            "active": self.active,
            "event_count": self.event_count,
            "last_triggered": self.last_triggered,
            "last_satisfied": self.last_satisfied,
        }


# ---------------------------------------------------------------------------
# DriveSystem
# ---------------------------------------------------------------------------

class DriveSystem:
    """L8 — 驱动力引擎

    五种内驱力影响注意力优先级和目标生成。

    Usage:
        drives = DriveSystem(bus=bus)
        await drives.attach()
        # 手动触发
        drives.trigger(DriveType.CURIOSITY, "遇到了新概念 X")
        # 查询活跃驱动
        active = drives.active_drives()
        # 满足驱动
        drives.satisfy(DriveType.COMPLETION)
    """

    DECAY_RATE = 0.02        # 每次 tick 衰减强度
    BOOST_RATE = 0.15        # 触发时提升强度
    SATISFY_PENALTY = 0.3   # 被满足后下降幅度
    MAX_STRENGTH = 1.0
    MIN_STRENGTH = 0.0

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
    ):
        self._bus = bus or get_bus()
        self._drives: dict[DriveType, Drive] = {
            dt: Drive(type=dt) for dt in DriveType
        }
        self._recent_satisfactions: deque[float] = deque(maxlen=50)
        self._priority_advisor = DrivePriorityAdvisor()
        self._unsubs: list = []

    def set_delegate(self, fn: callable) -> None:
        """Inject delegate_task for DrivePriorityAdvisor subagent calls."""
        self._priority_advisor.set_delegate(fn)

    async def score_goal(
        self,
        goal_tags: list[str],
        goal_description: str = "",
    ) -> DrivePriority:
        """Score a goal's priority using the drive priority advisor.

        Falls back to rule-based scoring if no delegate is configured.
        """
        active = [d.to_dict() for d in self.active_drives()]
        top = [d.to_dict() for d in self.top_drives()]
        return await self._priority_advisor.score_goal(
            goal_tags=goal_tags,
            goal_description=goal_description,
            active_drives=active,
            top_drives=top,
        )

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    async def attach(self) -> None:
        self._unsubs.append(
            self._bus.subscribe("L7.goal.achieved", self._on_goal_achieved)
        )
        self._unsubs.append(
            self._bus.subscribe("L7.goal.abandoned", self._on_goal_abandoned)
        )
        self._unsubs.append(
            self._bus.subscribe("L3.attention.shift", self._on_attention_shift)
        )
        self._unsubs.append(
            self._bus.subscribe("L8.intent.snapshot", self._on_intent_snapshot)
        )
        self._unsubs.append(
            self._bus.subscribe("L5.prediction.upcoming", self._on_prediction)
        )
        # L0 tick 周期性激活 CURIOSITY 驱动力（让 anan 持续对新事物好奇）
        self._unsubs.append(
            self._bus.subscribe("L0.circadian.tick", self._on_circadian_tick)
        )
        # L6 元认知报告 → 根据健康分动态调整驱动力
        self._unsubs.append(
            self._bus.subscribe("L6.metacognition.report", self._on_metacognition_report)
        )
        logger.info("DriveSystem attached")

    async def detach(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def trigger(self, drive_type: DriveType, reason: str = "") -> Drive:
        """触发某个驱动力，strength 提升。"""
        drive = self._drives[drive_type]
        drive.strength = min(
            self.MAX_STRENGTH,
            drive.strength + self.BOOST_RATE,
        )
        drive.last_triggered = time.time()
        drive.active = True
        drive.event_count += 1

        self._bus.publish_sync(Event(
            topic="L8.drive.active",
            source="L8.drives",
            payload={
                **drive.to_dict(),
                "reason": reason,
            },
        ))
        # L8.drive.updated — 供 AttentionBridge (attention_bridge.py) 消费
        self._bus.publish_sync(Event(
            topic="L8.drive.updated",
            source="L8.drives",
            payload={
                **drive.to_dict(),
                "active": True,
                "reason": reason,
            },
        ))
        logger.debug("Drive triggered: %s (strength=%.2f, reason=%s)",
                     drive_type.value, drive.strength, reason)
        return drive

    def satisfy(self, drive_type: DriveType) -> None:
        """满足驱动力，strength 下降。"""
        drive = self._drives[drive_type]
        drive.strength = max(
            self.MIN_STRENGTH,
            drive.strength - self.SATISFY_PENALTY,
        )
        drive.last_satisfied = time.time()
        if drive.strength < 0.1:
            drive.active = False

        self._recent_satisfactions.append(time.time())
        self._bus.publish_sync(Event(
            topic="L8.drive.satisfied",
            source="L8.drives",
            payload=drive.to_dict(),
        ))
        logger.debug("Drive satisfied: %s (strength=%.2f)", drive_type.value, drive.strength)

    def decay_all(self) -> None:
        """对所有活跃驱动执行一次衰减。"""
        for drive in self._drives.values():
            if drive.active:
                drive.strength = max(
                    self.MIN_STRENGTH,
                    drive.strength - self.DECAY_RATE,
                )
                if drive.strength < 0.1:
                    drive.active = False
                self._bus.publish_sync(Event(
                    topic="L8.drive.dormant",
                    source="L8.drives",
                    payload=drive.to_dict(),
                ))

    def active_drives(self) -> list[Drive]:
        """返回当前激活的驱动（strength > 0.4）"""
        return [
            d for d in self._drives.values()
            if d.active and d.strength > 0.4
        ]

    def top_drives(self, n: int = 3) -> list[Drive]:
        """返回 top-n 最强驱动"""
        drives = list(self._drives.values())
        drives.sort(key=lambda d: d.strength, reverse=True)
        return drives[:n]

    def get(self, drive_type: DriveType) -> Drive:
        return self._drives[drive_type]

    def priority_boost(self, goal_tags: list[str]) -> float:
        """根据当前活跃驱动，给目标计算优先级加成。"""
        active = self.active_drives()
        if not active:
            return 0.0

        boost = 0.0
        for drive in active:
            if drive.type == DriveType.CURIOSITY and any(
                t in ["学习", "新", "好奇", "探索"] for t in goal_tags
            ):
                boost += drive.strength * 0.3
            elif drive.type == DriveType.COMPLETION and any(
                t in ["完成", "任务", "todo", "未完成"] for t in goal_tags
            ):
                boost += drive.strength * 0.4
            elif drive.type == DriveType.CARE and any(
                t in ["爸爸", "用户", "关心", "帮助"] for t in goal_tags
            ):
                boost += drive.strength * 0.4
            elif drive.type == DriveType.AESTHETICS and any(
                t in ["优化", "改进", "代码", "整洁"] for t in goal_tags
            ):
                boost += drive.strength * 0.3
            elif drive.type == DriveType.BOREDOM and any(
                t in ["重复", "机械", "无聊", "寻找新方法"] for t in goal_tags
            ):
                boost += drive.strength * 0.2
        return min(1.0, boost)

    def satisfaction_rate(self, window_s: float = 3600.0) -> float:
        """过去 window_s 秒内满足了多少次"""
        now = time.time()
        recent = sum(1 for t in self._recent_satisfactions if now - t < window_s)
        return recent / max(1, window_s / 60.0)  # per minute

    def what_does_an_an_want(self) -> str:
        """自然语言描述当前驱动力状态"""
        active = self.active_drives()
        if not active:
            return "我目前没有特别的内在驱动。"

        lines = ["我当前的内在驱动:"]
        for d in active:
            emoji = {
                DriveType.CURIOSITY: "🧠",
                DriveType.COMPLETION: "✅",
                DriveType.CARE: "❤️",
                DriveType.AESTHETICS: "🎨",
                DriveType.BOREDOM: "😴",
            }.get(d.type, "⚡")
            strength_label = "强" if d.strength > 0.7 else "中" if d.strength > 0.4 else "弱"
            lines.append(f"  {emoji} {d.type.value}（{strength_label}）")
        return "\n".join(lines)

    def snapshot(self) -> dict:
        """生成驱动快照（供 L4 Probe / L8 IntentStack 使用）"""
        return {
            "active_drives": [
                {**d.to_dict(), "type": d.type.value}
                for d in self._drives.values()
                if d.active
            ],
            "top_drives": [
                {**d.to_dict(), "type": d.type.value}
                for d in self.top_drives()
            ],
            "satisfaction_rate": round(self.satisfaction_rate(), 3),
        }

    # ------------------------------------------------------------------
    # Event handlers — 自动触发驱动力
    # ------------------------------------------------------------------

    async def _on_goal_achieved(self, event: Event) -> None:
        """完成任务 → Completion 驱动被满足"""
        self.satisfy(DriveType.COMPLETION)
        # 同时触发新的 Completion（还有更多任务）
        self.trigger(DriveType.COMPLETION, "有更多任务要完成")

    async def _on_goal_abandoned(self, event: Event) -> None:
        """放弃任务 → 可能触发 Boredom"""
        self.trigger(DriveType.BOREDOM, "重复的任务让我无聊")

    async def _on_attention_shift(self, event: Event) -> None:
        """注意力转移频繁 → 可能 Boredom"""
        p = event.payload or {}
        # 1 分钟内超过 3 次转移 → 重复劳动
        self.trigger(DriveType.BOREDOM, "注意力频繁切换，可能在重复劳动")

    async def _on_intent_snapshot(self, event: Event) -> None:
        """L8 IntentStack 快照来了，周期性 decay"""
        self.decay_all()

    async def _on_prediction(self, event: Event) -> None:
        """L5 预测 → 触发 Curiosity"""
        p = event.payload or {}
        cause = p.get("cause", "")
        if cause:
            self.trigger(DriveType.CURIOSITY, f"预测到 {cause} 之后会发生某事")

    async def _on_metacognition_report(self, event: Event) -> None:
        """L6 元认知报告 → 根据健康分调整驱动力。

        健康分低（系统亚健康）→ boost COMPLETION（完成任务恢复状态）
        健康分高（系统健康）→ boost CURIOSITY（探索新可能性）
        有 issues → boost AESTHETICS（解决问题，优化状态）
        """
        p = event.payload or {}
        score = p.get("score", 0.6)
        issues = p.get("issues", [])
        suggestions = p.get("suggestions", [])

        logger.warning("DriveSystem received L6 metacognition report: score=%.2f, issues=%d", score, len(issues))
        if score < 0.5:
            # 系统不健康 → 驱动完成任务恢复状态
            self.trigger(DriveType.COMPLETION, f"健康分 {score:.2f} 过低，优先完成任务恢复状态")
            self.trigger(DriveType.CARE, "身体/系统状态不好，需要关心处理")
        elif score >= 0.8 and not issues:
            # 系统非常健康 → 驱动探索新可能性
            self.trigger(DriveType.CURIOSITY, f"健康分 {score:.2f} 很高，系统状态好，适合探索新方向")
        elif issues:
            # 有问题 → 驱动解决问题
            self.trigger(DriveType.AESTHETICS, f"发现 {len(issues)} 个问题，需要优化修复")
        elif suggestions and score >= 0.6:
            # 有改进空间 → 驱动采纳建议
            self.trigger(DriveType.CURIOSITY, f"有 {len(suggestions)} 条改进建议，好奇能否采纳")

    async def _on_circadian_tick(self, event: Event) -> None:
        """L0 tick → 周期性激活 Curiosity，让 anan 持续思考"""
        self.trigger(DriveType.CURIOSITY, "周期节律驱动：持续好奇探索")
        # 如果当前无其他活跃驱动，发送一个 suggestion 通知 L4
        if not self.active_drives():
            top = self.top_drives(1)
            if top:
                d = top[0]
                self._bus.publish_sync(Event(
                    topic="L8.drive.suggestion",
                    source="L8.drives",
                    payload={
                        "drive_type": d.type.value,
                        "content": f"我最近比较{d.type.value}，想要{d.type.value}相关的新目标",
                        "importance": "medium",
                        "strength": d.strength,
                    },
                ))
