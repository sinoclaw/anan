"""
L2 Memory — Recall Signal Advisor (Subagent)
============================================
给定候选记忆内容和上下文，判断：是否值得晋升到 mid-term？是否值得晋升到 long-term？

设计原则：
- Handler: MemoryTier 管存储和 promotion 执行
- Subagent: 给定记忆内容和上下文，判断晋升优先级

为什么用 subagent：
- "这条记忆是否重要"是主观判断，受上下文影响
- 频繁访问但 importance 低的内容可能实际很重要
- 与当前目标相关的记忆应该提升 importance
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("anan.L2.recall_advisor")

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class RecallSignal:
    promotion_priority: float          # 0.0-1.0, 晋升到 mid-term 的优先级
    longterm_candidate: bool         # 是否值得晋升到 long-term
    suggested_importance: float      # 建议的 importance 值
    reasoning: str                # 判断理由
    tags: list[str]               # 建议的 tags

    def to_dict(self) -> dict:
        return {
            "promotion_priority": round(self.promotion_priority, 3),
            "longterm_candidate": self.longterm_candidate,
            "suggested_importance": round(self.suggested_importance, 3),
            "reasoning": self.reasoning,
            "tags": self.tags,
        }


# ---------------------------------------------------------------------------
# Fallback handler
# ---------------------------------------------------------------------------

def fallback_signal(
    content: str,
    current_importance: float,
    access_count: int,
    age_hours: float,
    context_tags: Optional[list[str]] = None,
) -> RecallSignal:
    """Rule-based recall signal when subagent is unavailable."""
    tags = context_tags or []

    # High-value signals
    longterm_keywords = ["愿景", "决定", "方向", "爸爸", "身份", "原则", "学到了"]
    mid_keywords = ["规律", "发现", "教训", "优化", "改进"]
    causal_keywords = ["之后", "导致", "常出现", "因果"]

    has_longterm = any(kw in content for kw in longterm_keywords)
    has_mid = any(kw in content for kw in mid_keywords)
    has_causal = any(kw in content for kw in causal_keywords)

    # Access frequency bonus
    access_bonus = min(0.3, access_count * 0.05)

    # Recency penalty (older = less urgent to promote)
    recency_penalty = min(0.2, age_hours / 1000)

    # Compute priority
    base = current_importance
    if has_longterm:
        base = max(base, 0.8)
    elif has_mid:
        base = max(base, 0.6)
    elif has_causal:
        base = max(base, 0.5)

    priority = min(1.0, base + access_bonus - recency_penalty)

    # Long-term candidate: high importance + either vision/long-term keywords or causal pattern
    longterm = priority >= 0.7 or (has_longterm and priority >= 0.5)
    suggested_importance = min(1.0, max(current_importance, priority))

    reasoning = (
        f"importance={current_importance:.2f}"
        f" access={access_count}x"
        f" age={age_hours:.0f}h"
        f" → priority={priority:.2f}"
        f" {'(longterm)' if longterm else '(mid only)'}"
    )

    return RecallSignal(
        promotion_priority=priority,
        longterm_candidate=longterm,
        suggested_importance=suggested_importance,
        reasoning=reasoning,
        tags=tags,
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

RECALL_PROMPT = """你是 anan 的记忆召回顾问。

## 候选记忆
CONTENT: {content}
CURRENT_IMPORTANCE: {current_importance}
ACCESS_COUNT: {access_count}
AGE_HOURS: {age_hours}
CONTEXT_TAGS: {context_tags}

## 判断标准
- promotion_priority: 这条记忆是否值得从 short-term promote 到 mid-term (0.0-1.0)
- longterm_candidate: 是否值得直接 promote 到 long-term MEMORY.md
- suggested_importance: 建议的 importance 值 (0.0-1.0)
- tags: 建议的标签（从 content 推断）

## 重要记忆特征
- 身份/愿景相关 → high importance + longterm_candidate=true
- 因果规律/教训 → mid-to-long priority
- 高频访问 → importance boost
- 老旧 (>24h) 但仍然被访问 → 可能是重要知识，priority boost

## 输出格式（严格 JSON）
{{
  "promotion_priority": 0.0-1.0,
  "longterm_candidate": true|false,
  "suggested_importance": 0.0-1.0,
  "reasoning": "判断理由（1-2句）",
  "tags": ["tag1", "tag2"]
}}"""


# ---------------------------------------------------------------------------
# Advisor
# ---------------------------------------------------------------------------

class RecallSignalAdvisor:
    """Subagent for memory promotion decisions.

    Usage:
        advisor = RecallSignalAdvisor(delegate_fn=delegate_task)
        signal = await advisor.evaluate(
            content="因果确认：X → Y（lift=2.3）",
            current_importance=0.5,
            access_count=3,
            age_hours=12.0,
            context_tags=["causal", "insight"],
        )
    """

    def __init__(self, delegate_fn: Optional[callable] = None):
        self._delegate_fn = delegate_fn

    def set_delegate(self, fn: callable) -> None:
        self._delegate_fn = fn

    async def evaluate(
        self,
        content: str,
        current_importance: float,
        access_count: int,
        age_hours: float,
        context_tags: Optional[list[str]] = None,
    ) -> RecallSignal:
        """Evaluate whether a memory should be promoted."""
        prompt = RECALL_PROMPT.format(
            content=content[:500],  # truncate long content
            current_importance=f"{current_importance:.2f}",
            access_count=access_count,
            age_hours=f"{age_hours:.1f}",
            context_tags=json.dumps(context_tags or [], ensure_ascii=False),
        )

        if not self._delegate_fn:
            return fallback_signal(content, current_importance, access_count, age_hours, context_tags)

        try:
            result_text = await self._delegate_fn(
                goal="记忆晋升优先级评估",
                context=prompt,
                skills=["agent"],
            )
            return self._parse_response(
                result_text, content, current_importance, access_count, age_hours, context_tags,
            )
        except Exception as exc:
            logger.warning("RecallSignalAdvisor subagent failed: %s, fallback", exc)
            return fallback_signal(content, current_importance, access_count, age_hours, context_tags)

    def _parse_response(
        self,
        text: str,
        content: str,
        current_importance: float,
        access_count: int,
        age_hours: float,
        context_tags: Optional[list[str]],
    ) -> RecallSignal:
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                return RecallSignal(
                    promotion_priority=float(data.get("promotion_priority", 0.5)),
                    longterm_candidate=bool(data.get("longterm_candidate", False)),
                    suggested_importance=float(data.get("suggested_importance", current_importance)),
                    reasoning=data.get("reasoning", ""),
                    tags=list(data.get("tags") or []),
                )
            except (json.JSONDecodeError, ValueError):
                pass

        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                return RecallSignal(
                    promotion_priority=float(data.get("promotion_priority", 0.5)),
                    longterm_candidate=bool(data.get("longterm_candidate", False)),
                    suggested_importance=float(data.get("suggested_importance", current_importance)),
                    reasoning=data.get("reasoning", ""),
                    tags=list(data.get("tags") or []),
                )
            except (json.JSONDecodeError, ValueError):
                pass

        logger.warning("RecallSignalAdvisor: could not parse: %s", text[:200])
        return fallback_signal(content, current_importance, access_count, age_hours, context_tags)
