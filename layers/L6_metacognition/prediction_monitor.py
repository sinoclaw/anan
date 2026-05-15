"""
L6 PredictionMonitor — 元认知监控 L5 预测系统
=============================================

监听 L5.prediction.confirmed / L5.prediction.failed 事件：
  - 追踪预测准确率（滑动窗口）
  - 准确率跌破阈值时触发链路衰减（调用 PredictiveReasoner._decay_link）
  - 持续低迷时发出 L6.metacognition.warn

Usage:
    pm = PredictionMonitor(
        bus=bus,
        predictor=predictive_reasoner,  # L5 PredictiveReasoner instance
        accuracy_threshold=0.4,           # 准确率低于此值开始衰减
        window=20,                       # 滑动窗口大小
    )
    await pm.attach()
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.L6.prediction_monitor")


@dataclass
class _OutcomeRecord:
    cause: str
    effect: str
    outcome: str          # "confirmed" or "failed"
    latency_s: float      # how long until confirmed/failed


class PredictionMonitor:
    """L6 元认知监控器：监控 L5 预测准确率，触发链路衰减修正。"""

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        predictor: Optional[object] = None,   # PredictiveReasoner instance
        accuracy_threshold: float = 0.4,       # 开始衰减的阈值
        severe_threshold: float = 0.25,         # 触发严重告警的阈值
        window: int = 20,                       # 滑动窗口大小
    ):
        self._bus = bus or get_bus()
        self._pred = predictor
        self._acc_thresh = accuracy_threshold
        self._severe_thresh = severe_threshold
        self._window = window

        # 滑动窗口：最近 window 个预测结果
        self._outcomes: list[_OutcomeRecord] = []
        # 累计计数（用于快速计算）
        self._total_confirmed = 0
        self._total_failed = 0

        self._unsub_confirmed: Optional[Callable[[], None]] = None
        self._unsub_failed: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    async def attach(self) -> None:
        async def on_confirmed(event: Event):
            await self._on_confirmed(event)

        async def on_failed(event: Event):
            await self._on_failed(event)

        self._unsub_confirmed = self._bus.subscribe(
            "L5.prediction.confirmed", on_confirmed,
        )
        self._unsub_failed = self._bus.subscribe(
            "L5.prediction.failed", on_failed,
        )
        # DIAGNOSTIC: catch L5.prediction.upcoming to confirm events arrive
        async def on_upcoming_diag(event: Event):
            logger = logging.getLogger("anan.L6.prediction_monitor")
            logger.info("[DIAG] L5.prediction.upcoming ARRIVED: %s", event.payload)
        self._unsub_upcoming_diag = self._bus.subscribe("L5.prediction.upcoming", on_upcoming_diag)

    async def detach(self) -> None:
        if self._unsub_confirmed:
            self._unsub_confirmed()
            self._unsub_confirmed = None
        if self._unsub_failed:
            self._unsub_failed()
            self._unsub_failed = None

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_confirmed(self, event: Event) -> None:
        payload = event.payload or {}
        cause = payload.get("cause", "?")
        effect = payload.get("effect", "?")
        latency_s = payload.get("prediction_horizon_s", 0.0)

        rec = _OutcomeRecord(cause=cause, effect=effect, outcome="confirmed", latency_s=latency_s)
        self._add_outcome(rec)
        logger.debug("Prediction confirmed: %s → %s", cause, effect)

    async def _on_failed(self, event: Event) -> None:
        payload = event.payload or {}
        cause = payload.get("cause", "?")
        predicted_effect = payload.get("predicted_effect", "?")
        age_s = payload.get("age_s", 0.0)

        rec = _OutcomeRecord(cause=cause, effect=predicted_effect, outcome="failed", latency_s=age_s)
        self._add_outcome(rec)

        # 触发链路衰减 — 预测失败说明这条因果链不可靠
        if self._pred is not None:
            decayed = self._pred._decay_link(cause, predicted_effect)
            if decayed:
                logger.info(
                    "Decayed causal link after failed prediction: %s → %s",
                    cause, predicted_effect,
                )

        # 持续低迷 → 告警
        acc = self.accuracy()
        if acc < self._severe_thresh and len(self._outcomes) >= self._window:
            await self._bus.publish(Event(
                topic="L6.metacognition.warn",
                source="L6.prediction_monitor",
                payload={
                    "issues": [
                        f"预测准确率 {acc:.0%} 严重低迷（阈值 {self._severe_thresh:.0%}）"
                    ],
                    "context": {
                        "confirmed": self._total_confirmed,
                        "failed": self._total_failed,
                        "window": len(self._outcomes),
                    },
                },
            ))

        logger.debug("Prediction failed: %s → %s (accuracy=%.1f%%)", cause, predicted_effect, acc * 100)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _add_outcome(self, rec: _OutcomeRecord) -> None:
        self._outcomes.append(rec)
        if len(self._outcomes) > self._window:
            removed = self._outcomes.pop(0)
            if removed.outcome == "confirmed":
                self._total_confirmed -= 1
            else:
                self._total_failed -= 1

        if rec.outcome == "confirmed":
            self._total_confirmed += 1
        else:
            self._total_failed += 1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def accuracy(self) -> float:
        """Current prediction accuracy over the sliding window."""
        total = self._total_confirmed + self._total_failed
        if total == 0:
            return 1.0   # no data yet → assume healthy
        return self._total_confirmed / total

    def stats(self) -> dict:
        return {
            "accuracy": round(self.accuracy(), 3),
            "confirmed": self._total_confirmed,
            "failed": self._total_failed,
            "window": len(self._outcomes),
            "threshold": self._acc_thresh,
        }
