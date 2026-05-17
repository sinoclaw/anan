"""
L7 Goals — GoalAdvisor Subagent
===============================
Subagent for LLM-driven goal operations: generation, decomposition,
conflict resolution, and scoring/selection.

All LLM calls go through delegate_task (subagent mode), with a
rule-based fallback when no delegate is available.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("anan.L7.goal_advisor")

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class GoalDecisionAction(str, Enum):
    """Actions that GoalAdvisor can recommend."""
    # Generate goals
    GENERATE = "generate"        # LLM generated goals are worth creating
    SKIP_GENERATE = "skip_generate"
    # Decompose
    DECOMPOSE = "decompose"      # Goal should be decomposed into sub-goals
    SKIP_DECOMPOSE = "skip_decompose"
    # Conflict
    KEEP_BOTH = "keep_both"      # Goals don't actually conflict
    KEEP_A = "keep_a"            # Keep goal A, abandon goal B
    KEEP_B = "keep_b"
    MODIFY_A = "modify_a"        # Keep but modify one
    MODIFY_B = "modify_b"
    MERGE = "merge"              # Merge into a single goal
    # Scoring
    SELECT = "select"            # This goal should be selected next
    SKIP = "skip"                # Not the best next goal


@dataclass
class GoalDecision:
    """Result of a GoalAdvisor decision."""
    action: GoalDecisionAction
    goals: list[dict] = field(default_factory=list)  # parsed goal data
    scores: list[dict] = field(default_factory=list)  # [goal_id, score, rationale]
    reason: str = ""
    sub_goals: list[dict] = field(default_factory=list)  # for decompose
    resolution: str = "none"  # for conflict: keep_a/keep_b/modify_a/modify_b/merge/none
    used_rule_fallback: bool = False  # True when advisor fell back to rule-based logic


@dataclass
class GoalContext:
    """Context passed to GoalAdvisor for evaluation."""
    task: str  # "generate" | "decompose" | "resolve_conflict" | "score"

    # For generate
    context_prompt: str = ""

    # For decompose
    goal_description: str = ""
    goal_scope: str = ""
    goal_id: str = ""

    # For resolve_conflict
    goals_data: str = ""  # formatted goal list string

    # For score
    goal_list: str = ""
    current_time: str = ""

    # For all: optional metadata
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fallback handlers (rule-based, no LLM needed)
# ---------------------------------------------------------------------------

def _fallback_generate(context: GoalContext) -> GoalDecision:
    """Fallback: return SKIP (let the LLM path handle it via _call_llm)."""
    return GoalDecision(action=GoalDecisionAction.SKIP_GENERATE, reason="no_delegate", used_rule_fallback=True)


def _fallback_decompose(context: GoalContext) -> GoalDecision:
    """Fallback: decompose if goal has >5 words (non-trivial)."""
    words = len(context.goal_description.split())
    if words > 5:
        return GoalDecision(action=GoalDecisionAction.DECOMPOSE, reason=f"fallback: {words} words > 5 threshold", used_rule_fallback=True)
    return GoalDecision(action=GoalDecisionAction.SKIP_DECOMPOSE, reason=f"fallback: {words} words <= 5", used_rule_fallback=True)


def _fallback_resolve_conflict(context: GoalContext) -> GoalDecision:
    """Fallback: keep both if no clear winner."""
    return GoalDecision(
        action=GoalDecisionAction.KEEP_BOTH,
        reason="fallback: keep both goals",
        resolution="none",
        used_rule_fallback=True,
    )


def _fallback_score(context: GoalContext) -> GoalDecision:
    """Fallback: score based on scope priority (immediate > short > medium > long)."""
    scope_order = {"immediate": 4, "short": 3, "medium": 2, "long": 1}
    lines = context.goal_list.strip().split("\n")
    scored = []
    for line in lines:
        # Extract scope from "scope=immediate" etc.
        m = re.search(r'scope=(immediate|short|medium|long)', line)
        scope = m.group(1) if m else "medium"
        score = scope_order.get(scope, 2) / 4.0
        # Try to extract id
        id_m = re.search(r'id=([^,]+)', line)
        gid = id_m.group(1) if id_m else "unknown"
        scored.append({"goal_id": gid, "score": score, "rationale": f"fallback: scope={scope}"})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return GoalDecision(
        action=GoalDecisionAction.SELECT if scored else GoalDecisionAction.SKIP,
        scores=scored,
        reason="fallback: scope-based scoring",
        used_rule_fallback=True,
    )


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

GENERATE_PROMPT = """你是一个目标生成助手。根据以下情境，为anan（一个AI助手）生成3-5个有意义的目标。

情境：{context}

要求：
- 目标要具体、可执行、有时限
- 每个目标要有清晰的完成标准
- 涵盖不同时间范围（immediate/short/medium/long）
- 标签（tags）用英文逗号分隔

返回格式（严格JSON）：
{{
  "goals": [
    {{
      "description": "目标描述",
      "scope": "immediate|short|medium|long",
      "tags": ["tag1", "tag2"]
    }}
  ]
}}"""


DECOMPOSE_PROMPT = """将以下目标分解为3-6个可执行的子目标。

目标：{goal_description}
时间范围：{scope}

要求：
- 子目标要具体、可验证
- 按执行顺序排列
- 每个子目标应该是独立的、可单独完成的任务

返回格式（严格JSON）：
{{
  "sub_goals": [
    {{
      "description": "子目标描述",
      "scope": "immediate|short|medium|long",
      "action_type": "immediate|plan|explore"
    }}
  ]
}}"""


CONFLICT_PROMPT = """分析以下两个目标是否存在冲突，并决定如何处理。

目标A：{goal_a_description}
标签A：{tags_a}
时间范围A：{scope_a}

目标B：{goal_b_description}
标签B：{tags_b}
时间范围B：{scope_b}

共同标签：{overlap_tags}

请判断：
1. 这两个目标是否真的冲突（资源、注意力、时间上互斥）？
2. 如果冲突，应该保留哪个、修改哪个或放弃哪个？
3. 如果不冲突，说明原因。

返回格式（严格JSON）：
{{
  "is_conflict": true|false,
  "resolution": "keep_a|keep_b|modify_a|modify_b|abandon_a|abandon_b|merge|none",
  "reason": "解释原因",
  "suggested_modification": "如果需要修改，描述修改后的目标"
}}"""


SCORE_PROMPT = """为以下每个目标打分（0.0-1.0），决定下一个应该追求哪个。

当前时间：{current_time}
目标列表：
{goal_list}

评分标准（重要性 x 紧急度 x 可行性）：
- 重要性：这个目标对anan的长期发展和用户价值有多大？
- 紧急度：时间敏感性多高？
- 可行性：当前资源和技术条件下能完成吗？
- 协同效应：完成后对其他目标有帮助吗？

返回格式（严格JSON）：
{{
  "scores": [
    {{
      "goal_id": "goal-0",
      "score": 0.85,
      "rationale": "为什么这个分数"
    }}
  ]
}}"""


# ---------------------------------------------------------------------------
# GoalAdvisor
# ---------------------------------------------------------------------------

class GoalAdvisor:
    """
    Subagent for goal-level LLM operations.

    Handles:
    - Goal generation from context
    - Goal decomposition into sub-goals
    - Conflict resolution between goals
    - Goal scoring and selection

    Fallback to rule-based handlers when no delegate is available.
    """

    def __init__(self, delegate_fn: Optional[callable] = None):
        self._delegate_fn: Optional[callable] = delegate_fn
        self._cooldown: float = 5.0  # seconds between LLM calls

    def set_delegate(self, fn: callable) -> None:
        self._delegate_fn = fn

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def decide(self, context: GoalContext) -> GoalDecision:
        """Main entry point. Routes to the appropriate handler based on task."""
        task = context.task

        if task == "generate":
            return await self._decide_generate(context)
        elif task == "decompose":
            return await self._decide_decompose(context)
        elif task == "resolve_conflict":
            return await self._decide_conflict(context)
        elif task == "score":
            return await self._decide_score(context)
        else:
            logger.warning("GoalAdvisor: unknown task %s", task)
            return GoalDecision(action=GoalDecisionAction.SKIP, reason=f"unknown task: {task}")

    # -------------------------------------------------------------------------
    # Generate
    # -------------------------------------------------------------------------

    async def _decide_generate(self, context: GoalContext) -> GoalDecision:
        if not self._delegate_fn:
            return _fallback_generate(context)

        prompt = GENERATE_PROMPT.format(context=context.context_prompt)
        result = await self._call_delegate(
            task="goal_generate",
            messages=[{"role": "user", "content": prompt}],
        )
        return self._parse_generate(result)

    def _parse_generate(self, text: str) -> GoalDecision:
        data = self._extract_json(text)
        if not data:
            return GoalDecision(action=GoalDecisionAction.SKIP_GENERATE, reason="parse_failed")
        goals = data.get("goals", [])
        if not goals:
            return GoalDecision(action=GoalDecisionAction.SKIP_GENERATE, reason="no_goals")
        return GoalDecision(
            action=GoalDecisionAction.GENERATE,
            goals=goals,
            reason=f"generated {len(goals)} goals",
        )

    # -------------------------------------------------------------------------
    # Decompose
    # -------------------------------------------------------------------------

    async def _decide_decompose(self, context: GoalContext) -> GoalDecision:
        if not self._delegate_fn:
            return _fallback_decompose(context)

        prompt = DECOMPOSE_PROMPT.format(
            goal_description=context.goal_description,
            scope=context.goal_scope,
        )
        result = await self._call_delegate(
            task="goal_decompose",
            messages=[{"role": "user", "content": prompt}],
        )
        return self._parse_decompose(result, context)

    def _parse_decompose(self, text: str, context: GoalContext) -> GoalDecision:
        data = self._extract_json(text)
        if not data:
            return _fallback_decompose(context)
        sub_goals = data.get("sub_goals", [])
        if not sub_goals:
            return GoalDecision(
                action=GoalDecisionAction.SKIP_DECOMPOSE,
                reason="no_sub_goals",
            )
        return GoalDecision(
            action=GoalDecisionAction.DECOMPOSE,
            sub_goals=sub_goals,
            reason=f"decomposed into {len(sub_goals)} sub_goals",
        )

    # -------------------------------------------------------------------------
    # Conflict resolution
    # -------------------------------------------------------------------------

    async def _decide_conflict(self, context: GoalContext) -> GoalDecision:
        if not self._delegate_fn:
            return _fallback_resolve_conflict(context)

        # Parse goals_data string to extract goal pairs
        # goals_data format: "goal_a_desc|TAGS_A|scope_a||goal_b_desc|TAGS_B|scope_b"
        parts = context.goals_data.split("||")
        if len(parts) != 2:
            return _fallback_resolve_conflict(context)

        ga, gb = parts[0], parts[1]
        ga_parts = ga.split("|")
        gb_parts = gb.split("|")
        if len(ga_parts) < 3 or len(gb_parts) < 3:
            return _fallback_resolve_conflict(context)

        goal_a_desc, tags_a, scope_a = ga_parts[0], ga_parts[1], ga_parts[2]
        goal_b_desc, tags_b, scope_b = gb_parts[0], gb_parts[1], gb_parts[2]
        overlap_tags = context.extra.get("overlap_tags", "")

        prompt = CONFLICT_PROMPT.format(
            goal_a_description=goal_a_desc,
            tags_a=tags_a,
            scope_a=scope_a,
            goal_b_description=goal_b_desc,
            tags_b=tags_b,
            scope_b=scope_b,
            overlap_tags=overlap_tags,
        )
        result = await self._call_delegate(
            task="goal_conflict",
            messages=[{"role": "user", "content": prompt}],
        )
        return self._parse_conflict(result)

    def _parse_conflict(self, text: str) -> GoalDecision:
        data = self._extract_json(text)
        if not data:
            return _fallback_resolve_conflict(GoalContext(task="resolve_conflict"))
        resolution = data.get("resolution", "none")
        reason = data.get("reason", "")
        action_map = {
            "keep_a": GoalDecisionAction.KEEP_A,
            "keep_b": GoalDecisionAction.KEEP_B,
            "modify_a": GoalDecisionAction.MODIFY_A,
            "modify_b": GoalDecisionAction.MODIFY_B,
            "merge": GoalDecisionAction.MERGE,
            "none": GoalDecisionAction.KEEP_BOTH,
        }
        action = action_map.get(resolution, GoalDecisionAction.KEEP_BOTH)
        return GoalDecision(
            action=action,
            reason=reason,
            resolution=resolution,
        )

    # -------------------------------------------------------------------------
    # Score
    # -------------------------------------------------------------------------

    async def _decide_score(self, context: GoalContext) -> GoalDecision:
        if not self._delegate_fn:
            return _fallback_score(context)

        prompt = SCORE_PROMPT.format(
            current_time=context.current_time,
            goal_list=context.goal_list,
        )
        result = await self._call_delegate(
            task="goal_score",
            messages=[{"role": "user", "content": prompt}],
        )
        return self._parse_score(result)

    def _parse_score(self, text: str) -> GoalDecision:
        data = self._extract_json(text)
        if not data:
            return _fallback_score(GoalContext(task="score", goal_list="", current_time=""))
        scores = data.get("scores", [])
        if not scores:
            return GoalDecision(action=GoalDecisionAction.SKIP, reason="no_scores")
        return GoalDecision(
            action=GoalDecisionAction.SELECT,
            scores=scores,
            reason=f"scored {len(scores)} goals",
        )

    # -------------------------------------------------------------------------
    # Delegate helper
    # -------------------------------------------------------------------------

    async def _call_delegate(self, task: str, messages: list[dict]) -> str:
        """Call delegate_task, fallback to rule-based on failure."""
        if not self._delegate_fn:
            return ""
        try:
            result = self._delegate_fn(
                task=task,
                messages=messages,
            )
            # result may be Awaitable or direct str
            if hasattr(result, "__await__"):
                return await result
            return result
        except Exception as exc:
            logger.warning("GoalAdvisor delegate failed for %s: %s", task, exc)
            return ""

    # -------------------------------------------------------------------------
    # JSON parsing
    # -------------------------------------------------------------------------

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        """Extract JSON from LLM response."""
        if not text:
            return None
        # Strategy 1: find ```json ... ```
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # Strategy 2: find raw {...}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return None
