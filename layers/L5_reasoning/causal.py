"""
L5 CausalReasoner — 因果推理 (anan 学会"为啥")
=================================================

L4 求证当下；L5 跨时间归纳：**A 之后 B 比平时更容易发生吗？**

核心思路（符号统计, 无 LLM, 完全自给自足）:
  1. 听 bus 所有事件, 滚动窗口缓存最近 N 个
  2. 维护 (cause_topic, effect_topic) 共现表:
       within_window[A][B] = A 之后 W 秒内出现 B 的次数
       baseline[B]         = B 总出现次数
  3. lift(A→B) = P(B|A 之后) / P(B 总体)
       lift > 1.5 + 出现 ≥ min_observations → 推断为「弱因果」
  4. 专项追踪 L7→L6: 每次 L7 acted, 比较前后 K 周期 L6.health 的
     平均值差 → 学到"这个 action 平均涨多少分"

发现的规律以 L5.causal.* 事件发出, 也写进 self_model 当 vision
("我学到 attenuate_layer_salience 平均涨 0.17 health").

设计原则:
  - 完全增量 — 每来 1 事件 O(W) 更新, 无需重跑全 history
  - 阈值可调 (min_obs / lift_threshold / window_s)
  - 失败优雅降级
  - 不写硬规则 — 全部从数据归纳
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.L5.causal")


@dataclass
class CausalLink:
    cause: str
    effect: str
    co_count: int           # cause 之后窗口内出现 effect 的次数
    cause_count: int        # cause 出现总次数
    effect_count: int       # effect 出现总次数
    total_events: int       # 总事件数 (基线分母)
    lift: float             # P(effect|after cause) / P(effect)

    @property
    def confidence(self) -> float:
        return self.co_count / max(self.cause_count, 1)

    def __str__(self) -> str:
        return (f"{self.cause} → {self.effect} "
                f"(lift={self.lift:.2f}, conf={self.confidence:.2f}, "
                f"obs={self.co_count})")


@dataclass
class ActionEffect:
    """L7 action → L6 health 专项跟踪结果."""
    action: str
    samples: int
    avg_delta: float            # 平均 health 变化
    last_before: float = 0.0
    last_after: float = 0.0


class CausalReasoner:
    """L5 — 跨时间归纳因果联系.

    Wiring:
        l5 = CausalReasoner(bus=bus, self_model=l9.model, window_s=2.0)
        await l5.attach()
    """

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        self_model=None,
        window_s: float = 2.0,
        min_observations: int = 3,
        lift_threshold: float = 1.5,
        history_capacity: int = 500,
        action_eval_lookahead: int = 2,
        ignore_topics: Optional[set[str]] = None,
    ):
        self._bus = bus or get_bus()
        self._sm = self_model
        self._window_s = window_s
        self._min_obs = min_observations
        self._lift_threshold = lift_threshold
        self._lookahead = action_eval_lookahead
        # Don't reason about own emissions or ticks (would dominate)
        self._ignore = ignore_topics or set()
        self._ignore.add("L9.self.wisdom_grown")  # prevent feedback loop

        self._recent: deque[tuple[float, str]] = deque(maxlen=history_capacity)
        self._cause_count: dict[str, int] = defaultdict(int)
        self._effect_count: dict[str, int] = defaultdict(int)
        self._co: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._total_events: int = 0

        # L7→L6 专项
        self._pending_actions: deque[tuple[str, float]] = deque()  # (action, time)
        self._action_effects: dict[str, list[float]] = defaultdict(list)
        self._health_buffer: deque[tuple[float, float]] = deque(maxlen=20)

        self._discovered_links: set[tuple[str, str]] = set()
        self._unsubs: list[Callable[[], None]] = []

    # ------------------------------------------------------------------
    async def attach(self) -> None:
        async def on_any(event: Event):
            await self._on_event(event)

        async def on_l7_acted(event: Event):
            action = event.payload.get("action", "unknown")
            self._pending_actions.append((action, time.monotonic()))

        async def on_l6_report(event: Event):
            score = float(event.payload.get("score", 0.0))
            now = time.monotonic()
            self._health_buffer.append((now, score))
            await self._evaluate_pending_actions(score)

        # Subscribe to specific layer prefixes (L1-L9, excluding L0 ticks for noise)
        for layer in ["L1", "L2", "L3", "L4", "L6", "L7", "L8", "L9"]:
            self._unsubs.append(self._bus.subscribe(f"{layer}.*", on_any))
        self._unsubs.append(self._bus.subscribe("L0.circadian.wake", on_any))
        self._unsubs.append(self._bus.subscribe("L0.circadian.bedtime", on_any))
        self._unsubs.append(self._bus.subscribe("L0.circadian.asleep", on_any))
        self._unsubs.append(self._bus.subscribe("L7.regulator.acted", on_l7_acted))
        self._unsubs.append(self._bus.subscribe("L6.metacognition.report", on_l6_report))

    async def detach(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()

    # ------------------------------------------------------------------
    async def _on_event(self, event: Event) -> None:
        topic = event.topic
        # Don't feed own output back
        if topic.startswith("L5."):
            return
        # Skip ultra-high-frequency layers that would dominate
        if topic.startswith("L0.circadian.tick"):
            return
        if topic in self._ignore:
            return

        now = time.time()

        # Update co-occurrence with all in-window predecessors
        for prev_t, prev_topic in self._recent:
            if now - prev_t > self._window_s:
                continue
            if prev_topic == topic:
                continue
            self._co[prev_topic][topic] += 1

        self._recent.append((now, topic))
        self._effect_count[topic] += 1
        self._cause_count[topic] += 1
        self._total_events += 1

        # Check for newly significant links involving this topic as effect
        await self._check_new_links(topic)

    async def _check_new_links(self, effect: str) -> None:
        # Only check causes that ALREADY co-occur with this effect
        # (sparse — most pairs never co-occur)
        for cause, effects in self._co.items():
            co = effects.get(effect, 0)
            if co < self._min_obs:
                continue
            key = (cause, effect)
            if key in self._discovered_links:
                continue
            link = self._build_link(cause, effect)
            if link.lift < self._lift_threshold:
                continue
            self._discovered_links.add(key)
            await self._announce_link(link)

    def _build_link(self, cause: str, effect: str) -> CausalLink:
        co = self._co[cause][effect]
        cause_n = self._cause_count[cause]
        effect_n = self._effect_count[effect]
        total = max(self._total_events, 1)
        p_effect = effect_n / total
        p_effect_given_cause = co / max(cause_n, 1)
        lift = p_effect_given_cause / p_effect if p_effect > 0 else 0.0
        return CausalLink(cause, effect, co, cause_n, effect_n, total, lift)

    async def _announce_link(self, link: CausalLink) -> None:
        await self._safe_publish("L5.causal.link_discovered", {
            "cause": link.cause,
            "effect": link.effect,
            "lift": link.lift,
            "confidence": link.confidence,
            "observations": link.co_count,
        })
        # Also emit L5.pattern.discovered so SelfRegulator._on_causal_pattern picks it up
        await self._safe_publish("L5.pattern.discovered", {
            "antecedent": link.cause,
            "consequent": link.effect,
            "support": link.co_count,
            "confidence": link.confidence,
            "lift": link.lift,
            "summary": (
                f"{link.cause} 之后更可能出现 {link.effect} "
                f"(lift={link.lift:.2f}, 置信={link.confidence:.0%})"
            ),
        })
        # Bake into self-model as learning history
        if self._sm is not None:
            try:
                self._sm.history_facts.append(
                    f"我学到: {link.cause} 之后更可能出现 {link.effect} "
                    f"(lift={link.lift:.2f})"
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("L5 self_model write failed: %s", exc)

    # ------------------------------------------------------------------
    # L7→L6 专项: 每次 L7 acted, 取该时刻附近 health 当 before,
    # 等下个 L6.report 当 after, 算 delta
    async def _evaluate_pending_actions(self, after_score: float) -> None:
        if not self._pending_actions:
            return
        now = time.monotonic()
        # Find baseline before any pending action — the most recent health
        # reading older than the oldest pending action
        oldest_action_t = self._pending_actions[0][1]
        before_score: Optional[float] = None
        for ht, hs in reversed(self._health_buffer):
            if ht < oldest_action_t:
                before_score = hs
                break

        ready: list[tuple[str, float]] = []
        remaining: deque[tuple[str, float]] = deque()
        for action, t in self._pending_actions:
            if now - t > 0.001:  # at least one report has passed
                ready.append((action, t))
            else:
                remaining.append((action, t))
        self._pending_actions = remaining

        if before_score is None:
            return  # no baseline yet, but actions consumed

        for action, t in ready:
            delta = after_score - before_score
            self._action_effects[action].append(delta)
            samples = len(self._action_effects[action])
            if samples >= 2:  # announce only after 2+ samples
                avg = sum(self._action_effects[action]) / samples
                await self._safe_publish("L5.causal.action_effect", {
                    "action": action,
                    "samples": samples,
                    "avg_delta": avg,
                    "last_delta": delta,
                })
                if self._sm is not None and abs(avg) > 0.05:
                    try:
                        marker = f"我学到行动: {action}"
                        # Replace previous learning for this action
                        self._sm.history_facts = [
                            f for f in self._sm.history_facts
                            if not f.startswith(marker)
                        ]
                        self._sm.history_facts.append(
                            f"{marker} 平均使 health {'+' if avg >= 0 else ''}{avg:.2f}"
                            f" (n={samples})"
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("L5 self_model action write failed: %s", exc)

    async def _safe_publish(self, topic: str, payload: dict) -> None:
        # Fire-and-forget — don't block the current handler chain on our own
        # downstream subscribers. Prevents re-entrant deadlock / latency stacking
        # when L5 publishes inside an L5 handler invocation.
        try:
            asyncio.create_task(self._bus.publish(Event(
                topic=topic, source="L5.causal", payload=payload,
            )))
        except Exception as exc:  # noqa: BLE001
            logger.debug("L5 publish failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    def discovered_links(self) -> list[CausalLink]:
        return [self._build_link(c, e) for (c, e) in self._discovered_links]

    def action_effects(self) -> dict[str, ActionEffect]:
        result: dict[str, ActionEffect] = {}
        for action, deltas in self._action_effects.items():
            if not deltas:
                continue
            result[action] = ActionEffect(
                action=action,
                samples=len(deltas),
                avg_delta=sum(deltas) / len(deltas),
                last_after=deltas[-1],
            )
        return result

    def what_did_i_learn(self) -> str:
        lines = ["我学到的因果联系:"]
        links = sorted(self.discovered_links(), key=lambda l: -l.lift)
        if not links:
            lines.append("  (还没看到稳定的规律)")
        for link in links[:5]:
            lines.append(f"  • {link}")
        actions = self.action_effects()
        if actions:
            lines.append("我评估过的行动效果:")
            for a in sorted(actions.values(), key=lambda x: -abs(x.avg_delta)):
                sign = "+" if a.avg_delta >= 0 else ""
                lines.append(f"  • {a.action}: 平均 health {sign}{a.avg_delta:.2f} (n={a.samples})")
        return "\n".join(lines)

    def stats(self) -> dict:
        return {
            "total_events_seen": self._total_events,
            "unique_topics": len(self._effect_count),
            "links_discovered": len(self._discovered_links),
            "actions_evaluated": len(self._action_effects),
            "window_s": self._window_s,
        }
