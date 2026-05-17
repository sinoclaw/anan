"""
L7 Will — Drive Strength Advisor (Subagent)
===========================================
评估驱动抑制强度：给定 L6.warn issues + adaptation history，判断：
1.该不该行动（avoid 校验）2.用什么行动 3.强度多大

设计原则：
- Handler: SelfRegulator 管状态，adaptation history，执行具体 action
- Subagent: 给定 issue context，评估该不该做、做什么、力度多大

为什么需要这个：
- _react() 的 if-else 硬编码无法处理组合型 issue（如"注意力倾斜+身份停滞"同时出现）
- 静态 conf/lift 阈值不适应系统当前状态
- subagent 可以根据 history 判断"之前试过这个方法没有、效果如何"

数据流：
  L6.metacognition.warn → SelfRegulator._react()
    → DriveStrengthAdvisor 评估 → action decision
    → SelfRegulator._execute() 执行
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("anan.L7.will.advisor")

# ---------------------------------------------------------------------------
# Drive Strength Decision
# ---------------------------------------------------------------------------

@dataclass
class DriveDecision:
    action: str              # "heal_bus" | "rebalance_attention" | "stir_identity" | "noop"
    strength: float          # 0.0-1.0，力度
    reasoning: str           # 判断理由
    suppress_other_drives: bool = False  # 是否同时压制其他 drives

    # 已知的 action 列表（subagent 必须从中选择）
    VALID_ACTIONS = frozenset([
        "heal_bus",
        "rebalance_attention",
        "stir_identity",
        "apply_layer_attenuation",
        "noop",
    ])

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "strength": round(self.strength, 3),
            "reasoning": self.reasoning,
            "suppress_other_drives": self.suppress_other_drives,
        }


@dataclass
class CausalPatternDecision:
    """L5 因果规则评估决策 — 是否根据 L5 发现的因果模式主动干预."""
    should_act: bool          # 是否行动
    action: Optional[str] = None  # 行动类型
    detail: dict = None      # 行动参数
    reasoning: str = ""       # 判断理由

    def __post_init__(self):
        if self.detail is None:
            self.detail = {}

    def to_dict(self) -> dict:
        return {
            "should_act": self.should_act,
            "action": self.action,
            "detail": self.detail,
            "reasoning": self.reasoning,
        }


# ---------------------------------------------------------------------------
# Subagent prompt
# ---------------------------------------------------------------------------

DRIVE_EVALUATION_PROMPT = """你是 anan 的 L7 意志调节器。给定系统问题，判断 anan 应该采取什么行动，以及力度多大。

## 已知可用调节手段
- heal_bus: 发布 heal_bus 事件请求上层清理错误（用于总线错误）
- rebalance_attention: 降低某一层的注意力权重（用于注意力倾斜）
- stir_identity: 缩短睡眠阈值加快反思频率（用于身份停滞）
- apply_layer_attenuation: 手动调节某层衰减因子（通用）
- noop: 不行动（问题不严重或已有 avoid 标记）

## 当前系统状态
L6 WARN ISSUES:
{issues}

L6 HEALTH SCORE: {health_score}

## 近期 adaptation history（最近5次）
ADAPTATION_HISTORY:
{adaptation_history}

## 各层衰减因子状态
LAYER_ATTENUATIONS:
{layer_attenuations}

## L8 Intent Stack（avoid signals）
AVOID_SIGNALS:
{avoid_signals}

## 决策标准
1. 已有 avoid 标记的 action → 跳过
2. 严重 issue（health_score < 0.3）→ 高强度 action
3. 同一 issue 反复出现 → 换一种 action 或 noop
4. 注意力倾斜 + 身份停滞同时出现 → 同时执行两个 action（suppress_other_drives=false）
5. 之前试过的 action 这次换一种（避免路径依赖）

## 输出格式（严格 JSON）
{{
  "action": "行动名（必须来自上述列表）",
  "strength": 0.0-1.0的浮点数,
  "reasoning": "判断理由（1-3句）",
  "suppress_other_drives": true|false
}}"""


# ---------------------------------------------------------------------------
# Fallback handler
# ---------------------------------------------------------------------------

# 硬编码的 issue → action 映射（保留作 fallback）
_ISSUE_ACTION_MAP = [
    (["错误率", "严重"], "heal_bus", 0.9),
    (["错误率"], "heal_bus", 0.6),
    (["注意力倾斜"], "rebalance_attention", 0.7),
    (["身份", "停滞"], "stir_identity", 0.7),
    (["身份", "没增长"], "stir_identity", 0.6),
    (["我是谁"], "stir_identity", 0.5),
]


def fallback_decide(issues, health_score, adaptation_history, avoid_signals, layer_attenuations) -> DriveDecision:
    """Rule-based fallback when subagent is unavailable.

    Strategy:
    - Check avoid signals first (respect L5 learnings)
    - Match issues against hardcoded map
    - De-duplicate: don't repeat same action twice in a row unless forced
    """
    # Check avoid signals
    avoid_actions = set()
    for intent in avoid_signals:
        if intent.get("action") == "heal_bus":
            avoid_actions.add("heal_bus")
        elif intent.get("action") == "rebalance_attention":
            avoid_actions.add("rebalance_attention")
        elif intent.get("action") == "stir_identity":
            avoid_actions.add("stir_identity")

    # Check recent history for repeated actions
    recent_actions = [a.get("action") for a in adaptation_history[-3:]]

    # Match issues
    actions_to_take = []
    for issue_text in issues:
        for keywords, action, base_strength in _ISSUE_ACTION_MAP:
            if all(kw in issue_text for kw in keywords):
                if action not in avoid_actions:
                    # Lower strength if recently tried
                    if action in recent_actions:
                        strength = base_strength * 0.5
                    else:
                        strength = base_strength
                    actions_to_take.append((action, strength, issue_text))

    if not actions_to_take:
        return DriveDecision(
            action="noop",
            strength=0.0,
            reasoning="Fallback: 无匹配 action 或全部被 avoid 标记",
        )

    # Pick highest-strength action (unless suppressed)
    best = max(actions_to_take, key=lambda x: x[1])
    return DriveDecision(
        action=best[0],
        strength=best[1],
        reasoning=f"Fallback: 关键词匹配 → {best[2][:30]}",
    )


# ---------------------------------------------------------------------------
# Drive Strength Advisor
# ---------------------------------------------------------------------------

class DriveStrengthAdvisor:
    """Subagent for evaluating drive/adaptation decisions.

    Usage:
        advisor = DriveStrengthAdvisor(delegate_fn=delegate_task)
        decision = await advisor.decide(
            issues=["注意力倾斜：L5 层占了 70%"],
            health_score=0.5,
            adaptation_history=[],
            avoid_signals=[],
            layer_attenuations={},
        )
    """

    def __init__(
        self,
        delegate_fn: Optional[callable] = None,
        adaptation_history: Optional[list] = None,
    ):
        self._delegate_fn = delegate_fn
        self._adaptation_history = adaptation_history or []

    def set_delegate(self, fn: callable) -> None:
        self._delegate_fn = fn

    async def decide(
        self,
        issues: list[str],
        health_score: float = 0.6,
        adaptation_history: Optional[list] = None,
        avoid_signals: Optional[list] = None,
        layer_attenuations: Optional[dict] = None,
    ) -> DriveDecision:
        """Evaluate what action to take given current issues + context."""
        history = adaptation_history or self._adaptation_history
        avoids = avoid_signals or []
        attens = layer_attenuations or {}

        # Build history text
        history_lines = []
        for a in history[-5:]:
            ts = a.get("timestamp", "?")
            trig = a.get("trigger", "?")
            act = a.get("action", "?")
            history_lines.append(f"  [{ts}] {act} ← {trig[:40]}")
        history_text = "\n".join(history_lines) or "  （无历史）"

        # Build avoid signals text
        avoid_text = "\n".join(
            f"  {a.get('intent', '?')}: strength={a.get('strength', 0):.2f}"
            for a in avoids
        ) or "  （无 avoid 信号）"

        # Build atten text
        atten_text = ", ".join(
            f"{lyr}={fact:.2f}" for lyr, fact in attens.items()
        ) or "  （无衰减）"

        issues_text = "\n".join(f"  - {issue}" for issue in issues) if issues else "  （无新 issue）"

        prompt = DRIVE_EVALUATION_PROMPT.format(
            issues=issues_text,
            health_score=f"{health_score:.2f}",
            adaptation_history=history_text,
            layer_attenuations=atten_text,
            avoid_signals=avoid_text,
        )

        if not self._delegate_fn:
            logger.debug("DriveStrengthAdvisor: no delegate_fn, using fallback")
            return fallback_decide(issues, health_score, history, avoids, attens)

        try:
            result_text = await self._delegate_fn(
                goal="task 评估",
                context=prompt,
                parent_agent=None,
            )
            parsed = self._parse_response(result_text)
            logger.info(
                "DriveStrengthAdvisor: issues=%d → action=%s strength=%.2f",
                len(issues), parsed.action, parsed.strength,
            )
            return parsed
        except Exception as exc:
            logger.warning(
                "DriveStrengthAdvisor subagent failed: %s, falling back", exc,
            )
            return fallback_decide(issues, health_score, history, avoids, attens)

    @staticmethod
    def _parse_response(text: str) -> DriveDecision:
        """Parse subagent text response into DriveDecision."""
        # Strategy 1: ```json ... ```
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                return DriveStrengthAdvisor._from_data(data)
            except json.JSONDecodeError:
                pass

        # Strategy 2: raw {...}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                return DriveStrengthAdvisor._from_data(data)
            except json.JSONDecodeError:
                pass

        logger.warning("DriveStrengthAdvisor: could not parse: %s", text[:200])
        return DriveDecision(
            action="noop",
            strength=0.0,
            reasoning="解析失败，无行动",
        )

    @staticmethod
    def _from_data(data: dict) -> DriveDecision:
        raw_action = data.get("action", "noop")
        # Normalize
        if raw_action not in DriveDecision.VALID_ACTIONS:
            # Try partial match
            for valid in DriveDecision.VALID_ACTIONS:
                if valid in raw_action.lower():
                    raw_action = valid
                    break
            else:
                raw_action = "noop"

        return DriveDecision(
            action=raw_action,
            strength=max(0.0, min(1.0, float(data.get("strength", 0.5)))),
            reasoning=data.get("reasoning", ""),
            suppress_other_drives=bool(data.get("suppress_other_drives", False)),
        )

    # -------------------------------------------------------------------------
    # L5 因果模式评估 — 接管 SelfRegulator._on_causal_pattern() 的硬编码阈值
    # -------------------------------------------------------------------------

    CAUSAL_PATTERN_PROMPT = """你是 anan 的 L7 主动干预决策器。给定 L5 因果reasoner 发现的因果规则，判断 anan 是否应该主动干预。

## L5 发现的因果规则
Antecedent: {antecedent}
Consequent: {consequent}
Confidence: {confidence:.2%}  (0-1，越高说明历史数据越支持这条规则)
Lift: {lift:.2f}  (>1.0 表示正相关，>2.0 表示强正相关)

## 当前栈状态
Stack size: {stack_size}/{capacity}

## 决策标准
1. confidence >= 0.8 且 lift >= 2.0 → 高置信规则，考虑行动
2. consequent 是 L6/metacognition 问题（如 L6.metacognition.warn）→ 主动预防有价值
3. antecedent → L8.intent.* 且 consequent → L4.observation.* → "验证失败→意图加固"死循环，必须干预
4. confidence 或 lift 太低（无历史数据支持）→ 跳过，避免噪声
5. 已经对这个 pattern 采取过行动 → 跳过（避免重复干预同一规则）

## 干预选项
- attenuate_layer_salience: 当 antecedent 导致 L6 问题时，降权 antecedent 层
- weaken_intent: 当发现意图→验证失败循环时，主动减弱意图
- noop: 不干预（规则不够强或不适用）

## 输出格式（严格 JSON）
{{
  "should_act": true|false,
  "action": "action_name 或 null",
  "detail": {{"layer": "...", "rationale": "...", "factor": 0.8, ...}} 或空字典,
  "reasoning": "判断理由（1-2句）"
}}"""

    async def decide_causal_pattern(
        self,
        antecedent: str,
        consequent: str,
        confidence: float,
        lift: float,
        stack_size: int = 0,
        capacity: int = 7,
    ) -> CausalPatternDecision:
        """评估 L5 因果模式是否值得 L7 主动干预。

        接管了 SelfRegulator._on_causal_pattern() 的硬编码阈值：
        旧: if confidence < 0.8 or lift < 2.0: return
        新: 由 subagent 根据上下文判断
        """
        prompt = self.CAUSAL_PATTERN_PROMPT.format(
            antecedent=antecedent,
            consequent=consequent,
            confidence=confidence,
            lift=lift,
            stack_size=stack_size,
            capacity=capacity,
        )

        if not self._delegate_fn:
            logger.debug("DriveStrengthAdvisor: no delegate_fn for causal pattern, using fallback")
            return self._fallback_causal_pattern(confidence, lift, antecedent, consequent)

        try:
            result_text = await self._delegate_fn(
                goal="因果模式评估",
                context=prompt,
                parent_agent=None,
            )
            parsed = self._parse_causal_response(result_text)
            logger.info(
                "DriveStrengthAdvisor: causal_pattern %s→%s conf=%.2f lift=%.1f → act=%s",
                antecedent[:30], consequent[:30], confidence, lift, parsed.should_act,
            )
            return parsed
        except Exception as exc:
            logger.warning("DriveStrengthAdvisor causal pattern subagent failed: %s, falling back", exc)
            return self._fallback_causal_pattern(confidence, lift, antecedent, consequent)

    @staticmethod
    def _fallback_causal_pattern(
        confidence: float,
        lift: float,
        antecedent: str = "",
        consequent: str = "",
    ) -> CausalPatternDecision:
        """规则兜底 — 原有的硬编码阈值逻辑 + action 映射."""
        if confidence < 0.8 or lift < 2.0:
            return CausalPatternDecision(
                should_act=False,
                reasoning=f"fallback: conf={confidence:.2f}<0.8 or lift={lift:.1f}<2.0",
            )

        # Pattern 1: X → L6.metacognition.* → attenuate that layer
        if "L6.metacognition" in consequent:
            layer = antecedent.split(".")[0] if antecedent else None
            if layer in ("L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9"):
                return CausalPatternDecision(
                    should_act=True,
                    action="attenuate_layer_salience",
                    detail={"layer": layer, "factor": 0.3},
                    reasoning="fallback: X→L6.metacognition pattern, attenuate layer",
                )

        # Pattern 2: L8.intent.* → L4.observation.* → weaken intent
        if "L8.intent" in antecedent and "L4.observation" in consequent:
            return CausalPatternDecision(
                should_act=True,
                action="weaken_intent",
                detail={"rationale": "fallback: intent→observation死循环，主动减弱"},
                reasoning="fallback: intent→observation死循环",
            )

        return CausalPatternDecision(
            should_act=True,
            reasoning="fallback: passed threshold, acting on pattern",
        )

    def _parse_causal_response(self, text: str) -> CausalPatternDecision:
        """解析 LLM 返回的因果模式决策."""
        import json, re
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if not m:
            m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return self._fallback_causal_pattern(0.0, 0.0)
        try:
            data = json.loads(m.group())
            return CausalPatternDecision(
                should_act=bool(data.get("should_act", False)),
                action=data.get("action"),
                detail=data.get("detail", {}),
                reasoning=data.get("reasoning", ""),
            )
        except (json.JSONDecodeError, KeyError):
            return self._fallback_causal_pattern(confidence, lift, antecedent, consequent)
