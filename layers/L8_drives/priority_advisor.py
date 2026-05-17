"""
L8 Drives — Drive Priority Advisor (Subagent)
==============================================
评估目标优先级：给定候选目标 + 当前驱动力状态，判断优先级和 boost 值。

设计原则：
- Handler: DriveSystem 管驱动力状态和 decay/satisfy
- Subagent: 给定目标和驱动状态，判断 boost 强度

为什么用 subagent：
- "这个目标是否匹配当前驱动"是模糊判断
- 多个驱动竞争时需要权衡
- 目标标签可能和驱动器词汇不完全匹配
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("anan.L8.priority")

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class DrivePriority:
    priority_score: float          # 0.0-1.0, 综合优先级
    recommended_boost: float       # 0.0-1.0, 建议的 boost 值
    dominant_drive: str           # 最匹配的驱动力名称
    reasoning: str               # 判断理由
    alternative_goals: list[str]  # 备选目标建议

    def to_dict(self) -> dict:
        return {
            "priority_score": round(self.priority_score, 3),
            "recommended_boost": round(self.recommended_boost, 3),
            "dominant_drive": self.dominant_drive,
            "reasoning": self.reasoning,
            "alternative_goals": self.alternative_goals,
        }


# ---------------------------------------------------------------------------
# Fallback handler
# ---------------------------------------------------------------------------

def fallback_score(
    goal_tags: list[str],
    goal_description: str,
    active_drives: list[dict],
    top_drives: list[dict],
) -> DrivePriority:
    """Rule-based priority scoring when subagent is unavailable.

    Matching logic:
    - CURIOSITY → ["学习", "新", "好奇", "探索", "研究", "了解"]
    - COMPLETION → ["完成", "任务", "todo", "未完成", "收尾"]
    - CARE → ["爸爸", "用户", "关心", "帮助", "支持"]
    - AESTHETICS → ["优化", "改进", "代码", "整洁", "重构", "美感"]
    - BOREDOM → ["重复", "机械", "无聊", "寻找新方法", "变化"]
    """
    tag_map = {
        "curiosity": ["学习", "新", "好奇", "探索", "研究", "了解", "发现", "不懂"],
        "completion": ["完成", "任务", "todo", "未完成", "收尾", "结束", "close", "fix"],
        "care": ["爸爸", "用户", "关心", "帮助", "支持", "服务", "assist"],
        "aesthetics": ["优化", "改进", "代码", "整洁", "重构", "美感", "clean", "refactor"],
        "boredom": ["重复", "机械", "无聊", "寻找新方法", "变化", "不同", "新方向"],
    }

    combined = " ".join(goal_tags) + " " + goal_description
    best_drive, best_score = "none", 0.0
    for drive, keywords in tag_map.items():
        score = sum(1 for kw in keywords if kw in combined)
        if score > best_score:
            best_score = score
            best_drive = drive

    # Find matching active drives
    active_types = {d.get("type", "") for d in active_drives}
    if best_drive in active_types:
        drive_strength = next(
            (d.get("strength", 0.5) for d in active_drives if d.get("type") == best_drive),
            0.5,
        )
        boost = drive_strength * 0.4  # 0-0.4 based on drive strength
        priority = 0.5 + boost
    else:
        boost = 0.0
        priority = 0.3 + best_score * 0.1

    reasoning = f"目标标签{goal_tags}匹配 {best_drive} 驱动（强度={drive_strength if best_drive in active_types else 'N/A'}）"

    return DrivePriority(
        priority_score=min(1.0, priority),
        recommended_boost=min(1.0, boost),
        dominant_drive=best_drive,
        reasoning=reasoning,
        alternative_goals=[],
    )


# ---------------------------------------------------------------------------
# Subagent prompt
# ---------------------------------------------------------------------------

PRIORITY_PROMPT = """你是 anan 的驱动力优先级顾问。

## 候选目标
GOAL_TAGS: {goal_tags}
GOAL_DESCRIPTION: {goal_description}

## 当前驱动力状态
ACTIVE_DRIVES: {active_drives}
TOP_DRIVES: {top_drives}

## 驱动力类型说明
- curiosity: 遇到新概念想学习探索
- completion: 任务未完成想收尾
- care: 用户相关的事想帮忙
- aesthetics: 代码/方案丑想优化
- boredom: 重复劳动想找新方法

## 输出格式（严格 JSON）
{{
  "priority_score": 0.0-1.0,
  "recommended_boost": 0.0-1.0,
  "dominant_drive": "curiosity"|"completion"|"care"|"aesthetics"|"boredom"|"none",
  "reasoning": "判断理由（1-2句）",
  "alternative_goals": ["备选目标1", "备选目标2"]
}}"""


# ---------------------------------------------------------------------------
# Advisor
# ---------------------------------------------------------------------------

class DrivePriorityAdvisor:
    """Subagent for scoring goal priority based on drive state.

    Usage:
        advisor = DrivePriorityAdvisor(delegate_fn=delegate_task)
        result = await advisor.score_goal(
            goal_tags=["完成", "代码"],
            goal_description="实现用户要求的功能",
            active_drives=[{"type": "completion", "strength": 0.7}],
            top_drives=[{"type": "completion", "strength": 0.7}],
        )
    """

    def __init__(self, delegate_fn: Optional[callable] = None):
        self._delegate_fn = delegate_fn

    def set_delegate(self, fn: callable) -> None:
        self._delegate_fn = fn

    async def score_goal(
        self,
        goal_tags: list[str],
        goal_description: str,
        active_drives: list[dict],
        top_drives: list[dict],
    ) -> DrivePriority:
        """Score a goal's priority based on current drive state."""
        import json as _json

        prompt = PRIORITY_PROMPT.format(
            goal_tags=goal_tags,
            goal_description=goal_description,
            active_drives=_json.dumps(active_drives, ensure_ascii=False),
            top_drives=_json.dumps(top_drives, ensure_ascii=False),
        )

        if not self._delegate_fn:
            return fallback_score(goal_tags, goal_description, active_drives, top_drives)

        try:
            result_text = await self._delegate_fn(
                goal="L8 Drive 目标优先级评估",
                context=prompt,
                parent_agent=None,
            )
            return self._parse_response(result_text, goal_tags, goal_description, active_drives, top_drives)
        except Exception as exc:
            logger.warning("DrivePriorityAdvisor subagent failed: %s, fallback", exc)
            return fallback_score(goal_tags, goal_description, active_drives, top_drives)

    def _parse_response(
        self,
        text: str,
        goal_tags: list[str],
        goal_description: str,
        active_drives: list[dict],
        top_drives: list[dict],
    ) -> DrivePriority:
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                return DrivePriority(
                    priority_score=float(data.get("priority_score", 0.5)),
                    recommended_boost=float(data.get("recommended_boost", 0.0)),
                    dominant_drive=data.get("dominant_drive", "none"),
                    reasoning=data.get("reasoning", ""),
                    alternative_goals=list(data.get("alternative_goals") or []),
                )
            except (json.JSONDecodeError, ValueError):
                pass

        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                return DrivePriority(
                    priority_score=float(data.get("priority_score", 0.5)),
                    recommended_boost=float(data.get("recommended_boost", 0.0)),
                    dominant_drive=data.get("dominant_drive", "none"),
                    reasoning=data.get("reasoning", ""),
                    alternative_goals=list(data.get("alternative_goals") or []),
                )
            except (json.JSONDecodeError, ValueError):
                pass

        logger.warning("DrivePriorityAdvisor: could not parse: %s", text[:200])
        return fallback_score(goal_tags, goal_description, active_drives, top_drives)
