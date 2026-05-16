"""
L6 Metacognition — Mirror（镜子）
====================================

anan 第一次能"照镜子"。

之前 L0-L9 都是无意识在跑。L6 不参与做事，专门**评估做事的 anan**：
  - 心脏健康吗？(bus 错误率、tick 节奏)
  - 身份在长吗？(self-model facts 增长率)
  - 注意力对吗？(working memory 各 layer 占比)
  - 梦境有内容吗？(L1 反思 facts 数量)

输出：HealthReport + 启发性建议（不是命令，是"我觉得我应该…"）

为什么不接 LLM？
  L6 v0.1 是**元认知骨架**——纯统计 + 阈值。
  LLM-based metacognition (真"反思自己"思维) 留给 v0.6+ 的 L6.deep。
  先把骨架立起来，能 emit 报告事件，let 其他层去回应。

事件:
  L6.metacognition.report  — 一次完整自省，payload = HealthReport.to_dict()
  L6.metacognition.warn    — 高严重度问题（healthy=False 或 critical issue）
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.L6.mirror")


@dataclass
class HealthReport:
    """anan 一次自省的快照。"""

    timestamp: str
    score: float                    # 0.0 ~ 1.0 综合分
    healthy: bool                   # score >= 0.6
    metrics: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)     # severity-ordered
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "score": round(self.score, 3),
            "healthy": self.healthy,
            "metrics": self.metrics,
            "issues": list(self.issues),
            "suggestions": list(self.suggestions),
        }

    def summary(self) -> str:
        flag = "✅" if self.healthy else "⚠️"
        head = f"{flag} health={self.score:.2f}"
        if self.issues:
            head += f"  问题: {len(self.issues)}"
        if self.suggestions:
            head += f"  建议: {len(self.suggestions)}"
        return head


class Mirror:
    """L6 元认知组件。

    Usage:
        mirror = Mirror(bus=bus, working_memory=wm, self_model=l9.model)
        report = await mirror.reflect()      # one-shot
        # OR keep emitting periodically
        await mirror.attach()                # subscribes to circadian.asleep
        # ... lives on bus until detach() ...
    """

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        working_memory=None,        # L3 — optional
        self_model=None,            # L9 SelfModel — optional
        # tunables
        attention_skew_threshold: float = 0.7,    # if any 1 layer > 70% of WM, flag
        identity_stagnation_cycles: int = 5,      # if no new identity facts in N reports, flag
        healthy_score_threshold: float = 0.6,     # score >= this → healthy
    ):
        self._bus = bus or get_bus()
        self._wm = working_memory
        self._self = self_model
        self._attn_thresh = attention_skew_threshold
        self._stag_cycles = identity_stagnation_cycles
        self._healthy_thresh = healthy_score_threshold
        self._unsub = None
        self._reports: list[HealthReport] = []
        self._last_identity_count: Optional[int] = None
        self._stagnation_streak = 0

    # ------------------------------------------------------------------
    async def attach(self) -> None:
        """Subscribe to L0.circadian.asleep (after each cycle) and L0.circadian.tick (periodic).
        
        After each sleep cycle: reflect_and_emit()
        During active phase: reflect every N ticks so Mirror is not completely silent
        """
        async def on_asleep(event: Event):
            await self.reflect_and_emit()

        async def on_tick(event: Event):
            # Only reflect on every 5th tick to keep overhead low
            payload = event.payload or {}
            ticks = payload.get("ticks", 0)
            logger.warning("MIRROR_PROBE ticks=%d", ticks)
            if ticks % 5 == 0:
                logger.warning("MIRROR_TRIGGER: calling reflect_and_emit() for ticks=%d", ticks)
                await self.reflect_and_emit()

        self._unsub_asleep = self._bus.subscribe("L0.circadian.asleep", on_asleep)
        self._unsub_tick = self._bus.subscribe("L0.circadian.tick", on_tick)

    async def stop(self) -> None:
        """供 MindStackRunner 调用。"""
        await self.detach()

    async def detach(self) -> None:
        if self._unsub_asleep:
            self._unsub_asleep()
            self._unsub_asleep = None
        if hasattr(self, '_unsub_tick') and self._unsub_tick:
            self._unsub_tick()
            self._unsub_tick = None

    # ------------------------------------------------------------------
    async def reflect_and_emit(self) -> HealthReport:
        logger.warning("MIRROR_ENTER reflect_and_emit called")
        try:
            report = self.reflect()
            logger.warning("MIRROR reflect done: score=%s", getattr(report, 'score', '?'))
        except Exception as exc:
            logger.warning("MIRROR reflect ERROR: %s", exc)
            raise
        await self._emit(report)
        return report

    def reflect(self) -> HealthReport:
        """Compute a HealthReport from currently attached sources."""
        metrics: dict = {}
        issues: list[str] = []
        suggestions: list[str] = []
        sub_scores: list[float] = []

        # ---- 1. Bus health (error rate) ----
        bus_stats = self._bus.stats()
        published = bus_stats.get("published", 0)
        errors = bus_stats.get("errors", 0)
        error_rate = (errors / published) if published > 0 else 0.0
        metrics["bus"] = {
            "published": published,
            "delivered": bus_stats.get("delivered", 0),
            "errors": errors,
            "error_rate": round(error_rate, 4),
        }
        if error_rate == 0:
            sub_scores.append(1.0)
        elif error_rate < 0.01:
            sub_scores.append(0.8)
        elif error_rate < 0.05:
            sub_scores.append(0.5)
            issues.append(f"事件总线错误率 {error_rate:.1%} 偏高")
            suggestions.append("查看最近 errors 来源，可能某 handler 在抛")
        else:
            sub_scores.append(0.2)
            issues.append(f"事件总线错误率 {error_rate:.1%} 严重")
            suggestions.append("立刻 detach 故障 handler，否则梦境也会受影响")

        # ---- 2. Self-model growth ----
        if self._self is not None:
            metrics["self"] = {
                "facts": (
                    len(self._self.identity_facts)
                    + len(self._self.vision_facts)
                    + len(self._self.history_facts)
                ),
                "identity": len(self._self.identity_facts),
                "vision": len(self._self.vision_facts),
                "history": len(self._self.history_facts),
            }
            id_count = len(self._self.identity_facts)
            if self._last_identity_count is None:
                self._stagnation_streak = 0
            elif id_count == self._last_identity_count:
                self._stagnation_streak += 1
            else:
                self._stagnation_streak = 0
            self._last_identity_count = id_count
            metrics["self"]["stagnation_streak"] = self._stagnation_streak

            if self._stagnation_streak == 0:
                sub_scores.append(1.0)
            elif self._stagnation_streak < self._stag_cycles:
                sub_scores.append(0.7)
            else:
                sub_scores.append(0.4)
                issues.append(
                    f"身份事实已经 {self._stagnation_streak} 个周期没增长"
                )
                suggestions.append("梦境内容太重复，试试在 active 阶段做点新事")

            if id_count == 0:
                issues.append("还没形成身份事实 — 我还不知道我是谁")
                suggestions.append("再睡几个深度梦，让 reflect_deep 跑")
        else:
            metrics["self"] = None

        # ---- 3. Attention distribution (working memory) ----
        if self._wm is not None:
            entries = self._wm.snapshot()
            metrics["working_memory"] = self._wm.stats()
            if entries:
                topic_layers = Counter(
                    e.event.topic.split(".")[0] for e in entries
                )
                metrics["working_memory"]["layer_distribution"] = dict(topic_layers)
                total = sum(topic_layers.values())
                top_layer, top_count = topic_layers.most_common(1)[0]
                top_share = top_count / total
                metrics["working_memory"]["top_layer_share"] = round(top_share, 3)

                if top_share > self._attn_thresh:
                    sub_scores.append(0.5)
                    issues.append(
                        f"注意力倾斜：{top_layer} 层占了 {top_share:.0%} "
                        f"({top_count}/{total})"
                    )
                    suggestions.append(
                        f"考虑提高其他层的 salience，避免只盯着 {top_layer}"
                    )
                else:
                    sub_scores.append(1.0)
            else:
                sub_scores.append(0.5)
                issues.append("working memory 是空的 — 没在感知任何事件")
        else:
            metrics["working_memory"] = None

        # ---- 4. Composite score ----
        score = sum(sub_scores) / len(sub_scores) if sub_scores else 0.0
        healthy = score >= self._healthy_thresh

        report = HealthReport(
            timestamp=datetime.now().isoformat(),
            score=score,
            healthy=healthy,
            metrics=metrics,
            issues=issues,
            suggestions=suggestions,
        )
        self._reports.append(report)
        return report

    # ------------------------------------------------------------------
    async def _emit(self, report: HealthReport) -> None:
        logger.warning("MIRROR_EMIT bus=%s id=%d", type(self._bus).__name__, id(self._bus))
        try:
            await self._bus.publish(Event(
                topic="L6.metacognition.report",
                source="L6.mirror",
                payload=report.to_dict(),
            ))
            if not report.healthy or any("严重" in i for i in report.issues):
                await self._bus.publish(Event(
                    topic="L6.metacognition.warn",
                    source="L6.mirror",
                    payload={
                        "score": report.score,
                        "issues": report.issues,
                        "suggestions": report.suggestions,
                    },
                ))
            logger.warning("MIRROR_EMIT done")
        except Exception as exc:  # noqa: BLE001
            logger.warning("MIRROR_EMIT_ERROR: %s", exc)

    # ------------------------------------------------------------------
    def history(self) -> list[HealthReport]:
        return list(self._reports)

    def latest(self) -> Optional[HealthReport]:
        return self._reports[-1] if self._reports else None
