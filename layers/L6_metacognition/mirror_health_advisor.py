"""
L6 Metacognition — MirrorHealthAdvisor
======================================

接管 Mirror._report() 的决策：
  1. 给定原始 metrics + issues，判断健康分和严重度
  2. 判断是否需要发 warn 事件
  3. 生成哪些 suggestion（基于 LLM 分析，fallback 用规则）

用法:
    advisor = MirrorHealthAdvisor()
    decision = await advisor.evaluate(context)
    # decision.score_override   — override the computed score (None = use own)
    # decision.healthy_override  — override healthy flag (None = use threshold)
    # decision.new_issues        — additional issues beyond the rule-based ones
    # decision.new_suggestions  — suggestions to add/replace
    # decision.emit_warn        — whether to force a warn event
    # decision.urgency          — low / medium / high
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tools.delegate_tool import delegate_task

import logging

logger = logging.getLogger("anan.L6.mirror_health_advisor")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class MirrorHealthContext:
    """mirror.reflect() 收集的原始数据，供 advisor 分析。"""

    # Bus metrics
    bus_published: int = 0
    bus_delivered: int = 0
    bus_errors: int = 0
    bus_error_rate: float = 0.0

    # Self-model metrics
    self_identity_count: int = 0
    self_vision_count: int = 0
    self_history_count: int = 0
    self_wisdom_count: int = 0
    self_stagnation_streak: int = 0

    # Working memory metrics
    wm_total_entries: int = 0
    wm_top_layer: str = ""
    wm_top_share: float = 0.0

    # Rule-based issues already generated (from if-else rules)
    rule_issues: list[str] = field(default_factory=list)
    rule_suggestions: list[str] = field(default_factory=list)
    rule_score: float = 0.0

    # System phase
    phase: str = "active"  # active / asleep / dream

    # History
    n_reports: int = 0  # how many reflect cycles have run

    def summary(self) -> str:
        return (
            f"bus_err={self.bus_error_rate:.2%} "
            f"id={self.self_identity_count} "
            f"stag={self.self_stagnation_streak} "
            f"wm_top={self.wm_top_layer}@{self.wm_top_share:.0%} "
            f"score={self.rule_score:.2f}"
        )


@dataclass
class MirrorHealthDecision:
    """advisor 的决策结果。"""

    # Override guidance — None means "use the rule-based value"
    score_override: Optional[float] = None  # None = keep rule_score
    healthy_override: Optional[bool] = None  # None = compute from score
    new_issues: list[str] = field(default_factory=list)
    new_suggestions: list[str] = field(default_factory=list)
    emit_warn: bool = False  # force L6.metacognition.warn even if healthy
    urgency: str = "low"      # low / medium / high
    reflection_note: str = ""  # brief LLM commentary on system health

    # If delegate unavailable, fall back to rule-based logic
    @property
    def final_score(self) -> float:
        return self.score_override if self.score_override is not None else 0.0

    @property
    def final_healthy(self) -> bool:
        if self.healthy_override is not None:
            return self.healthy_override
        return self.final_score >= 0.6


# ---------------------------------------------------------------------------
# Advisor
# ---------------------------------------------------------------------------


class MirrorHealthAdvisor:
    """Decides Mirror's health assessment using LLM reasoning.

    Falls back to rule-based heuristics when no delegate is configured.

    Decision logic:
    - Score: blend of bus health, self-model growth, attention balance
    - Warn: forced when urgency=high OR score < 0.4
    - Issues: may extend rule-based list with LLM-detected patterns
    - Suggestions: ranked by urgency
    """

    def __init__(self):
        self._delegate_fn: Optional[callable] = None
        self._last_decision: Optional[MirrorHealthDecision] = None
        self._last_decision_time: float = 0.0
        self._decision_cooldown: float = 30.0  # minimum seconds between LLM calls

    # ------------------------------------------------------------------ 
    # Public API
    # ------------------------------------------------------------------ 

    def set_delegate(self, fn: Optional[callable]) -> None:
        """MindStackRunner injects the async delegate callable here."""
        self._delegate_fn = fn

    async def evaluate(self, ctx: MirrorHealthContext) -> MirrorHealthDecision:
        """Main entry point — analyze mirror metrics and produce a health decision.

        Rate-limited to one LLM call per _decision_cooldown seconds.
        """
        now = time.time()

        # ---- Rule-based fallback (always available) ----
        fallback = self._fallback_decide(ctx)

        # ---- Try LLM delegate ----
        if self._delegate_fn is not None and (now - self._last_decision_time) > self._decision_cooldown:
            try:
                system_prompt = (
                    "你是一个九层认知架构的 L6 元认知顾问（MirrorHealthAdvisor）。"
                    "你的职责是评估 anan 的整体健康状态，并决定是否需要发出警告。\n\n"
                    "你收到的 MirrorHealthContext 包含：\n"
                    "  - bus_error_rate: 事件总线错误率（0.05=5%）\n"
                    "  - self_identity_count: 身份事实数量\n"
                    "  - self_stagnation_streak: 身份事实连续无增长的周期数\n"
                    "  - wm_top_share: 工作记忆中最活跃层占比（>0.7=注意力倾斜）\n"
                    "  - rule_score / rule_issues: 规则系统已有的评分和问题\n\n"
                    "你的决策（返回 JSON）：\n"
                    "  score_override: float 0.0~1.0（覆盖规则评分，null=保持规则）\n"
                    "  healthy_override: bool（覆盖健康标志，null=自动计算）\n"
                    "  new_issues: list[str]（在规则问题基础上追加的问题，描述要具体）\n"
                    "  new_suggestions: list[str]（建议列表，越重要越靠前）\n"
                    "  emit_warn: bool（是否强制发 warn 事件）\n"
                    "  urgency: \"low\" | \"medium\" | \"high\"\n"
                    "  reflection_note: str（简短 LLM 评注，20字以内）\n\n"
                    "注意：\n"
                    "  - 只有在 LLM 发现规则系统遗漏的重要问题时才设置 score_override\n"
                    "  - urgency=high 仅用于：score<0.4 或 严重度很高的系统性风险\n"
                    "  - new_suggestions 必须具体可操作，不是泛泛而谈\n"
                    "  - 始终保持 fallback 规则的问题不动，只追加新的"
                )
                user_prompt = self._build_user_message(ctx)
                decision = await self._delegate_fn(
                    goal=f"reflect\n\nSystem: {system_prompt}\n\nUser: {user_prompt}",
                )
                parsed = self._parse_decision(decision, fallback)
                self._last_decision = parsed
                self._last_decision_time = now
                logger.info(
                    "MirrorHealthAdvisor LLM decision: urgency=%s score=%.2f emit_warn=%s",
                    parsed.urgency,
                    parsed.final_score,
                    parsed.emit_warn,
                )
                return parsed

            except Exception as exc:
                logger.warning("MirrorHealthAdvisor delegate failed: %s — using fallback", exc)
                return fallback

        # ---- Rate-limited or no delegate: use fallback + cached if recent ----
        if self._last_decision and (now - self._last_decision_time) < self._decision_cooldown:
            logger.debug("MirrorHealthAdvisor: using cached decision (age=%.0fs)", now - self._last_decision_time)
            return self._last_decision
        return fallback

    # ------------------------------------------------------------------
    # Fallback — mirrors the original if-else logic exactly
    # ------------------------------------------------------------------

    def _fallback_decide(self, ctx: MirrorHealthContext) -> MirrorHealthDecision:
        """Rule-based fallback — reproduces the original Mirror.reflect() logic."""

        sub_scores: list[float] = []

        # ---- 1. Bus health ----
        if ctx.bus_error_rate == 0:
            sub_scores.append(1.0)
        elif ctx.bus_error_rate < 0.01:
            sub_scores.append(0.8)
        elif ctx.bus_error_rate < 0.05:
            sub_scores.append(0.5)
        else:
            sub_scores.append(0.2)

        # ---- 2. Self-model growth ----
        if ctx.self_identity_count == 0 and ctx.n_reports > 0:
            sub_scores.append(0.0)  # no identity after multiple cycles
        elif ctx.self_stagnation_streak == 0:
            sub_scores.append(1.0)
        elif ctx.self_stagnation_streak < 5:
            sub_scores.append(0.7)
        else:
            sub_scores.append(0.4)

        # ---- 3. Working memory attention ----
        if ctx.wm_total_entries == 0:
            sub_scores.append(0.5)
        elif ctx.wm_top_share > 0.7:
            sub_scores.append(0.5)
        else:
            sub_scores.append(1.0)

        score = sum(sub_scores) / len(sub_scores) if sub_scores else 0.0
        healthy = score >= 0.6

        # Emit warn if unhealthy or high error rate
        emit_warn = not healthy or ctx.bus_error_rate >= 0.05

        urgency = "low"
        if score < 0.4:
            urgency = "high"
        elif score < 0.6 or ctx.bus_error_rate >= 0.05:
            urgency = "medium"

        # Collect issues from rule-based checks
        issues: list[str] = []
        suggestions: list[str] = []

        if ctx.bus_error_rate >= 0.05:
            issues.append(f"事件总线错误率 {ctx.bus_error_rate:.1%} 严重")
            suggestions.append("立刻 detach 故障 handler")
        elif ctx.bus_error_rate >= 0.01:
            issues.append(f"事件总线错误率 {ctx.bus_error_rate:.1%} 偏高")
            suggestions.append("查看最近 errors 来源")

        if ctx.self_stagnation_streak >= 5:
            issues.append(f"身份事实已经 {ctx.self_stagnation_streak} 个周期没增长")
            suggestions.append("在 active 阶段尝试新活动")

        if ctx.self_identity_count == 0 and ctx.n_reports > 0:
            issues.append("还没形成身份事实 — 我还不知道我是谁")
            suggestions.append("多进行深度睡眠，让 reflect 跑")

        if ctx.wm_top_share > 0.7:
            issues.append(f"注意力倾斜：{ctx.wm_top_layer} 层占了 {ctx.wm_top_share:.0%}")
            suggestions.append(f"考虑提高其他层的 salience")

        if ctx.wm_total_entries == 0:
            issues.append("working memory 是空的")
            suggestions.append("等待事件流入")

        return MirrorHealthDecision(
            score_override=score,
            healthy_override=healthy,
            new_issues=issues,
            new_suggestions=suggestions,
            emit_warn=emit_warn,
            urgency=urgency,
            reflection_note="规则判定",
        )

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _build_user_message(self, ctx: MirrorHealthContext) -> str:
        return (
            f"Mirror 健康评估请求\n\n"
            f"当前指标：\n"
            f"  事件总线错误率: {ctx.bus_error_rate:.2%}（已发布={ctx.bus_published}, 错误={ctx.bus_errors}）\n"
            f"  身份事实数: {ctx.self_identity_count}（停滞周期={ctx.self_stagnation_streak}）\n"
            f"  愿景/历史/智慧: {ctx.self_vision_count}/{ctx.self_history_count}/{ctx.self_wisdom_count}\n"
            f"  工作记忆: {ctx.wm_total_entries} 条，Top={ctx.wm_top_layer}@{ctx.wm_top_share:.0%}\n"
            f"  已完成自省周期数: {ctx.n_reports}\n\n"
            f"规则系统已有：\n"
            f"  score={ctx.rule_score:.2f}\n"
            f"  issues={ctx.rule_issues}\n"
            f"  suggestions={ctx.rule_suggestions}\n\n"
            f"分析并决定：是否需要调整评分？是否要追加新问题或建议？是否发 warn？紧急度？\n"
            f"返回 JSON 格式的 MirrorHealthDecision。"
        )

    def _parse_decision(
        self, raw: str, fallback: MirrorHealthDecision
    ) -> MirrorHealthDecision:
        """Parse LLM JSON response into MirrorHealthDecision."""
        import json, re

        # Try to extract JSON block
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            logger.warning("MirrorHealthAdvisor: no JSON in LLM response, using fallback")
            return fallback
        try:
            d = json.loads(match.group())
        except json.JSONDecodeError:
            logger.warning("MirrorHealthAdvisor: JSON parse failed, using fallback")
            return fallback

        def _float(val, default):
            if val is None:
                return default
            try:
                return float(val)
            except (TypeError, ValueError):
                return default

        def _bool(val, default):
            if val is None:
                return default
            return bool(val)

        def _str(val, default):
            if val is None:
                return default
            return str(val)

        def _list(val):
            if not isinstance(val, list):
                return []
            return [str(x) for x in val]

        return MirrorHealthDecision(
            score_override=_float(d.get("score_override"), None),
            healthy_override=_bool(d.get("healthy_override"), None),
            new_issues=_list(d.get("new_issues", [])),
            new_suggestions=_list(d.get("new_suggestions", [])),
            emit_warn=_bool(d.get("emit_warn"), False),
            urgency=_str(d.get("urgency", "low"), "low"),
            reflection_note=_str(d.get("reflection_note", ""), ""),
        )
