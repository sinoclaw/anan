"""
L7 Self-Regulator — 闭环调节
==============================

L6 镜子能照出问题但只是发报告。L7 是 anan 第一次"听镜子的话"——
拿到 L6.warn 事件后**真的改自己**。

动作类型（每种问题对应一种 adaptation）:
  - bus 错误率高 → 发 L7.intent.heal_bus，建议外层 detach 故障 handler；
                    自己留 history 记录到 self_model
  - 注意力倾斜 → 给 working_memory 加 layer attenuation（被霸占的层降权）
  - 身份停滞 → 缩短 sleep_threshold，让心跳更频繁，更多睡眠 → 更多反思
  - 通用：每次 adaptation 都发 L7.regulator.acted 事件，让 L9 能记下来

设计原则:
  1. L7 **不直接做事**——通过修改其他组件的可调参数实现调节
  2. 每次调节有上限/下限，避免漂移
  3. adaptation 历史可查，便于 L6 评估"我的调节有用吗？"

未来 v0.7+ 会引入 LLM 真"决策"，目前是规则引擎 + 阈值。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.L7.regulator")


@dataclass
class Adaptation:
    """A single self-regulation action taken."""
    timestamp: str
    trigger: str            # which L6 issue caused it
    action: str             # what L7 did
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "trigger": self.trigger,
            "action": self.action,
            "detail": self.detail,
        }


class SelfRegulator:
    """L7 — listens to L6.warn, adjusts anan's own knobs.

    Hooks (all optional — L7 only acts on what's wired):
      - working_memory: if provided, L7 can attenuate skewed layers' salience
      - circadian_loop: if provided, L7 can shorten sleep_threshold

    Usage:
        l7 = SelfRegulator(bus=bus, working_memory=wm, circadian=loop)
        await l7.attach()
        # ... lives, listens, adapts ...
        await l7.detach()
    """

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        working_memory=None,
        circadian=None,
        intent_stack=None,            # L8 IntentStack — to check avoid_ intents before acting
        # tunables
        salience_attenuation: float = 0.3,    # multiply skewed-layer salience by this
        min_sleep_threshold: float = 1.0,     # don't shorten below this
        threshold_step: float = 0.5,          # how much to shorten per intervention
        max_actions_per_warn: int = 3,        # don't go nuts on a single warn
        llm: Optional[Callable[..., Awaitable[str]]] = None,
    ):
        self._bus = bus or get_bus()
        self._wm = working_memory
        self._circadian = circadian
        self._intent_stack = intent_stack
        self._sal_atten = salience_attenuation
        self._min_thresh = min_sleep_threshold
        self._thresh_step = threshold_step
        self._max_actions = max_actions_per_warn
        self._unsub: Optional[Callable[[], None]] = None
        self._history: list[Adaptation] = []
        # Per-layer salience attenuation factor (1.0 = unchanged, <1 = damped)
        self._layer_atten: dict[str, float] = {}
        self._original_salience_fn = None
        self._llm = llm  # LLM for reasoning about unknown issues

    # ------------------------------------------------------------------
    async def attach(self) -> None:
        """Subscribe to L6.metacognition.warn (reactive) and L5.pattern.discovered (proactive)."""
        async def on_warn(event: Event):
            await self._react(event)
        async def on_pattern_discovered(event: Event):
            await self._on_causal_pattern(event)
        self._unsub = self._bus.subscribe("L6.metacognition.warn", on_warn)
        self._unsub_l5 = self._bus.subscribe("L5.pattern.discovered", on_pattern_discovered)
        self._unsub_goal_achieved = self._bus.subscribe("L7.goal.achieved", self._on_goal_achieved)
        self._unsub_goal_abandoned = self._bus.subscribe("L7.goal.abandoned", self._on_goal_abandoned)
        self._learned_risky_patterns = set()  # (antecedent, consequent) we already acted on

        # If WM is wired, swap in our wrapping salience_fn (preserves original)
        if self._wm is not None:
            self._original_salience_fn = self._wm.salience_fn

            def regulated_salience(ev: Event) -> float:
                base = self._original_salience_fn(ev)
                layer = ev.topic.split(".")[0]
                return base * self._layer_atten.get(layer, 1.0)

            self._wm.salience_fn = regulated_salience

    async def detach(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        if hasattr(self, '_unsub_l5') and self._unsub_l5:
            self._unsub_l5()
            self._unsub_l5 = None
        if hasattr(self, '_unsub_goal_achieved') and self._unsub_goal_achieved:
            self._unsub_goal_achieved()
            self._unsub_goal_achieved = None
        if hasattr(self, '_unsub_goal_abandoned') and self._unsub_goal_abandoned:
            self._unsub_goal_abandoned()
            self._unsub_goal_abandoned = None
        # Restore original salience fn so we don't leak state
        if self._wm is not None and self._original_salience_fn is not None:
            self._wm.salience_fn = self._original_salience_fn
            self._original_salience_fn = None

    async def _on_causal_pattern(self, event: Event) -> None:
        """L5 discovered a causal pattern — evaluate if we should act preemptively.

        Only acts on high-confidence patterns where:
        - consequent is a known negative event (L6.metacognition.warn, bus errors)
        - we haven't already acted on this pattern (avoid spam)
        """
        payload = event.payload
        antecedent = payload.get("antecedent", "")
        consequent = payload.get("consequent", "")
        confidence = payload.get("confidence", 0.0)
        lift = payload.get("lift", 1.0)

        # Skip if already acted, or confidence too low
        pattern_key = (antecedent, consequent)
        if pattern_key in self._learned_risky_patterns:
            return
        if confidence < 0.8 or lift < 2.0:
            return

        # Is consequent something bad we can prevent?
        is_bad = False
        action = None
        detail = {}

        # Pattern 1: X → L6.metacognition.* (X causes metacognition warnings)
        if "L6.metacognition" in consequent:
            layer = antecedent.split(".")[0]
            if layer in ("L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9"):
                is_bad = True
                action = "attenuate_layer_salience"
                detail = {
                    "layer": layer,
                    "rationale": f"[proactive from L5 insight] {antecedent} → {consequent} (置信={confidence:.0%}, 提升={lift:.1f}x)",
                    "factor": self._sal_atten,
                }

        # Pattern 2: L8.intent.* → L4.observation.* (intent leads to observation/verification)
        # This means: every time we have an intent, we get observed/falsified
        # That's the "try harder → fail → try harder" loop — L7 should intervene
        elif "L8.intent" in antecedent and "L4.observation" in consequent:
            is_bad = True
            action = "weaken_intent"
            # The pattern tells us failure leads to reinforce, but not which specific intent
            # We'll emit a general weaken signal; L8 can decide which intent to weaken
            detail = {
                "rationale": f"[proactive from L5 insight] 发现『验证失败→意图加固』死循环，{antecedent} → {consequent} (置信={confidence:.0%}, 提升={lift:.1f}x) — 这是疯狂的定义，主动减弱",
                "confidence": confidence,
                "lift": lift,
            }

        if is_bad and action:
            if action == "attenuate_layer_salience" and self._wm is not None:
                self._learned_risky_patterns.add(pattern_key)
                await self._apply_layer_attenuation(detail["layer"], detail["factor"], detail["rationale"])
            elif action == "weaken_intent":
                self._learned_risky_patterns.add(pattern_key)
                # Publish intent weaken signal for L8 to consume
                await self._bus.publish(Event(
                    topic="L7.regulator.weaken_intent",
                    source="L7",
                    payload=detail,
                ))
            await self._record_and_emit(
                trigger=f"L5 insight: {antecedent} → {consequent}",
                action=action,
                detail=detail,
            )

    # ------------------------------------------------------------------
    async def _react(self, warn_event: Event) -> None:
        issues: list[str] = warn_event.payload.get("issues", [])
        actions_taken = 0
        for issue in issues:
            if actions_taken >= self._max_actions:
                break
            if "错误率" in issue and "严重" in issue:
                await self._heal_bus(issue)
                actions_taken += 1
            elif "注意力倾斜" in issue:
                await self._rebalance_attention(issue)
                actions_taken += 1
            elif "身份" in issue and ("停滞" in issue or "没增长" in issue):
                await self._stir_identity(issue)
                actions_taken += 1
            elif "我是谁" in issue:
                await self._stir_identity(issue)
                actions_taken += 1
            else:
                # LLM 推理未知 issue 的应对策略
                handled = await self._react_unknown(issue)
                if handled:
                    actions_taken += 1

    async def _heal_bus(self, trigger: str) -> None:
        """High bus error rate — flag intent, can't fix without knowing the offender."""
        action = "emit_heal_intent"
        detail = {"intent": "请上层定位并 detach 抛错的 handler"}
        await self._record_and_emit(trigger, action, detail)
        # 真正发布 heal_bus 事件，让上层有机会处理
        await self._bus.publish(Event(
            topic="L7.intent.heal_bus",
            source="L7.regulator",
            payload={"trigger": trigger, "detail": detail},
        ))

    async def _react_unknown(self, issue: str) -> bool:
        """Use LLM to decide how to handle an unrecognized issue.

        Returns True if LLM suggested an action that was taken.
        Falls back to False (no action) if no LLM is configured.
        """
        if not self._llm:
            return False

        # Build context: recent adaptations + current layer attenuation state
        recent = [
            f"  {a.trigger}: {a.action}"
            for a in self._history[-5:]
        ]
        atten_state = ", ".join(
            f"{lyr}={fact:.2f}" for lyr, fact in self._layer_atten.items()
        ) if self._layer_atten else "（无）"

        prompt = f"""你是 anan 的 L7 意志调节器。给定一个未知的系统问题，
判断 anan 应该采取什么行动来应对。

已知的调节手段：
- heal_bus: 发布 heal_bus 事件请求上层清理错误
- rebalance_attention: 降低某一层的注意力权重（如果该层过载）
- stir_identity: 缩短睡眠阈值加快反思频率（如果身份停滞）
- apply_layer_attenuation: 手动调节某层衰减因子

当前系统状态：
- 未知问题：{issue}
- 最近动作：{chr(10).join(recent) if recent else "（无）"}
- 各层衰减状态：{atten_state}

请直接输出要采取的行动（只选一个），格式："action: <行动名>, reason: <原因>"。
不要输出其他内容。"""

        try:
            result = (await self._llm([{"role": "user", "content": prompt}])).strip()
            if not result:
                return False

            # Parse "action: X, reason: Y"
            action_name, reason = result, ""
            if ":" in result:
                parts = result.split(":", 1)
                action_name = parts[0].strip().lower()
                reason = parts[1].strip() if len(parts) > 1 else ""

            # Map LLM response to actual action
            if action_name == "heal_bus":
                await self._heal_bus(f"[LLM] {issue}: {reason}")
                return True
            elif action_name == "stir_identity":
                await self._stir_identity(f"[LLM] {issue}: {reason}")
                return True
            elif "rebalance" in action_name or "attenuation" in action_name:
                layer = self._extract_layer(issue) or self._extract_layer(reason) or "L5"
                await self._apply_layer_attenuation(layer, 0.5, f"[LLM] {issue}: {reason}")
                return True
            else:
                # Fallback: emit as a new intent for higher layers to handle
                await self._bus.publish(Event(
                    topic="L7.intent.llm_unknown_issue",
                    source="L7.regulator",
                    payload={"issue": issue, "llm_response": result},
                ))
                await self._record_and_emit(f"[LLM] {issue}", "llm_delegated", {"response": result})
                return True

        except Exception as exc:
            logger.warning("LLM unknown-issue reasoning failed: %s", exc)
            return False

    async def _rebalance_attention(self, trigger: str) -> None:
        """Skewed attention — attenuate the dominant layer's salience."""
        if self._wm is None:
            return
        # Parse "注意力倾斜：L9 层占了 90%" — pull the layer name out
        layer = self._extract_layer(trigger)
        if not layer:
            return
        await self._apply_layer_attenuation(layer, self._sal_atten, trigger)

    async def _apply_layer_attenuation(self, layer: str, factor: float, trigger: str = "proactive") -> None:
        """Apply salience attenuation to a layer, with floor."""
        if self._wm is None:
            return
        # Check if L8 has flagged this action as harmful — respect anan's own learning
        if self._intent_stack is not None:
            avoid_key = f"avoid_attenuate_layer_salience"
            avoid_intent = self._intent_stack.get(avoid_key)
            if avoid_intent is not None and avoid_intent.strength > 0.2:
                logger.debug(
                    "L7 skipped attenuate_layer_salience: L8 flagged it as harmful "
                    "(strength=%.2f). L7 respects L5's evidence.",
                    avoid_intent.strength,
                )
                await self._record_and_emit(
                    trigger=f"L8 avoid intent active ({trigger})",
                    action="noop_respect_avoid",
                    detail={"reason": f"L8 判定有害，跳过: {avoid_key}", "layer": layer},
                )
                return
        new_atten = self._layer_atten.get(layer, 1.0) * factor
        # Floor it so we don't kill a layer entirely
        new_atten = max(new_atten, 0.05)
        self._layer_atten[layer] = new_atten
        action = "attenuate_layer_salience"
        detail = {
            "layer": layer,
            "factor": round(new_atten, 4),
            "rationale": f"L6 说 {layer} 在 WM 占主导，降权让别的层有机会" if trigger.startswith("注意力倾斜")
                        else trigger,
        }
        await self._record_and_emit(trigger, action, detail)

    async def _stir_identity(self, trigger: str) -> None:
        """Identity stagnation — shorten sleep_threshold so cycles fire faster
        → more sleep cycles → more reflect_deep chances → more chances to grow."""
        if self._circadian is None:
            await self._record_and_emit(trigger, "noop", {
                "reason": "no circadian wired — can't change sleep threshold",
            })
            return
        cur = self._circadian.config.sleep_threshold
        new = max(cur - self._thresh_step, self._min_thresh)
        if new == cur:
            await self._record_and_emit(trigger, "noop", {
                "reason": f"sleep_threshold already at floor {self._min_thresh}",
            })
            return
        self._circadian.config.sleep_threshold = new
        action = "shorten_sleep_threshold"
        detail = {
            "from": cur,
            "to": new,
            "rationale": "身份停滞 → 让心跳更频繁触发睡眠反思",
        }
        await self._record_and_emit(trigger, action, detail)

    @staticmethod
    def _extract_layer(text: str) -> Optional[str]:
        # Look for things that look like "L0", "L1", ..., "L9"
        import re
        m = re.search(r"\bL\d+\b", text)
        return m.group(0) if m else None

    async def _record_and_emit(
        self, trigger: str, action: str, detail: dict[str, Any],
    ) -> None:
        adaptation = Adaptation(
            timestamp=datetime.now().isoformat(),
            trigger=trigger,
            action=action,
            detail=detail,
        )
        self._history.append(adaptation)
        try:
            await self._bus.publish(Event(
                topic="L7.regulator.acted",
                source="L7.regulator",
                payload=adaptation.to_dict(),
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("L7 emit failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    def history(self) -> list[Adaptation]:
        return list(self._history)

    def latest(self) -> Optional[Adaptation]:
        return self._history[-1] if self._history else None

    def stats(self) -> dict:
        from collections import Counter
        actions = Counter(a.action for a in self._history)
        return {
            "total_adaptations": len(self._history),
            "by_action": dict(actions),
            "layer_attenuations": dict(self._layer_atten),
        }

    # ------------------------------------------------------------------
    # L7 Goals listener — 目标达成/放弃 → 记录到 adaptation history
    # ------------------------------------------------------------------
    async def _on_goal_achieved(self, event: Event) -> None:
        """L7.goal.achieved → 记录一次成功的自我调节，增强下次信心。"""
        payload = event.payload or {}
        goal_id = payload.get("goal_id", "unknown")
        goal_text = payload.get("goal_text", "")
        self._history.append(
            Adaptation(
                timestamp=datetime.now().isoformat(),
                trigger="L7.goal.achieved",
                action="goal_achieved",
                detail={"goal_id": goal_id, "goal_text": goal_text},
            )
        )
        logger.info(f"[L7] 目标达成记录: {goal_text[:50]}")

    async def _on_goal_abandoned(self, event: Event) -> None:
        """L7.goal.abandoned → 记录放弃，下次同类目标降低优先级。"""
        payload = event.payload or {}
        goal_id = payload.get("goal_id", "unknown")
        goal_text = payload.get("goal_text", "")
        reason = payload.get("reason", "unknown")
        self._history.append(
            Adaptation(
                timestamp=datetime.now().isoformat(),
                trigger="L7.goal.abandoned",
                action="goal_abandoned",
                detail={"goal_id": goal_id, "goal_text": goal_text, "reason": reason},
            )
        )
        logger.info(f"[L7] 目标放弃记录: {goal_text[:50]}, reason={reason}")
