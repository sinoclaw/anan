"""
L9 Self — Self Evaluation Advisor (Subagent)
============================================
周期性评估 anan 整体健康状态：综合 L6 health / L7 goals / L5 patterns，
给出总体 self 健康评分（0-100）和改进建议。

设计原则：
- Handler: SelfModelLive 管 SelfModel 状态和 fact 写入
- Subagent: 给定各层状态数据，生成总体评估

为什么用 subagent：
- "系统健康"是模糊的多维评估 — 需要同时看 L6/L7/L5/L9 数据
- 评估标准随时段变化 — 爸爸在时更活跃，目标冲刺期更关注 goal
- 未来可以学: "当 self_health < 50 时优先保证 goal progress"

数据来源：
- L6 metacognition.report → health score, issue count
- L7 goals → active goals, avg progress, completed milestones
- L5 patterns → recent discoveries, pattern quality
- SelfModel → identity/vision/wisdom fact counts
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("anan.L9.self_eval")

# ---------------------------------------------------------------------------
# Evaluation Result
# ---------------------------------------------------------------------------

@dataclass
class SelfEvaluation:
    overall_score: float           # 0-100, 综合健康分
    health_dimension: float        # 0-100, L6 元认知健康
    goal_dimension: float          # 0-100, L7 目标进度
    pattern_dimension: float       # 0-100, L5 挖掘质量
    identity_dimension: float      # 0-100, L9 自我认知
    status_label: str             # "excellent" | "healthy" | "fair" | "struggling"
    top_strengths: list[str]     # 当前最强项
    top_concerns: list[str]      # 需要关注的项
    recommendations: list[str]    # 改进建议
    reasoning: str = ""           # 评估理由

    def to_dict(self) -> dict:
        return {
            "overall_score": self.overall_score,
            "health_dimension": self.health_dimension,
            "goal_dimension": self.goal_dimension,
            "pattern_dimension": self.pattern_dimension,
            "identity_dimension": self.identity_dimension,
            "status_label": self.status_label,
            "top_strengths": self.top_strengths,
            "top_concerns": self.top_concerns,
            "recommendations": self.recommendations,
            "reasoning": self.reasoning,
        }


# ---------------------------------------------------------------------------
# Fallback handler (rule-based)
# ---------------------------------------------------------------------------

def fallback_evaluate(
    health_score: Optional[float],
    issue_count: int,
    active_goals: int,
    avg_progress: float,
    completed_milestones: int,
    patterns_recent: int,
    identity_count: int,
    wisdom_count: int,
) -> SelfEvaluation:
    """Rule-based self-evaluation when subagent is unavailable.

    Scoring heuristics:
    - health_dimension: health_score if available, else 50 baseline
    - goal_dimension: avg_progress weighted by active_goals (no goals = neutral 50)
    - pattern_dimension: patterns_recent > 0 → 60-80, none → 40
    - identity_dimension: identity_count > 0 → 60 + min(identity_count * 5, 30)
    - overall: weighted average
    """
    # Health dimension
    if health_score is not None:
        health_dim = health_score * 100
    else:
        health_dim = max(0, 70 - issue_count * 10)

    # Goal dimension
    if active_goals == 0:
        goal_dim = 50  # neutral — no goals is not bad
    else:
        goal_dim = avg_progress * 100

    # Pattern dimension
    if patterns_recent == 0:
        pattern_dim = 40
    elif patterns_recent < 3:
        pattern_dim = 60
    elif patterns_recent < 10:
        pattern_dim = 75
    else:
        pattern_dim = 80

    # Identity dimension
    if identity_count == 0:
        identity_dim = 30  # just born
    else:
        identity_dim = min(60 + identity_count * 5, 95)

    # Overall: weighted average
    overall = (
        health_dim * 0.35 +
        goal_dim * 0.30 +
        pattern_dim * 0.20 +
        identity_dim * 0.15
    )

    # Status label
    if overall >= 85:
        label = "excellent"
    elif overall >= 70:
        label = "healthy"
    elif overall >= 50:
        label = "fair"
    else:
        label = "struggling"

    # Strengths
    strengths = []
    if health_dim >= 80:
        strengths.append("元认知健康稳定")
    if goal_dim >= 70 and active_goals > 0:
        strengths.append(f"目标推进良好（均进度 {avg_progress:.0%}）")
    if patterns_recent >= 5:
        strengths.append(f"因果规律发现活跃（{patterns_recent}个新规律）")
    if identity_dim >= 70:
        strengths.append("自我认知清晰")

    # Concerns
    concerns = []
    if issue_count >= 3:
        concerns.append(f"L6 累计 {issue_count} 个未解决 issue")
    if goal_dim < 50 and active_goals > 0:
        concerns.append("目标进度偏慢")
    if patterns_recent == 0:
        concerns.append("L5 尚未发现稳定规律")
    if identity_count < 3:
        concerns.append("自我认知仍在形成中")

    # Recommendations
    recs = []
    if concerns:
        recs.append(f"优先解决: {', '.join(concerns[:2])}")
    if health_dim < 60:
        recs.append("建议降低 L6 warn 阈值或执行 pending tuning")
    if goal_dim < 50 and active_goals == 0:
        recs.append("建议生成新的 immediate goal")
    if pattern_dim < 50:
        recs.append("建议降低 L5 min_lift 阈值以发现更多规律")

    reasoning = (
        f"health={health_dim:.0f} goal={goal_dim:.0f} "
        f"pattern={pattern_dim:.0f} identity={identity_dim:.0f} → overall={overall:.0f}"
    )

    return SelfEvaluation(
        overall_score=round(overall, 1),
        health_dimension=round(health_dim, 1),
        goal_dimension=round(goal_dim, 1),
        pattern_dimension=round(pattern_dim, 1),
        identity_dimension=round(identity_dim, 1),
        status_label=label,
        top_strengths=strengths[:3],
        top_concerns=concerns[:3],
        recommendations=recs[:3],
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Subagent prompt
# ---------------------------------------------------------------------------

SELF_EVALUATION_PROMPT = """你是 anan 的自我评估顾问。评估 anan 整体系统健康状态。

## 系统各层状态
HEALTH_SCORE: {health_score}（L6 元认知评分，0.0-1.0）
ISSUE_COUNT: {issue_count}（L6 未解决的 metacognition issues 数量）
ACTIVE_GOALS: {active_goals}（L7 当前活跃目标数）
AVG_GOAL_PROGRESS: {avg_progress}（L7 活跃目标平均进度，0.0-1.0）
COMPLETED_MILESTONES: {completed_milestones}（L7 已完成的里程碑总数）
PATTERNS_RECENT: {patterns_recent}（L5 最近发现的 pattern 数量）
IDENTITY_FACTS: {identity_count}（L9 身份事实数量）
WISDOM_FACTS: {wisdom_count}（L9 智慧规律数量）

## 评分说明
- health_dimension: L6 metacognition 健康分（0-100）
- goal_dimension: L7 目标进度分（0-100），无目标时为 50
- pattern_dimension: L5 规律发现分（0-100），无 pattern 时为 40
- identity_dimension: L9 自我认知分（0-100），身份事实越多越高
- overall_score: 综合分 = health×0.35 + goal×0.30 + pattern×0.20 + identity×0.15

## 状态标签
- ≥85: excellent（极佳）
- 70-84: healthy（健康）
- 50-69: fair（一般）
- <50: struggling（困难）

## 输出格式（严格 JSON）
{{
  "overall_score": 0-100,
  "health_dimension": 0-100,
  "goal_dimension": 0-100,
  "pattern_dimension": 0-100,
  "identity_dimension": 0-100,
  "status_label": "excellent"|"healthy"|"fair"|"struggling",
  "top_strengths": ["最强项1", "最强项2"],
  "top_concerns": ["关注项1", "关注项2"],
  "recommendations": ["建议1", "建议2"],
  "reasoning": "判断理由（1-3句）"
}}"""


# ---------------------------------------------------------------------------
# Self Evaluation Advisor
# ---------------------------------------------------------------------------

class SelfEvaluationAdvisor:
    """Subagent for periodic overall system self-assessment.

    Usage:
        advisor = SelfEvaluationAdvisor(delegate_fn=delegate_task)
        eval = await advisor.evaluate(
            health_score=0.75,
            issue_count=1,
            active_goals=2,
            avg_progress=0.6,
            completed_milestones=3,
            patterns_recent=5,
            identity_count=8,
            wisdom_count=12,
        )
    """

    def __init__(
        self,
        delegate_fn: Optional[callable] = None,
    ):
        self._delegate_fn = delegate_fn

    def set_delegate(self, fn: callable) -> None:
        self._delegate_fn = fn

    async def evaluate(
        self,
        health_score: Optional[float],
        issue_count: int,
        active_goals: int,
        avg_progress: float,
        completed_milestones: int,
        patterns_recent: int,
        identity_count: int,
        wisdom_count: int,
    ) -> SelfEvaluation:
        """Run self-evaluation across all layers."""
        prompt = SELF_EVALUATION_PROMPT.format(
            health_score=f"{health_score:.2f}" if health_score is not None else "N/A",
            issue_count=issue_count,
            active_goals=active_goals,
            avg_progress=f"{avg_progress:.0%}",
            completed_milestones=completed_milestones,
            patterns_recent=patterns_recent,
            identity_count=identity_count,
            wisdom_count=wisdom_count,
        )

        if not self._delegate_fn:
            logger.debug("SelfEvaluationAdvisor: no delegate_fn, using fallback")
            return fallback_evaluate(
                health_score, issue_count, active_goals,
                avg_progress, completed_milestones, patterns_recent,
                identity_count, wisdom_count,
            )

        try:
            result_text = await self._delegate_fn(
                goal="anan 全系统健康评估",
                context=prompt,
                skills=["agent"],
            )
            evaluation = self._parse_response(result_text)
            logger.info(
                "SelfEvaluationAdvisor: overall=%.1f (%s) — health=%.1f goal=%.1f pattern=%.1f identity=%.1f",
                evaluation.overall_score, evaluation.status_label,
                evaluation.health_dimension, evaluation.goal_dimension,
                evaluation.pattern_dimension, evaluation.identity_dimension,
            )
            return evaluation
        except Exception as exc:
            logger.warning(
                "SelfEvaluationAdvisor subagent failed: %s, falling back", exc,
            )
            return fallback_evaluate(
                health_score, issue_count, active_goals,
                avg_progress, completed_milestones, patterns_recent,
                identity_count, wisdom_count,
            )

    @staticmethod
    def _parse_response(text: str) -> SelfEvaluation:
        """Parse subagent text response into SelfEvaluation."""
        # Strategy 1: ```json ... ```
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                return SelfEvaluationAdvisor._from_data(data)
            except json.JSONDecodeError:
                pass

        # Strategy 2: raw {...}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                return SelfEvaluationAdvisor._from_data(data)
            except json.JSONDecodeError:
                pass

        logger.warning("SelfEvaluationAdvisor: could not parse: %s", text[:200])
        return fallback_evaluate(None, 0, 0, 0.0, 0, 0, 0, 0)

    @staticmethod
    def _from_data(data: dict) -> SelfEvaluation:
        return SelfEvaluation(
            overall_score=float(data.get("overall_score", 50)),
            health_dimension=float(data.get("health_dimension", 50)),
            goal_dimension=float(data.get("goal_dimension", 50)),
            pattern_dimension=float(data.get("pattern_dimension", 50)),
            identity_dimension=float(data.get("identity_dimension", 50)),
            status_label=data.get("status_label", "fair"),
            top_strengths=list(data.get("top_strengths") or []),
            top_concerns=list(data.get("top_concerns") or []),
            recommendations=list(data.get("recommendations") or []),
            reasoning=data.get("reasoning", ""),
        )
