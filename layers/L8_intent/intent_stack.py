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
    # Listeners
    # ------------------------------------------------------------------
    async def _learn_from_l7(self, event: Event) -> None:
        """L7 acted — promote each adaptation into a 'keep this fixed' intent."""
        action = event.payload.get("action")
        detail = event.payload.get("detail", {})
        if action == "attenuate_layer_salience":
            layer = detail.get("layer", "?")
            await self.propose(
                key=f"keep_attention_balanced",
                description=f"保持注意力均衡 (上次抑制 {layer})",
                source="L7",
                detail={"last_layer": layer, "factor": detail.get("factor")},
            )
        elif action == "shorten_sleep_threshold":
            await self.propose(
                key="grow_identity",
                description="让身份持续生长 (缩短睡眠阈值寻求更多反思)",
                source="L7",
                detail=detail,
            )
        elif action == "emit_heal_intent":
            await self.propose(
                key="heal_bus",
                description="修复事件总线错误源",
                source="L7",
                detail=detail,
            )

    async def _weaken_most_failing(self, event: Event) -> None:
        """L5 discovered 'failure → reinforce' loop — weaken the most reinforced failing intent.
        
        This is anan learning 'if it's not working, stop trying harder':
        - Pick the intent with highest reinforce_count (being tried the most)
        - Halve its strength; if below floor, abandon it
        """
        if not self._intents:
            return
        # Find most-reinforced intent (we've been trying this the hardest)
        most_tried = max(self._intents.values(), key=lambda i: i.reinforce_count)
        if most_tried.reinforce_count < 2:
            return  # only act on intents we've actually tried multiple times

        # Halve it — "insanity is doing the same thing expecting different results"
        old_strength = most_tried.strength
        most_tried.strength *= 0.5
        logger.debug(
            "L5→L7 insight weakened intent '%s': %.2f → %.2f (reinforced %d times)",
            most_tried.key, old_strength, most_tried.strength, most_tried.reinforce_count,
        )

        if most_tried.strength < self._floor:
            await self._abandon(most_tried, reason="L5_insight_failing_pattern")
        else:
            await self._safe_publish("L8.intent.weakened", {
                **most_tried.to_dict(),
                "old_strength": round(old_strength, 4),
                "reason": event.payload.get("rationale", "L5 insight"),
            })

    async def _learn_from_l6(self, event: Event) -> None:
        """L6 reported — repeated issues become persistent intents."""
        # Each issue text is hashed to a stable key so reappearance reinforces.
        for issue in event.payload.get("issues", []):
            key = self._issue_to_key(issue)
            if key:
                await self.propose(
                    key=key,
                    description=self._issue_to_want(issue),
                    source="L6",
                    detail={"raw_issue": issue},
                )

    @staticmethod
    def _issue_to_key(issue: str) -> Optional[str]:
        if "注意力倾斜" in issue:
            return "keep_attention_balanced"
        if "身份" in issue and ("停滞" in issue or "没增长" in issue):
            return "grow_identity"
        if "错误率" in issue:
            return "heal_bus"
        if "我是谁" in issue or "self-model" in issue.lower():
            return "know_myself"
        return None

    @staticmethod
    def _issue_to_want(issue: str) -> str:
        if "注意力倾斜" in issue:
            return "保持注意力均衡, 不被某一层霸占"
        if "身份" in issue:
            return "让身份持续生长, 不停滞"
        if "错误率" in issue:
            return "保持事件总线健康"
        return f"应对: {issue[:30]}"
