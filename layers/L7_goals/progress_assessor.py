"""
L7 Goals — Progress Assessor (Subagent Mode)
============================================
评估目标完成进度（0-100%）。

设计原则：
- Handler: GoalEngine 管状态，milestone 管理，achieve/abandon 逻辑
- Subagent: 给定 goal + context，做进度推理，返回 progress + reasoning
- 无 LLM bridge 依赖，subagent 独立运行，handler 只做状态持久化

Subagent prompt 设计：
- few-shot examples 约束输出格式
- 要求输出 JSON schema
- 要求输出 reasoning trace
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("anan.L7.progress_assessor")

# ---------------------------------------------------------------------------
# Progress Assessment Result
# ---------------------------------------------------------------------------

@dataclass
class ProgressResult:
    progress: int          # 0-100
    reasoning: str         # Why this score
    next_milestone: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "progress": self.progress,
            "reasoning": self.reasoning,
            "next_milestone": self.next_milestone,
        }


# ---------------------------------------------------------------------------
# Subagent prompt
# ---------------------------------------------------------------------------

PROGRESS_ASSESSMENT_PROMPT = """你是一个目标进度评估专家。为 AI agent "anan" 评估目标完成进度。

## 你的任务
给定一个目标及其上下文，评估当前完成进度（0-100%），并给出理由。

## 评估标准
- 0%: 刚创建或完全没有开始
- 1-30%: 刚刚起步，核心工作尚未开始
- 31-60%: 已有实质性进展，部分里程碑已完成
- 61-90%: 接近完成，还剩关键里程碑
- 100%: 目标完全达成，可以调用 achieve

## 目标信息
GOAL_ID: {goal_id}
描述: {goal_description}
范围: {scope}
标签: {tags}
创建时间: {created_at}
当前状态: {status}
已有进度: {current_progress}%
MILESTONES（检查点）:
{milestones_text}

## 上下文（最近的认知事件）
{context}

## 要求
1. 综合所有信息，给出 0-100 的进度估值
2. 如果有 milestones，优先根据完成情况估算
3. 如果没有 milestones，根据目标性质和已有行动估算
4. 进度100表示目标完全达成，可以调用 achieve；0表示刚起步
5. 指出当前最关键的下一个 milestone（如果有）

## 输出格式（严格 JSON，不要有其他内容）
{{
  "progress": 整数0-100,
  "reasoning": "简短原因说明（1-3句）",
  "next_milestone": "下一个最重要未完成milestone描述，或null"
}}"""


# ---------------------------------------------------------------------------
# Fallback handler (rule-based, no subagent needed)
# ---------------------------------------------------------------------------

def fallback_assess(goal) -> ProgressResult:
    """Rule-based progress assessment when subagent is unavailable.

    Strategy:
    - If milestones exist: completed / total * 100
    - If no milestones: keep current progress (no change)
    """
    if goal.milestones:
        total = len(goal.milestones)
        completed = sum(1 for m in goal.milestones if m.completed)
        progress = int(completed / total * 100)
        next_m = next((m.description for m in goal.milestones if not m.completed), None)
        return ProgressResult(
            progress=progress,
            reasoning=f"Fallback: {completed}/{total} milestones completed",
            next_milestone=next_m,
        )

    # No milestones and no subagent: keep current progress
    return ProgressResult(
        progress=goal.progress,
        reasoning="Fallback: no milestones, no subagent, keeping current progress",
        next_milestone=None,
    )


# ---------------------------------------------------------------------------
# Progress Assessor (delegates to subagent)
# ---------------------------------------------------------------------------

class ProgressAssessor:
    """Subagent-based goal progress assessor.

    Uses delegate_task to spawn a subagent for progress evaluation.
    Falls back to rule-based handler if delegation fails.

    Usage:
        assessor = ProgressAssessor(delegate_fn=delegate_task)
        result = await assessor.assess(goal, recent_events=["L5.pattern.discovered ..."])
    """

    def __init__(
        self,
        delegate_fn: Optional[callable] = None,
        recent_events: Optional[list[str]] = None,
    ):
        """
        Args:
            delegate_fn: delegate_task function, injected at construction or via set_delegate
            recent_events: default recent events (can be overridden per call)
        """
        self._delegate_fn = delegate_fn
        self._recent_events = recent_events or []

    def set_delegate(self, fn: callable) -> None:
        """Allow late injection of delegate_task."""
        self._delegate_fn = fn

    async def assess(self, goal, recent_events: Optional[list[str]] = None) -> ProgressResult:
        """Assess progress for a single goal via subagent.

        Tries subagent first (delegate_task), falls back to rule-based handler on failure.
        """
        if not goal or goal.status.value not in ("active", "proposed"):
            return fallback_assess(goal)

        # Build milestones text
        if goal.milestones:
            milestones_text = "\n".join(
                f"  - [{'x' if m.completed else ' '}] {m.description}"
                for m in goal.milestones
            )
        else:
            milestones_text = "  (无 milestone)"

        # Build context from recent events
        events = recent_events or self._recent_events
        context = "\n".join(f"  - {e}" for e in events[-10:]) or "  (无近期事件)"

        prompt = PROGRESS_ASSESSMENT_PROMPT.format(
            goal_id=goal.id,
            goal_description=goal.description,
            created_at=goal.created_at,
            scope=goal.scope.value,
            tags=", ".join(goal.tags) or "(无标签)",
            status=goal.status.value,
            milestones_text=milestones_text,
            current_progress=goal.progress,
            context=context,
        )

        if not self._delegate_fn:
            logger.debug(
                "ProgressAssessor: no delegate_fn, using fallback for goal=%s",
                goal.id,
            )
            return fallback_assess(goal)

        try:
            # Launch subagent via delegate_task
            result_text = await self._delegate_fn(
                goal=f"评估目标进度: {goal.description}",
                context=prompt,
                skills=["agent"],
            )

            parsed = self._parse_response(result_text)
            logger.info(
                "ProgressAssessor: goal=%s progress=%d via subagent",
                goal.id, parsed.progress,
            )
            return parsed

        except Exception as exc:
            logger.warning(
                "ProgressAssessor subagent failed for goal=%s: %s, falling back",
                goal.id, exc,
            )
            return fallback_assess(goal)

    @staticmethod
    def _parse_response(text: str) -> ProgressResult:
        """Parse subagent text response into ProgressResult."""
        # Strategy 1: ```json ... ```
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                return ProgressAssessor._from_data(data)
            except json.JSONDecodeError:
                pass

        # Strategy 2: raw {...}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                return ProgressAssessor._from_data(data)
            except json.JSONDecodeError:
                pass

        logger.warning(
            "ProgressAssessor: could not parse subagent response: %s",
            text[:200],
        )
        return ProgressResult(
            progress=50,
            reasoning="解析失败，保守估计50%",
            next_milestone=None,
        )

    @staticmethod
    def _from_data(data: dict) -> ProgressResult:
        progress = max(0, min(100, int(data.get("progress", 50))))
        reasoning = data.get("reasoning", "")
        next_m = data.get("next_milestone")
        if next_m == "":
            next_m = None
        return ProgressResult(
            progress=progress,
            reasoning=reasoning,
            next_milestone=next_m,
        )
