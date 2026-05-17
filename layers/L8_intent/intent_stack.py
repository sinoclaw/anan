"""
L8 IntentStack — 持续渴望
==========================

L7 是反射 — 看见问题做一次就完。L8 是渴望 — 一件事会**一直想着**直到完成。

设计模型 (灵感来自人类的 "持续在意" 状态):
  - **Intent**: 一个想要保持/改变的状态; 有 strength (0..1) 和 age (cycles)
  - **propose**: 新出现, strength=initial; 入栈
  - **reinforce**: 同一意图再次被触发 → strength 提升 (对数饱和)
  - **decay**: 每 tick/cycle 自然衰减 strength *= decay_factor
  - **satisfy**: 被外部信号判定完成 → strength 衰减更快 → 滑出
  - **abandon**: strength 跌破 floor → 出栈, 记入历史

栈 = top-N 按 strength 排序; 上限 capacity (默认 7±2 — 米勒数字).

订阅来源:
  - L7.regulator.acted   → 把 L7 的修正升格成"持续保持"意图
  - L6.metacognition.report → 把 L6 反复出现的 issue/suggestion 升格

发出的事件:
  - L8.intent.proposed   → 新意图入栈
  - L8.intent.reinforced → 老意图被加强
  - L8.intent.abandoned  → 出栈
  - L8.intent.snapshot   → 周期性快照, 供 L1 deep dream 拿"我最在意的 3 件事"

API:
  - what_do_i_want() → 自然语言描述 top-3
  - top(n=3)         → list[Intent]
  - all_intents()    → 全栈
  - history()        → 已 abandoned 的旧渴望
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from kernel.event_bus import Event, EventBus, get_bus

from layers.L8_intent.intent_monitoring_advisor import (
    IntentMonitoringAdvisor,
    IntentContext,
    IntentDecision,
)

logger = logging.getLogger("anan.L8.intent_stack")


@dataclass
class Intent:
    """A persistent want — anan 一直惦记着的某件事."""
    key: str                 # canonical id (e.g. "balance_attention", "heal_bus")
    description: str         # 人话, 用来回答 what_do_i_want()
    source: str              # who proposed: L6/L7/manual
    strength: float          # 0..1, 越高越在意
    proposed_at: str         # ISO timestamp
    last_reinforced_at: str  # ISO timestamp
    reinforce_count: int = 0
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "description": self.description,
            "source": self.source,
            "strength": round(self.strength, 4),
            "reinforce_count": self.reinforce_count,
            "proposed_at": self.proposed_at,
            "last_reinforced_at": self.last_reinforced_at,
            "detail": self.detail,
        }


class IntentStack:
    """L8 — anan 持续在意的事的栈.

    Wiring:
        l8 = IntentStack(bus=bus)
        await l8.attach()           # listens to L6/L7
        intents = l8.top(3)         # what anan most wants right now
        text = l8.what_do_i_want()
        await l8.detach()
    """

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        capacity: int = 7,                  # 米勒数字
        initial_strength: float = 0.5,
        reinforce_alpha: float = 0.25,      # log saturation rate
        decay_factor: float = 0.92,         # per decay tick
        abandon_floor: float = 0.05,
        snapshot_topic: str = "L8.intent.snapshot",
    ):
        self._bus = bus or get_bus()
        self._capacity = capacity
        self._init_str = initial_strength
        self._alpha = reinforce_alpha
        self._decay = decay_factor
        self._floor = abandon_floor
        self._snapshot_topic = snapshot_topic
        self._intents: dict[str, Intent] = {}
        self._abandoned: list[Intent] = []
        self._unsubs: list[Callable[[], None]] = []
        self._advisor = IntentMonitoringAdvisor()
        self._delegate_fn: Optional[Callable[..., any]] = None

    def set_delegate(self, fn: Callable[..., any]) -> None:
        """MindStackRunner 注入 runtime handle 的 delegate 函数."""
        self._delegate_fn = fn
        self._advisor.set_delegate(fn)

    # ------------------------------------------------------------------
    async def attach(self) -> None:
        """Subscribe to L6.report and L7.acted to grow intents organically."""
        async def on_l6(event: Event):
            await self._learn_from_l6(event)

        async def on_l7(event: Event):
            await self._learn_from_l7(event)

        async def on_weaken_intent(event: Event):
            await self._weaken_most_failing(event)

        self._unsubs.append(self._bus.subscribe("L6.metacognition.report", on_l6))
        self._unsubs.append(self._bus.subscribe("L7.regulator.acted", on_l7))
        self._unsubs.append(self._bus.subscribe("L7.regulator.weaken_intent", on_weaken_intent))
        self._unsubs.append(self._bus.subscribe("L5.causal.action_effect", self._on_action_effect))
        self._unsubs.append(self._bus.subscribe("L5.pattern.discovered", self._on_pattern_discovered))
        self._unsubs.append(self._bus.subscribe("L3.attention.shift", self._on_attention_shift))
        self._unsubs.append(self._bus.subscribe("L8.drive.suggestion", self._on_drive_suggestion))
        self._unsubs.append(self._bus.subscribe("L4.thought.pushed", self._on_thought_pushed))

    async def stop(self) -> None:
        """供 MindStackRunner 调用，等价于 detach()。"""
        await self.detach()

    async def detach(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()

    # ------------------------------------------------------------------
    # Public manual API (also used by L7/L6 listeners)
    # ------------------------------------------------------------------
    async def propose(
        self, key: str, description: str, source: str = "manual",
        detail: Optional[dict] = None,
    ) -> Intent:
        """Add or reinforce an intent."""
        existing = self._intents.get(key)
        now = datetime.now().isoformat()
        if existing is None:
            intent = Intent(
                key=key, description=description, source=source,
                strength=self._init_str,
                proposed_at=now, last_reinforced_at=now,
                detail=detail or {},
            )
            self._intents[key] = intent
            await self._safe_publish("L8.intent.proposed", intent.to_dict())
            self._enforce_capacity()
            return intent
        # reinforce
        existing.strength = self._reinforced(existing.strength)
        existing.reinforce_count += 1
        existing.last_reinforced_at = now
        if detail:
            existing.detail.update(detail)
        await self._safe_publish("L8.intent.reinforced", existing.to_dict())
        return existing

    async def satisfy(self, key: str) -> Optional[Intent]:
        """Mark intent satisfied — accelerates its decay."""
        intent = self._intents.get(key)
        if intent is None:
            return None
        intent.strength *= 0.4
        if intent.strength < self._floor:
            await self._abandon(intent, reason="satisfied")
        return intent

    async def decay_tick(self) -> int:
        """Apply natural decay; return number abandoned this tick."""
        abandoned = 0
        for key in list(self._intents.keys()):
            intent = self._intents[key]
            intent.strength *= self._decay
            if intent.strength < self._floor:
                await self._abandon(intent, reason="decay")
                abandoned += 1
        return abandoned

    async def snapshot(self) -> None:
        """Emit a snapshot event — L1 deep dream listens to this."""
        top = self.top(3)
        await self._safe_publish(self._snapshot_topic, {
            "top_intents": [i.to_dict() for i in top],
            "stack_size": len(self._intents),
        })

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def top(self, n: int = 3) -> list[Intent]:
        return sorted(
            self._intents.values(), key=lambda i: i.strength, reverse=True,
        )[:n]

    def all_intents(self) -> list[Intent]:
        return sorted(
            self._intents.values(), key=lambda i: i.strength, reverse=True,
        )

    def history(self) -> list[Intent]:
        return list(self._abandoned)

    def get(self, key: str) -> Optional[Intent]:
        return self._intents.get(key)

    def what_do_i_want(self) -> str:
        top = self.top(3)
        if not top:
            return "我现在没什么特别想要的，就这么活着。"
        lines = ["我现在最在意的事:"]
        for i, intent in enumerate(top, 1):
            stars = "★" * min(int(intent.strength * 5) + 1, 5)
            lines.append(
                f"  {i}. {intent.description} {stars}"
                f" (强度={intent.strength:.2f}, 加固{intent.reinforce_count}次)"
            )
        return "\n".join(lines)

    def stats(self) -> dict:
        return {
            "active": len(self._intents),
            "capacity": self._capacity,
            "abandoned_total": len(self._abandoned),
            "top_3": [(i.key, round(i.strength, 3)) for i in self.top(3)],
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _reinforced(self, current: float) -> float:
        # Log-saturating: each reinforcement adds less.
        # new = current + alpha * (1 - current); approaches 1 asymptotically.
        return min(1.0, current + self._alpha * (1.0 - current))

    def _enforce_capacity(self) -> None:
        if len(self._intents) <= self._capacity:
            return
        # Drop weakest until back at capacity
        weakest = sorted(
            self._intents.values(), key=lambda i: i.strength,
        )[: len(self._intents) - self._capacity]
        for w in weakest:
            self._intents.pop(w.key, None)
            self._abandoned.append(w)

    async def _abandon(self, intent: Intent, reason: str) -> None:
        self._intents.pop(intent.key, None)
        self._abandoned.append(intent)
        payload = intent.to_dict()
        payload["abandon_reason"] = reason
        await self._safe_publish("L8.intent.abandoned", payload)

    async def _safe_publish(self, topic: str, payload: dict) -> None:
        try:
            await self._bus.publish(Event(
                topic=topic, source="L8.intent_stack", payload=payload,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("L8 publish failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Advisor integration helpers
    # ------------------------------------------------------------------

    def _stack_health(self) -> dict:
        """Build stack health dict for IntentContext."""
        top = self.top(3)
        return {
            "stack_size": len(self._intents),
            "capacity": self._capacity,
            "decay_rate": self._decay,
            "top_intents": [i.to_dict() for i in top],
            "abandoned_recently": len([a for a in self._abandoned[-10:]]),
        }

    async def _apply_decision(self, decision: IntentDecision, ctx: IntentContext) -> None:
        """Apply advisor decision to the intent stack."""
        d = decision.decision
        if d == "propose" and decision.key:
            existing = self._intents.get(decision.key)
            if existing is None:
                intent = Intent(
                    key=decision.key,
                    description=decision.description or "",
                    source=ctx.source_layer,
                    strength=decision.strength,
                    proposed_at=datetime.now().isoformat(),
                    last_reinforced_at=datetime.now().isoformat(),
                    detail=decision.detail,
                )
                self._intents[decision.key] = intent
                await self._safe_publish("L8.intent.proposed", intent.to_dict())
                self._enforce_capacity()
            else:
                existing.strength = self._reinforced(existing.strength)
                existing.reinforce_count += 1
                existing.last_reinforced_at = datetime.now().isoformat()
                if decision.detail:
                    existing.detail.update(decision.detail)
                await self._safe_publish("L8.intent.reinforced", existing.to_dict())

        elif d == "reinforce" and decision.key:
            existing = self._intents.get(decision.key)
            if existing:
                existing.strength = self._reinforced(existing.strength)
                existing.reinforce_count += 1
                existing.last_reinforced_at = datetime.now().isoformat()
                await self._safe_publish("L8.intent.reinforced", existing.to_dict())

        elif d == "weaken" and decision.key:
            existing = self._intents.get(decision.key)
            if existing:
                old = existing.strength
                existing.strength = decision.strength
                logger.debug("L8 advisor weakened '%s': %.3f → %.3f (%s)",
                             decision.key, old, decision.strength, decision.reasoning)
                await self._safe_publish("L8.intent.weakened", {
                    **existing.to_dict(), "old_strength": round(old, 4),
                    "reason": decision.reasoning,
                })

        elif d == "abandon" and decision.key:
            existing = self._intents.get(decision.key)
            if existing:
                await self._abandon(existing, reason=f"advisor:{decision.reasoning}")

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------
    async def _learn_from_l7(self, event: Event) -> None:
        """L7 acted — ask advisor whether to promote to a persistent intent."""
        action = event.payload.get("action")
        detail = event.payload.get("detail", {})
        if not action:
            return

        ctx = IntentContext(
            source_layer="L7",
            event_type="l7_acted",
            action=action,
            action_detail=detail,
            **self._stack_health(),
        )
        decision = await self._advisor.evaluate(ctx)
        if decision.decision != "skip":
            await self._apply_decision(decision, ctx)
        else:
            logger.debug("IntentMonitoringAdvisor skipped L7 action '%s': %s", action, decision.reasoning)

    async def _weaken_most_failing(self, event: Event) -> None:
        """L5 discovered 'failure → reinforce' loop — use advisor to decide how to respond.

        Asks the advisor: weaken, abandon, or give another chance?
        """
        if not self._intents:
            return
        # Find most-reinforced intent (we've been trying this the hardest)
        most_tried = max(self._intents.values(), key=lambda i: i.reinforce_count)
        if most_tried.reinforce_count < 2:
            return  # only act on intents we've actually tried multiple times

        ctx = IntentContext(
            source_layer="L5",
            event_type="weaken_failing",
            failing_intent_key=most_tried.key,
            failing_intent_strength=most_tried.strength,
            failing_intent_reinforce_count=most_tried.reinforce_count,
            **self._stack_health(),
        )
        decision = await self._advisor.evaluate(ctx)
        if decision.decision == "skip":
            logger.debug("IntentMonitoringAdvisor skipped weaken for '%s': %s",
                        most_tried.key, decision.reasoning)
            return
        await self._apply_decision(decision, ctx)

    async def _learn_from_l6(self, event: Event) -> None:
        """L6 reported — ask advisor whether repeated issues become persistent intents."""
        for issue in event.payload.get("issues", []):
            if not issue:
                continue
            ctx = IntentContext(
                source_layer="L6",
                event_type="l6_report",
                raw_issue=issue,
                **self._stack_health(),
            )
            decision = await self._advisor.evaluate(ctx)
            if decision.decision != "skip":
                await self._apply_decision(decision, ctx)

    # ------------------------------------------------------------------
    # L5 因果 listener — L5 发现行动效果，L8 把它升格为持续意图
    # ------------------------------------------------------------------
    async def _on_action_effect(self, event: Event) -> None:
        """L5 评估了某个 L7 action 的效果 — advisor 判断是否升格为持续意图."""
        payload = event.payload or {}
        action = payload.get("action", "")
        avg_delta = payload.get("avg_delta", 0.0)
        samples = payload.get("samples", 0)

        if not action:
            return

        ctx = IntentContext(
            source_layer="L5",
            event_type="action_effect",
            action=action,
            avg_delta=avg_delta,
            samples=samples,
            **self._stack_health(),
        )
        decision = await self._advisor.evaluate(ctx)
        if decision.decision != "skip":
            await self._apply_decision(decision, ctx)

    # ------------------------------------------------------------------
    # L5 PatternMiner listener — L5 发现了正向因果规则，L8 把它升格为持续渴望
    # ------------------------------------------------------------------
    async def _on_pattern_discovered(self, event: Event) -> None:
        """L5 PatternMiner 发现了 A→B 高置信规则 — advisor 判断是否升格为持续渴望."""
        payload = event.payload or {}
        antecedent = payload.get("antecedent", "")
        consequent = payload.get("consequent", "")
        confidence = payload.get("confidence", 0.0)
        lift = payload.get("lift", 1.0)

        if not antecedent or not consequent:
            return

        ctx = IntentContext(
            source_layer="L5.miner",
            event_type="pattern_discovered",
            antecedent=antecedent,
            consequent=consequent,
            confidence=confidence,
            lift=lift,
            **self._stack_health(),
        )
        decision = await self._advisor.evaluate(ctx)
        if decision.decision != "skip":
            await self._apply_decision(decision, ctx)

    # ------------------------------------------------------------------
    # L3 Attention listener — 注意力长期集中在某类事件上 → 升格为持续意图
    # ------------------------------------------------------------------
    async def _on_attention_shift(self, event: Event) -> None:
        """L3 注意力转移 → 如果同一 layer 持续被关注，强化为 L8 意图。"""
        payload = event.payload or {}
        layer = payload.get("layer", "")
        duration_s = payload.get("duration_s", 0.0)
        focus_score = payload.get("focus_score", 0.0)

        if not layer or duration_s < 30.0:
            return  # ignore fleeting attention

        key = f"focus_on_{layer}"
        existing = self._intents.get(key)
        if existing is not None:
            # reinforce existing
            existing.strength = min(existing.strength + 0.05, 0.85)
            existing.last_reinforced_at = datetime.now().isoformat()
            await self._safe_publish("L8.intent.reinforced", existing.to_dict())
        else:
            intent = Intent(
                key=key,
                description=f"持续关注 {layer} 层（已专注 {duration_s:.0f}s，关注度 {focus_score:.2f}）",
                source="L3.attention",
                strength=0.3,
                proposed_at=datetime.now().isoformat(),
                last_reinforced_at=datetime.now().isoformat(),
                detail={"layer": layer, "duration_s": duration_s, "focus_score": focus_score},
            )
            self._intents[key] = intent
            await self._safe_publish("L8.intent.proposed", intent.to_dict())

    # ------------------------------------------------------------------
    # L8 Drive listener — 驱动力建议 → 直接升格为 L8 意图
    # ------------------------------------------------------------------
    async def _on_drive_suggestion(self, event: Event) -> None:
        """L8 Drive 发出了驱动力建议 → 升格为持续意图。"""
        payload = event.payload or {}
        content = payload.get("content", "")
        drive_type = payload.get("drive_type", "unknown")
        importance = payload.get("importance", "medium")

        if not content:
            return

        # Map drive importance to intent strength
        imp_map = {"low": 0.25, "medium": 0.4, "high": 0.6, "critical": 0.8}
        strength = imp_map.get(importance, 0.35)

        key = f"drive_{drive_type}"
        existing = self._intents.get(key)
        if existing is not None:
            existing.strength = min(existing.strength + strength * 0.3, 0.85)
            existing.last_reinforced_at = datetime.now().isoformat()
            await self._safe_publish("L8.intent.reinforced", existing.to_dict())
        else:
            intent = Intent(
                key=key,
                description=content,
                source=f"L8.drive.{drive_type}",
                strength=strength,
                proposed_at=datetime.now().isoformat(),
                last_reinforced_at=datetime.now().isoformat(),
                detail={"drive_type": drive_type, "importance": importance},
            )
            self._intents[key] = intent
            await self._safe_publish("L8.intent.proposed", intent.to_dict())

    # ------------------------------------------------------------------
    # L4 Thought listener — 被推给用户的想法 → 升格为 L8 意图
    # ------------------------------------------------------------------
    async def _on_thought_pushed(self, event: Event) -> None:
        """L4 推送了一个想法给用户 → 说明这个想法足够重要，升格为持续意图。"""
        payload = event.payload or {}
        content = payload.get("content", "")
        thought_type = payload.get("thought_type", "")
        importance = payload.get("importance", "medium")

        if not content:
            return

        imp_map = {"low": 0.3, "medium": 0.45, "high": 0.65, "critical": 0.85}
        strength = imp_map.get(importance, 0.4)

        key = f"thought_{thought_type}"
        existing = self._intents.get(key)
        if existing is not None:
            existing.strength = min(existing.strength + 0.1, 0.9)
            existing.last_reinforced_at = datetime.now().isoformat()
            await self._safe_publish("L8.intent.reinforced", existing.to_dict())
        else:
            intent = Intent(
                key=key,
                description=f"主动思考：{content[:80]}",
                source="L4.thought.pushed",
                strength=strength,
                proposed_at=datetime.now().isoformat(),
                last_reinforced_at=datetime.now().isoformat(),
                detail={"thought_type": thought_type, "importance": importance},
            )
            self._intents[key] = intent
            await self._safe_publish("L8.intent.proposed", intent.to_dict())
