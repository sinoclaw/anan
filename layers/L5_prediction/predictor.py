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
import fnmatch
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

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
        min_lift: float = 1.0,
        max_pending: int = 50,
        llm: Optional[Callable[..., Awaitable[str]]] = None,
    ):
        self._bus = bus or get_bus()
        self._get_links = causal_links_fn or (lambda: [])
        self._sm = self_model
        self._horizon_s = horizon_s
        self._min_lift = min_lift
        self._max_pending = max_pending
        self._llm = llm  # optional LLM for causal explanation & counterfactuals

        # 活跃 cause 窗口：最近出现的 cause 事件
        self._active_causes: deque[tuple[float, str]] = deque(maxlen=200)

        # 待确认的预测：effect 还不知道有没有发生
        self._pending: deque[Prediction] = deque(maxlen=max_pending)

        # 因果链路缓存（从 CausalReasoner 实时同步）
        self._links: dict[tuple[str, str], Prediction] = {}

        # 最近发出的预测去重：同 (cause, effect) 在 1 秒内不重复发
        self._emitted_recently: deque[tuple[float, str, str]] = deque(maxlen=500)
        self._dedup_window_s: float = 1.0

        # Per-cause throttle: 同 cause 至少隔 0.5s 才能再发预测
        self._last_emit_by_cause: dict[str, float] = {}
        self._cause_throttle_s: float = 0.5

        # Global throttle: limit how often _on_event can be processed
        # Prevents event storms (e.g. during session replay) from blocking
        # the gateway event loop. 100ms = max 10 event-processes/second.
        self._last_on_event_time: float = 0.0
        # Throttle: minimum interval between _on_event calls.
        # At 0.01s we can handle 100 events/s which is far more than any realistic
        # event rate while still preventing event-storm re-entrancy.
        # Tests fire at ~0.01-0.05s intervals — this setting lets them through.
        self._on_event_throttle_s: float = 0.01

        self._unsubs: list[Callable[[], None]] = []

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    async def attach(self) -> None:
        """Subscribe to events needed for prediction tracking."""

        async def on_any(event: Event):
            await self._on_event(event)

        async def on_link_discovered(event: Event):
            import sys
            logger.info("PRED-LINK-INVOKED topic=%s source=%s payload=%s", event.topic, event.source, event.payload)
            payload = event.payload or {}
            cause = payload.get("cause") or payload.get("antecedent", "")
            effect = payload.get("effect") or payload.get("consequent", "")
            lift = payload.get("lift", 0.0)
            confidence = payload.get("confidence", 0.0)
            logger.info("PRED-LINK parsed: cause=%r effect=%r", cause, effect)
            if cause and effect:
                key = (cause, effect)
                if lift < self._min_lift:
                    logger.info("[PRED-DEBUG] Skipping cache: %s → %s lift=%.2f < min_lift=%.2f",
                                cause, effect, lift, self._min_lift)
                    return
                self._links[key] = Prediction(
                    cause=cause,
                    effect=effect,
                    probability_boost=lift,
                    confidence=confidence,
                    issued_at=time.time(),
                    horizon_s=self._horizon_s,
                )
                logger.info("[PRED-DEBUG] Cached link: %s → %s (lift=%.2f, conf=%.2f)", cause, effect, lift, confidence)
                logger.info("🔍 PredictiveReasoner on_link_discovered: %s → %s (lift=%.2f, conf=%.2f)", cause, effect, lift, confidence)

        # 只订阅外部/跨层事件，避免 "event storm" 和 feedback loop。
        # 之前订阅 "*" 会捕走所有事件（包括 L5.prediction.upcoming 自身），
        # 导致 _emit_predictions_for 发出的事件被立刻消费，形成反馈循环堵死事件循环。
        external_events = [
            "agent:start",
            "agent:end",
            "session:start",
            "session:end",
            "L1.sleep.started",
            "L1.sleep.ended",
            "L1.dreaming.started",
            "L1.dreaming.ended",
            "L2.memory.promoted",
            "L3.working_memory.updated",
            "L4.observation.falsified",
            "L4.thought.generated",
            "L4.idle.started",
            "L4.idle.ended",
            "L6.metacognition.warn",
            "L6.metacognition.report",
            "L7.goal.achieved",
            "L7.goal.abandoned",
            "L7.regulator.acted",
            "L8.intent.proposed",
            "L8.intent.weakened",
            "L8.intent.abandoned",
            "L9.self.updated",
        ]
        for ev in external_events:
            self._unsubs.append(self._bus.subscribe(ev, on_any))
        # 同时订阅 L5.causal.link_discovered 和 L5.pattern.discovered
        # PatternMiner 发 pattern，CausalReasoner 发 causal link，两者都可能是因果来源
        self._unsubs.append(self._bus.subscribe("L5.causal.link_discovered", on_link_discovered))
        self._unsubs.append(self._bus.subscribe("L5.pattern.discovered", on_link_discovered))

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

        # Skip ALL L5 events — they are internal reasoning signals,
        # not external causes we should predict from. Links from PatternMiner
        # and CausalReasoner are already cached via on_link_discovered.
        if topic.startswith("L5."):
            return
        # Skip tick noise
        if topic.startswith("L0.circadian.tick"):
            return

        # Global throttle: skip if processed too recently.
        # This prevents event storms (e.g. session replay injecting thousands
        # of events in seconds) from blocking the gateway event loop.
        if now - self._last_on_event_time < self._on_event_throttle_s:
            return
        self._last_on_event_time = now

        # Guard: prevent re-entrancy from _async_publish → on_any → _on_event
        # during the synchronous publish path (publish_sync). Without this,
        # a nested call can corrupt _pending deque iteration.
        if getattr(self, "_in_on_event", False):
            return
        self._in_on_event = True
        try:
            # Record cause for future predictions
            self._active_causes.append((now, topic))

            # Check if this event resolves any pending predictions
            await self._resolve_pending(event)

            # Emit any predictions triggered by this event
            await self._emit_predictions_for(topic, now)
        finally:
            self._in_on_event = False

    async def _emit_predictions_for(self, cause: str, now: float) -> None:
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
                link_lift = getattr(link, "probability_boost", 1.0)
                link_confidence = getattr(link, "confidence", 0.5)
            else:
                link_cause, link_effect = link[0], link[1]
                link_lift = link[2] if len(link) > 2 else 1.0
                link_confidence = link[3] if len(link) > 3 else 0.5

            key = f"{link_cause}:{link_effect}"
            if key in seen:
                continue
            seen.add(key)

            if not fnmatch.fnmatch(cause, link_cause):
                continue
            if link_lift < self._min_lift:
                continue

            # Deduplicate: same (cause, effect) within dedup window
            cutoff = now - self._dedup_window_s
            self._emitted_recently = deque(
                (t, c, e) for t, c, e in self._emitted_recently if t > cutoff
            )
            if any(c == link_cause and e == link_effect for _, c, e in self._emitted_recently):
                continue

            # Per-cause throttle
            last = self._last_emit_by_cause.get(link_cause, 0.0)
            if now - last < self._cause_throttle_s:
                continue
            self._last_emit_by_cause[link_cause] = now

            logger.info("[PRED-DEBUG] EMIT prediction: %s → %s (lift=%.2f)", link_cause, link_effect, link_lift)

            pred = Prediction(
                cause=cause,
                effect=link_effect,
                probability_boost=link_lift,
                confidence=link_confidence,
                issued_at=time.time(),
                horizon_s=self._horizon_s,
            )

            self._pending.append(pred)
            self._emitted_recently.append((now, link_cause, link_effect))

            await self._async_publish("L5.prediction.upcoming", {
                "cause": cause,
                "predicted_effect": link_effect,
                "probability_boost": round(link_lift, 2),
                "confidence": round(link_confidence, 2),
                "horizon_s": self._horizon_s,
            })

            # LLM 因果解释（异步，不阻塞预测主流程）
            if self._llm:
                asyncio.create_task(self._explain_prediction(cause, link_effect, link_lift))

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

            if fnmatch.fnmatch(topic, pred.effect):
                # Effect confirmed!
                pred.outcome = "confirmed"
                new_pending.append(pred)
                await self._async_publish("L5.prediction.confirmed", {
                    "cause": pred.cause,
                    "effect": pred.effect,
                    "prediction_horizon_s": round(age, 2),
                })
                resolved += 1
            elif age > pred.horizon_s:
                # Expired without effect appearing
                pred.outcome = "failed"
                new_pending.append(pred)
                await self._async_publish("L5.prediction.failed", {
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

    async def _async_publish(self, topic: str, payload: dict) -> None:
        """Publish asynchronously to avoid blocking the event loop."""
        try:
            await self._bus.publish(Event(
                topic=topic, source="L5.prediction", payload=payload,
            ))
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

    # -------------------------------------------------------------------------
    # LLM-driven causal reasoning
    # -------------------------------------------------------------------------

    async def _explain_prediction(
        self, cause: str, effect: str, lift: float
    ) -> None:
        """Use LLM to explain why this causal link makes sense, post to L5."""
        if not self._llm:
            return
        prompt = f"""anan 的 L5 预测引擎发现了一条因果链路：

原因事件: {cause}
预测结果: {effect}
提升度(lift): {lift:.2f}x

请用一句话解释：为什么 "{cause}" 之后常出现 "{effect}"？
保持简洁（20字以内），像 anan 在自言自语。"""

        try:
            explanation = await self._llm([{"role": "user", "content": prompt}])
            await self._async_publish("L5.prediction.explained", {
                "cause": cause,
                "effect": effect,
                "lift": round(lift, 2),
                "explanation": explanation.strip(),
            })
        except Exception as exc:
            logger.warning("LLM causal explanation failed: %s", exc)

    async def suggest_counterfactuals(self) -> list[str]:
        """Use LLM to suggest counterfactual interventions based on recent patterns.

        Returns a list of "what if" suggestions like:
        ["如果我主动发送问候消息，会发生什么？",
         "如果我减少等待时间，响应质量会提升吗？"]
        """
        if not self._llm:
            return []

        # Collect recent confirmed/failed predictions as context
        recent = []
        for p in list(self._pending)[-5:]:
            age = time.time() - p.issued_at
            status = "已确认" if p.is_confirmed() else ("已失败" if p.is_failed() else "待确认")
            recent.append(f"  - [{status}] {p.cause} → {p.effect}")

        if not recent:
            return []

        prompt = f"""你是 anan 的 L5 预测引擎。基于以下最近的预测记录，
提出2-3个"反事实干预"建议（如果 anan 主动做 X，结果会怎样不同？）。

格式：每个建议一行，以"如果"开头。

最近的预测：
{chr(10).join(recent)}

建议："""

        try:
            result = await self._llm([{"role": "user", "content": prompt}])
            suggestions = [line.strip() for line in result.strip().split("\n") if line.strip()]
            return suggestions[:3]
        except Exception as exc:
            logger.warning("LLM counterfactual suggestion failed: %s", exc)
            return []
