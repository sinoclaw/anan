"""
IntentMonitoringAdvisor — L8 Intent Stack 的智能决策 advisor
================================================================

职责：在 IntentStack 收到外部事件（来自 L6/L7/L5）时，判断：
  1. 是否要创建 / 强化 / 放弃某个 intent
  2. intent 的 strength 应该是多少
  3. stack 整体健康状况，是否需要调整 decay_rate / capacity

与 DrivePriorityAdvisor 的区别：
  - DrivePriorityAdvisor：决定 L7/L8 drive 的优先级排序
  - IntentMonitoringAdvisor：决定从 L6/L7/L5 收到的事件是否升格为持久意图

集成点（IntentStack 内部）：
  _learn_from_l6(event)   → 接管 _issue_to_key() / _issue_to_want() 的硬编码映射
  _learn_from_l7(event)   → 接管 action→intent 的静态规则
  _on_action_effect(evt)  → 接管 avg_delta / samples 硬编码阈值
  _on_pattern_discov(evt) → 接管 confidence/lift 硬编码阈值
  _weaken_most_failing()  → 接管 strength*=0.5 硬编码衰减

用法：
  advisor = IntentMonitoringAdvisor()
  advisor.set_delegate(delegate_fn)          # 注入 runtime handle
  decision = await advisor.evaluate(context) # LLM 判断
  # fallback: return advisor.fallback_evaluate(context)  # 规则兜底
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger("anan.L8.intent_monitoring_advisor")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class IntentContext:
    """一次完整的 intent 决策上下文 — 由 IntentStack 收集后传给 advisor."""
    source_layer: str                    # "L5" / "L6" / "L7"
    event_type: str                     # "action_effect" / "pattern_discovered" / "l6_report" / "l7_acted"
    # L6 report fields
    raw_issue: Optional[str] = None
    # L7 acted fields
    action: Optional[str] = None
    action_detail: Optional[dict] = None
    # L5 action_effect fields
    avg_delta: float = 0.0
    samples: int = 0
    # L5 pattern_discovered fields
    antecedent: Optional[str] = None
    consequent: Optional[str] = None
    confidence: float = 0.0
    lift: float = 1.0
    # L5 causal pattern fields (for _weaken_most_failing)
    failing_intent_key: Optional[str] = None
    failing_intent_strength: float = 0.0
    failing_intent_reinforce_count: int = 0
    # Stack health (always provided)
    stack_size: int = 0
    capacity: int = 7
    decay_rate: float = 0.92
    top_intents: list[dict] = field(default_factory=list)
    abandoned_recently: int = 0
    # Extra metadata
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class IntentDecision:
    """Advisor 返回的决策结果."""
    decision: str                    # "propose" / "reinforce" / "weaken" / "abandon" / "skip"
    key: Optional[str] = None         # intent key（propose/reinforce 时需要）
    description: Optional[str] = None  # intent description（propose 时需要）
    strength: float = 0.0            # 0..1，建议的 initial / new strength
    detail: dict = field(default_factory=dict)   # 传给 intent detail 字段
    reasoning: str = ""              # LLM 判断理由（供日志/调试用）
    stack_action: Optional[str] = None  # "tighten_decay" / "loosen_decay" / "increase_capacity" — 栈级调整建议

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "key": self.key,
            "description": self.description,
            "strength": round(self.strength, 4),
            "detail": self.detail,
            "reasoning": self.reasoning,
            "stack_action": self.stack_action,
        }


# ---------------------------------------------------------------------------
# IntentMonitoringAdvisor
# ---------------------------------------------------------------------------

class IntentMonitoringAdvisor:
    """L8 Intent Stack 的智能决策大脑 — 用 LLM 判断是否/如何干预意图栈.

    接入点：
      1. _learn_from_l6()   — 判断 L6 report 是否值得成为 intent
      2. _learn_from_l7()   — 判断 L7 action 是否值得成为 intent
      3. _on_action_effect() — 判断 L5 评估结果是否值得强化 intent
      4. _on_pattern_discovered() — 判断 L5 pattern 是否值得成为 intent
      5. _weaken_most_failing() — 判断如何削弱失败的 intent

    Stack health 监控：
      - decay_rate 调整（强度衰减是否过快/过慢）
      - capacity 建议（栈是否过于拥挤/空旷）
    """

    def __init__(
        self,
        *,
        enable_stack_health: bool = True,
        min_samples_for_effect: int = 2,
        min_confidence_for_pattern: float = 0.6,
        min_lift_for_pattern: float = 1.5,
        min_delta_for_effect: float = 0.03,
    ):
        self._delegate_fn: Optional[Callable[..., any]] = None
        self._enable_stack_health = enable_stack_health
        # Fallback threshold constants (used in fallback_evaluate)
        self._min_samples = min_samples_for_effect
        self._min_confidence = min_confidence_for_pattern
        self._min_lift = min_lift_for_pattern
        self._min_delta = min_delta_for_effect

    # ------------------------------------------------------------------ Public API

    def set_delegate(self, fn: Callable[..., any]) -> None:
        """MindStackRunner 注入 runtime handle 的 delegate 函数."""
        self._delegate_fn = fn

    async def evaluate(self, ctx: IntentContext) -> IntentDecision:
        """主要入口 — 用 LLM 判断如何决策（需要 delegate_fn）."""
        if self._delegate_fn is None:
            logger.warning("IntentMonitoringAdvisor: no delegate_fn, using fallback")
            return self.fallback_evaluate(ctx)

        try:
            result = await self._delegate_fn(
                task="intent_monitoring",
                messages=[{
                    "role": "user",
                    "content": self._build_prompt(ctx),
                }],
            )
            return self._parse_result(result, ctx)
        except Exception as exc:
            logger.debug("IntentMonitoringAdvisor LLM call failed: %s", exc)
            return self.fallback_evaluate(ctx)

    def fallback_evaluate(self, ctx: IntentContext) -> IntentDecision:
        """规则兜底 — 无 LLM 时用的确定性逻辑."""
        if ctx.event_type == "l6_report":
            return self._fallback_l6(ctx)
        elif ctx.event_type == "l7_acted":
            return self._fallback_l7(ctx)
        elif ctx.event_type == "action_effect":
            return self._fallback_action_effect(ctx)
        elif ctx.event_type == "pattern_discovered":
            return self._fallback_pattern_discovered(ctx)
        elif ctx.event_type == "weaken_failing":
            return self._fallback_weaken(ctx)
        else:
            return IntentDecision(decision="skip", reasoning="unknown event_type")

    # ------------------------------------------------------------------ LLM helpers

    def _build_prompt(self, ctx: IntentContext) -> str:
        top = ctx.top_intents[:5]
        top_str = "\n".join(
            f"  - {t['key']} (strength={t.get('strength', 0):.2f}, "
            f"reinforced={t.get('reinforce_count', 0)}×)"
            for t in top
        ) or "  (empty)"

        if ctx.event_type == "l6_report":
            issue = ctx.raw_issue or ""
            return (
                f"Anan (AI cognitive system) 的 L8 Intent Stack 收到 L6 Metacognition 报告。\n"
                f"\n"
                f"L6 问题: {issue}\n"
                f"当前栈状态: {ctx.stack_size}/{ctx.capacity} intents\n"
                f"Top intents:\n{top_str}\n"
                f"\n"
                f"任务：判断是否创建一个新的 persistent intent。\n"
                f"1. 如果这个问题是系统性的（反复出现/影响多个层），创建 intent（key 需语义相关，description 要自然语言）\n"
                f"2. 如果这个问题已有相关 intent 在栈上，强化它\n"
                f"3. 如果栈已满（>={ctx.capacity}），考虑用 intent 的重要性替代现有 weakest\n"
                f"4. 如果问题无关紧要（偶发/低影响），skip\n"
                f"\n"
                f"返回 JSON（无 markdown）：\n"
                f'{{"decision": "propose|reinforce|skip", "key": "...", '
                f'"description": "...", "strength": 0.3-0.7, "reasoning": "..."}}'
            )

        elif ctx.event_type == "l7_acted":
            action = ctx.action or ""
            detail = ctx.action_detail or {}
            return (
                f"Anan 的 L8 Intent Stack 收到 L7 Regulator 的 action 报告。\n"
                f"\n"
                f"L7 Action: {action}\n"
                f"Action detail: {detail}\n"
                f"当前栈状态: {ctx.stack_size}/{ctx.capacity}\n"
                f"Top intents:\n{top_str}\n"
                f"\n"
                f"任务：判断是否把这个 action 升格为 persistent intent（让 anan 持续记住这件事）。\n"
                f"规则：\n"
                f"  - heal_bus / rebalance → 通常值得持有关注（系统健康）\n"
                f"  - attenuate_layer_salience → 值得（注意力均衡）\n"
                f"  - shorten_sleep_threshold → 值得（身份生长）\n"
                f"  - inject_energy / shutdown → 视上下文决定\n"
                f"  - 重复 action（detail.repeated>3）→ 降权或 weaken\n"
                f"\n"
                f"返回 JSON（无 markdown）：\n"
                f'{{"decision": "propose|reinforce|skip", "key": "...", '
                f'"description": "...", "strength": 0.3-0.7, "reasoning": "..."}}'
            )

        elif ctx.event_type == "action_effect":
            return (
                f"Anan 的 L8 Intent Stack 收到 L5 CausalReasoner 的 action 效果评估。\n"
                f"\n"
                f"Action: {ctx.action or '?'}\n"
                f"avg_delta (health 变化): {ctx.avg_delta:+.3f}\n"
                f"samples: {ctx.samples}\n"
                f"当前栈状态: {ctx.stack_size}/{ctx.capacity}\n"
                f"\n"
                f"任务：\n"
                f"  - avg_delta > 0 且 samples >= {self._min_samples} → propose intent 让 anan 继续这个 action\n"
                f"  - avg_delta < 0 且 samples >= {self._min_samples} → 建议 avoid intent（低强度）\n"
                f"  - samples < {self._min_samples} → skip（样本不足）\n"
                f"  - |avg_delta| < {self._min_delta} → skip（效果微弱）\n"
                f"\n"
                f"返回 JSON（无 markdown）：\n"
                f'{{"decision": "propose|avoid|skip", "key": "...", '
                f'"description": "...", "strength": 0.2-0.6, "reasoning": "..."}}'
            )

        elif ctx.event_type == "pattern_discovered":
            return (
                f"Anan 的 L8 Intent Stack 收到 L5 PatternMiner 的规则发现通知。\n"
                f"\n"
                f"规则: {ctx.antecedent} → {ctx.consequent}\n"
                f"confidence: {ctx.confidence:.2f}  lift: {ctx.lift:.2f}\n"
                f"当前栈状态: {ctx.stack_size}/{ctx.capacity}\n"
                f"\n"
                f"任务：判断是否把这个规则升格为 persistent intent（让 anan 持续追求这个结果）。\n"
                f"高置信+高提升（conf>={self._min_confidence}, lift>={self._min_lift}）+ 有益的 consequent → propose\n"
                f"否则 → skip\n"
                f"\n"
                f"返回 JSON（无 markdown）：\n"
                f'{{"decision": "propose|skip", "key": "...", '
                f'"description": "...", "strength": 0.3-0.5, "reasoning": "..."}}'
            )

        elif ctx.event_type == "weaken_failing":
            return (
                f"Anan 的 L8 Intent Stack 发现某个 intent 持续失败（reinforce 多次但效果为负）。\n"
                f"\n"
                f"Target intent: {ctx.failing_intent_key}\n"
                f"Current strength: {ctx.failing_intent_strength:.3f}\n"
                f"Reinforce count: {ctx.failing_intent_reinforce_count}\n"
                f"Current decay_rate: {ctx.decay_rate:.2f}\n"
                f"Stack size: {ctx.stack_size}/{ctx.capacity}\n"
                f"\n"
                f"任务：\n"
                f"  - 如果 reinforce_count >= 3 且 strength > 0.1 → weaken（减半或更多）\n"
                f"  - 如果 strength <= 0.1 → abandon（直接放弃）\n"
                f"  - 如果 reinforce_count < 3 → skip（给一次机会）\n"
                f"\n"
                f"返回 JSON（无 markdown）：\n"
                f'{{"decision": "weaken|abandon|skip", '
                f'"strength_multiplier": 0.0-0.9, '
                f'"reasoning": "..."}}'
            )

        else:
            return (
                f"Unknown event type: {ctx.event_type}\n"
                f"Context: {ctx}\n"
                f'{"decision": "skip"}'
            )

    def _parse_result(self, raw: any, ctx: IntentContext) -> IntentDecision:
        """解析 delegate_fn 返回的 LLM 结果."""
        try:
            text = raw if isinstance(raw, str) else str(raw)
            import json, re
            # Extract JSON object
            m = re.search(r'\{[^{}]*\}', text)
            if not m:
                return self.fallback_evaluate(ctx)
            obj = json.loads(m.group())

            decision = obj.get("decision", "skip")

            if decision in ("propose", "reinforce"):
                return IntentDecision(
                    decision=decision,
                    key=obj.get("key"),
                    description=obj.get("description"),
                    strength=float(obj.get("strength", 0.4)),
                    detail=ctx.action_detail or {},
                    reasoning=obj.get("reasoning", ""),
                )
            elif decision == "avoid":
                return IntentDecision(
                    decision="propose",   # 复用 propose 分支创建 avoid intent
                    key=f"avoid_{ctx.action or ctx.antecedent or 'unknown'}",
                    description=obj.get("description", f"Avoid {ctx.action}"),
                    strength=float(obj.get("strength", 0.25)),
                    detail=ctx.action_detail or {},
                    reasoning=obj.get("reasoning", ""),
                )
            elif decision == "weaken":
                multiplier = float(obj.get("strength_multiplier", 0.5))
                new_strength = ctx.failing_intent_strength * multiplier
                return IntentDecision(
                    decision="weaken",
                    key=ctx.failing_intent_key,
                    strength=new_strength,
                    reasoning=obj.get("reasoning", ""),
                )
            elif decision == "abandon":
                return IntentDecision(
                    decision="abandon",
                    key=ctx.failing_intent_key,
                    reasoning=obj.get("reasoning", ""),
                )
            else:
                return IntentDecision(decision="skip", reasoning=obj.get("reasoning", ""))

        except Exception:
            return self.fallback_evaluate(ctx)

    # ------------------------------------------------------------------ Fallback rules

    def _fallback_l6(self, ctx: IntentContext) -> IntentDecision:
        issue = ctx.raw_issue or ""
        # Hardcoded mapping (preserved from original _issue_to_key)
        key_map = [
            ("注意力倾斜", "keep_attention_balanced", "保持注意力均衡, 不被某一层霸占"),
            ("身份", "grow_identity", "让身份持续生长, 不停滞"),
            ("错误率", "heal_bus", "保持事件总线健康"),
            ("我是谁", "know_myself", "了解自我"),
        ]
        for keyword, key, desc in key_map:
            if keyword in issue:
                strength = 0.45 if ctx.stack_size < ctx.capacity else 0.3
                return IntentDecision(
                    decision="propose", key=key, description=desc,
                    strength=strength,
                    detail={"raw_issue": issue},
                    reasoning=f"fallback: keyword '{keyword}' matched",
                )
        # Unrecognized issue — still propose a generic intent (preserve original behavior)
        # Original would create: key=None, description=f"应对: {issue[:30]}"
        # We create a stable key from the raw issue
        safe_key = "handle_" + issue.replace(" ", "_")[:20].strip() or "unknown_issue"
        safe_key = "".join(c if c.isalnum() else "_" for c in safe_key)
        strength = 0.3 if ctx.stack_size < ctx.capacity else 0.2
        return IntentDecision(
            decision="propose",
            key=safe_key,
            description=f"应对: {issue[:40]}",
            strength=strength,
            detail={"raw_issue": issue, "unmatched_keyword": True},
            reasoning=f"fallback: no keyword match, creating generic intent for '{issue[:20]}'",
        )

    def _fallback_l7(self, ctx: IntentContext) -> IntentDecision:
        action = ctx.action or ""
        detail = ctx.action_detail or {}
        key_map = {
            "attenuate_layer_salience": ("keep_attention_balanced",
                "保持注意力均衡", 0.5),
            "shorten_sleep_threshold": ("grow_identity",
                "让身份持续生长", 0.45),
            "emit_heal_intent": ("heal_bus",
                "修复事件总线错误源", 0.55),
            "heal_bus": ("heal_bus",
                "修复事件总线", 0.55),
            "rebalance": ("keep_balance",
                "保持系统平衡", 0.4),
            "inject_energy": ("maintain_energy",
                "维持能量水平", 0.35),
            "shutdown": ("safe_shutdown",
                "安全关闭", 0.3),
        }
        if action in key_map:
            key, desc, base_strength = key_map[action]
            repeated = detail.get("repeated", 0)
            strength = base_strength if repeated < 3 else base_strength * 0.6
            return IntentDecision(
                decision="propose", key=key, description=desc,
                strength=strength,
                detail=detail,
                reasoning=f"fallback: action '{action}' mapped to intent",
            )
        return IntentDecision(
            decision="skip", reasoning=f"fallback: action '{action}' not in key_map"
        )

    def _fallback_action_effect(self, ctx: IntentContext) -> IntentDecision:
        if ctx.samples < self._min_samples:
            return IntentDecision(
                decision="skip",
                reasoning=f"fallback: samples {ctx.samples} < {self._min_samples}",
            )
        if abs(ctx.avg_delta) < self._min_delta:
            return IntentDecision(
                decision="skip",
                reasoning=f"fallback: |delta| {ctx.avg_delta:.3f} < {self._min_delta}",
            )
        action = ctx.action or "unknown"
        if ctx.avg_delta > 0:
            return IntentDecision(
                decision="propose",
                key=f"keep_doing_{action}",
                description=f"继续 {action}（已证明平均提升 +{ctx.avg_delta:.3f}）",
                strength=min(0.3 + ctx.avg_delta * 2, 0.7),
                detail={"action": action, "avg_delta": ctx.avg_delta, "samples": ctx.samples},
                reasoning=f"fallback: positive effect avg_delta={ctx.avg_delta:.3f}",
            )
        else:
            return IntentDecision(
                decision="propose",
                key=f"avoid_{action}",
                description=f"避免 {action}（已证明平均降低 {-ctx.avg_delta:.3f}）",
                strength=min(0.25 + abs(ctx.avg_delta), 0.4),
                detail={"action": action, "avg_delta": ctx.avg_delta, "samples": ctx.samples},
                reasoning=f"fallback: negative effect avg_delta={ctx.avg_delta:.3f}",
            )

    def _fallback_pattern_discovered(self, ctx: IntentContext) -> IntentDecision:
        if ctx.confidence < self._min_confidence or ctx.lift < self._min_lift:
            return IntentDecision(
                decision="skip",
                reasoning=f"fallback: conf={ctx.confidence:.2f}<{self._min_confidence} or lift={ctx.lift:.2f}<{self._min_lift}",
            )
        if not ctx.antecedent or not ctx.consequent:
            return IntentDecision(decision="skip", reasoning="fallback: missing antecedent/consequent")
        key = f"keep_triggering_{ctx.consequent.replace('.', '_')}"
        description = (
            f"保持触发 {ctx.consequent}（规则 {ctx.antecedent}→{ctx.consequent} "
            f"置信{ctx.confidence:.0%}，提升{ctx.lift:.1f}x）"
        )
        return IntentDecision(
            decision="propose", key=key, description=description,
            strength=0.35,
            detail={"antecedent": ctx.antecedent, "consequent": ctx.consequent,
                    "confidence": ctx.confidence, "lift": ctx.lift},
            reasoning=f"fallback: strong pattern conf={ctx.confidence:.2f} lift={ctx.lift:.2f}",
        )

    def _fallback_weaken(self, ctx: IntentContext) -> IntentDecision:
        # Original: only skip if reinforce_count < 2
        # At reinforce_count >= 2, always act (weaken or abandon based on strength)
        if ctx.failing_intent_reinforce_count < 2:
            return IntentDecision(
                decision="skip",
                reasoning=f"fallback: reinforce_count {ctx.failing_intent_reinforce_count} < 2",
            )
        if ctx.failing_intent_strength <= 0.1:
            return IntentDecision(
                decision="abandon",
                key=ctx.failing_intent_key,
                reasoning="fallback: strength <= 0.1, direct abandon",
            )
        # Halve — "insanity is doing the same thing expecting different results"
        new_strength = ctx.failing_intent_strength * 0.5
        return IntentDecision(
            decision="weaken",
            key=ctx.failing_intent_key,
            strength=new_strength,
            reasoning=f"fallback: halve strength {ctx.failing_intent_strength:.3f} → {new_strength:.3f} (reinforce_count={ctx.failing_intent_reinforce_count})",
        )
