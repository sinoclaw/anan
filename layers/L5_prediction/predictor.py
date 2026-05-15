"""
L5 PredictiveReasoner — 预测未来
=================================

基于 CausalReasoner 发现的因果链路，在 A 发生后预测 B 即将到来。

工作原理：
  1. 监听事件总线
  2. 维护一个"活跃因果链"窗口（最近 N 秒内出现的 cause 事件）
  3. 当 cause 出现，查找 lift > threshold 的链路，发布"预测"事件
  4. 预测不是占卜 — 是统计外推： lift=2.0 意味着 A 之后 B 出现概率是基线的 2 倍

发布事件：
  - L5.prediction.upcoming  {cause, predicted_effect, probability_boost, time_horizon_s}
  - L5.prediction.confirmed  {cause, effect}    （预测的 effect 真发生了）
  - L5.prediction.failed     {cause, effect}     （时间窗口内没发生）

与其他模块的关系：
  - CausalReasoner：提供 discovered_links（已发现的因果规则）
  - L9 SelfModel：预测结果写入 self_model.history_facts（"我预测了 X→Y"）
  - L7 Goals/L8 Intent：消费预测事件，驱动主动行动
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.L5.prediction")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Prediction:
    cause: str
    effect: str
    probability_boost: float   # lift 值：基线的倍数
    confidence: float          # 0-1，基于 co_count / total
    issued_at: float            # time.time() 预测发出时间
    horizon_s: float           # 有效时间窗口
    outcome: Optional[str] = None  # None=pending, "confirmed", "failed"

    def is_expired(self) -> bool:
        return time.time() - self.issued_at > self.horizon_s

    def is_confirmed(self) -> bool:
        return self.outcome == "confirmed"

    def is_failed(self) -> bool:
        # Failed = expired (ran out of time) OR explicitly marked failed
        return self.outcome == "failed" or (self.outcome is None and self.is_expired())

    def is_pending(self) -> bool:
        return self.outcome is None and not self.is_expired()


# ---------------------------------------------------------------------------
# PredictiveReasoner
# ---------------------------------------------------------------------------

class PredictiveReasoner:
    """L5 — 用已发现的因果规律预测未来事件.

    需要注入 CausalReasoner.discovered_links() 的引用，
    或者自己订阅 L5.causal.link_discovered 事件来实时更新链路表。
    """

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        causal_links_fn: Optional[Callable[[], list]] = None,
        self_model=None,
        horizon_s: float = 3.0,
        min_lift: float = 1.5,
        max_pending: int = 50,
    ):
        self._bus = bus or get_bus()
        self._get_links = causal_links_fn or (lambda: [])
        self._sm = self_model
        self._horizon_s = horizon_s
        self._min_lift = min_lift
        self._max_pending = max_pending

        # 活跃 cause 窗口：最近出现的 cause 事件
        self._active_causes: deque[tuple[float, str]] = deque(maxlen=200)

        # 待确认的预测：effect 还不知道有没有发生
        self._pending: deque[Prediction] = deque(maxlen=max_pending)

        # 因果链路缓存（从 CausalReasoner 实时同步）
        self._links: dict[tuple[str, str], Prediction] = {}

        self._unsubs: list[Callable[[], None]] = []

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    async def attach(self) -> None:
        """Subscribe to events needed for prediction tracking."""

        async def on_any(event: Event):
            await self._on_event(event)

        async def on_link_discovered(event: Event):
            # 实时接收 CausalReasoner 发现的新链路
            cause = event.payload.get("cause", "")
            effect = event.payload.get("effect", "")
            lift = event.payload.get("lift", 0.0)
            confidence = event.payload.get("confidence", 0.0)
            if lift >= self._min_lift:
                key = (cause, effect)
                self._links[key] = Prediction(
                    cause=cause,
                    effect=effect,
                    probability_boost=lift,
                    confidence=confidence,
                    issued_at=time.time(),
                    horizon_s=self._horizon_s,
                )
                logger.debug("Cached new link: %s → %s (lift=%.2f)", cause, effect, lift)

        # Subscribe to ALL events so we can detect when predicted effects occur.
        # _on_event filters out L5.* and L0.circadian.tick internally.
        self._unsubs.append(self._bus.subscribe("*", on_any))
        self._unsubs.append(self._bus.subscribe("L5.causal.link_discovered", on_link_discovered))

    async def detach(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()

    # ------------------------------------------------------------------
    # Core prediction logic
    # ------------------------------------------------------------------

    async def _on_event(self, event: Event) -> None:
        topic = event.topic
        now = time.time()

        # Skip predictions about our own emissions
        if topic.startswith("L5."):
            return
        # Skip tick noise
        if topic.startswith("L0.circadian.tick"):
            return

        # Record cause for future predictions
        self._active_causes.append((now, topic))

        # Check if this event resolves any pending predictions
        await self._resolve_pending(event)

        # Emit any predictions triggered by this event
        await self._emit_predictions_for(topic)

    async def _emit_predictions_for(self, cause: str) -> None:
        """Given a cause event, emit predictions for downstream effects."""
        # Collect links from both sources: initial causal_links_fn AND
        # cached links from L5.causal.link_discovered events
        links_from_fn = self._get_links()
        links_from_cache = list(self._links.values())

        seen: set[str] = set()  # deduplicate by effect string

        for link in [*links_from_fn, *links_from_cache]:
            # Normalise to (cause, effect, lift, confidence)
            if hasattr(link, "cause"):
                link_cause = link.cause
                link_effect = link.effect
                link_lift = getattr(link, "lift", 1.0)
                link_confidence = getattr(link, "confidence", 0.5)
            else:
                link_cause, link_effect = link[0], link[1]
                link_lift = link[2] if len(link) > 2 else 1.0
                link_confidence = link[3] if len(link) > 3 else 0.5

            key = f"{link_cause}:{link_effect}"
            if key in seen:
                continue
            seen.add(key)

            if link_cause != cause:
                continue
            if link_lift < self._min_lift:
                continue

            pred = Prediction(
                cause=cause,
                effect=link_effect,
                probability_boost=link_lift,
                confidence=link_confidence,
                issued_at=time.time(),
                horizon_s=self._horizon_s,
            )

            self._pending.append(pred)

            await self._safe_publish("L5.prediction.upcoming", {
                "cause": cause,
                "predicted_effect": link_effect,
                "probability_boost": round(link_lift, 2),
                "confidence": round(link_confidence, 2),
                "horizon_s": self._horizon_s,
            })

            if self._sm is not None:
                try:
                    self._sm.history_facts.append(
                        f"我预测: {cause} 之后会出现 {link_effect} "
                        f"(置信度 {link_confidence:.0%}, 窗口 {self._horizon_s}s)"
                    )
                except Exception:
                    pass

    async def _resolve_pending(self, event: Event) -> None:
        """Check if any pending prediction was confirmed or failed."""
        topic = event.topic
        now = time.time()
        resolved = 0

        new_pending: deque[Prediction] = deque(maxlen=self._max_pending)

        for pred in self._pending:
            if pred.outcome is not None:
                # already resolved, keep it in deque for stats reporting
                new_pending.append(pred)
                continue

            age = now - pred.issued_at

            if pred.effect == topic:
                # Effect confirmed!
                pred.outcome = "confirmed"
                new_pending.append(pred)
                await self._safe_publish("L5.prediction.confirmed", {
                    "cause": pred.cause,
                    "effect": pred.effect,
                    "prediction_horizon_s": round(age, 2),
                })
                resolved += 1
            elif age > pred.horizon_s:
                # Expired without effect appearing
                pred.outcome = "failed"
                new_pending.append(pred)
                await self._safe_publish("L5.prediction.failed", {
                    "cause": pred.cause,
                    "predicted_effect": pred.effect,
                    "age_s": round(age, 2),
                })
                resolved += 1
            else:
                # Still pending
                new_pending.append(pred)

        self._pending = new_pending

        if resolved > 0:
            logger.debug("Resolved %d predictions (confirmed/failed)", resolved)

    async def _safe_publish(self, topic: str, payload: dict) -> None:
        try:
            asyncio.create_task(self._bus.publish(Event(
                topic=topic, source="L5.prediction", payload=payload,
            )))
        except Exception as exc:
            logger.debug("L5.prediction publish failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def pending_predictions(self) -> list[Prediction]:
        return [p for p in self._pending if p.is_pending()]

    def confirmed_predictions(self) -> list[Prediction]:
        return [p for p in self._pending if p.is_confirmed()]

    def failed_predictions(self) -> list[Prediction]:
        return [p for p in self._pending if p.is_failed()]

    def accuracy(self) -> float:
        """Precision: confirmed / (confirmed + failed). Returns 0.0 if no resolved predictions."""
        confirmed = len(self.confirmed_predictions())
        failed = len(self.failed_predictions())
        total = confirmed + failed
        return confirmed / total if total > 0 else 0.0

    def _decay_link(self, cause: str, effect: str, decay: float = 0.15) -> bool:
        """Decay a causal link's lift after prediction failure.

        Called by L6 PredictionMonitor when a prediction expires without confirmation.
        Returns True if the link was found and decayed, False if not found.
        """
        key = (cause, effect)
        if key not in self._links:
            return False

        link = self._links[key]
        old_lift = link.probability_boost
        link.probability_boost = max(0.1, old_lift - decay * old_lift)
        link.confidence = max(0.0, link.confidence - 0.05)
        logger.debug(
            "Decayed link %s→%s: lift %.2f → %.2f, conf %.2f → %.2f",
            cause, effect,
            old_lift, link.probability_boost,
            link.confidence + 0.05, link.confidence,
        )
        return True

    def stats(self) -> dict:
        pending = self.pending_predictions()
        return {
            "links_cached": len(self._links),
            "pending": len(pending),
            "confirmed": len(self.confirmed_predictions()),
            "failed": len(self.failed_predictions()),
            "accuracy": round(self.accuracy(), 3),
            "horizon_s": self._horizon_s,
        }

    def what_do_i_predict(self) -> str:
        lines = ["我当前的预测:"]
        pending = self.pending_predictions()
        if not pending:
            lines.append("  (暂无待确认的预测)")
            return "\n".join(lines)

        for p in pending[:5]:
            lines.append(
                f"  → {p.cause} 后会出现 {p.effect} "
                f"(置信度 {p.confidence:.0%}, 剩余 {p.horizon_s - (time.time() - p.issued_at):.1f}s)"
            )
        return "\n".join(lines)
