"""
L4 Consciousness — Output Gate Advisor (Subagent)
================================================
评估思考是否值得推给用户（而非仅存为内部笔记）。

设计原则：
- Handler: OutputGate 管 thought buffer 和推送执行
- Subagent: 给定 thought + stream context，判断推送优先级

为什么用 subagent：
- "这条想法是否值得打扰用户"需要综合判断
- 需要看近期 stream 里有没有重复内容
- CRITICAL/HIGH 等规则是保守的，subagent 可以更灵活
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("anan.L4.output_gate")

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class PushDecision:
    decision: str              # "push" | "internal"
    priority_score: float      # 0.0-1.0, 推送优先级
    reasoning: str           # 判断理由
    alternative_action: str   # 不推送的话建议怎么处理

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "priority_score": round(self.priority_score, 3),
            "reasoning": self.reasoning,
            "alternative_action": self.alternative_action,
        }


# ---------------------------------------------------------------------------
# Fallback handler
# ---------------------------------------------------------------------------

def fallback_decision(
    thought_type: str,
    importance: str,
    content: str,
    recent_contents: list[str],
) -> PushDecision:
    """Rule-based push decision when subagent is unavailable.

    Mirrors the existing OutputGate._should_push() logic.
    """
    # CRITICAL → push
    if importance == "critical":
        return PushDecision(
            decision="push",
            priority_score=1.0,
            reasoning="CRITICAL importance → must push",
            alternative_action="none",
        )

    # HIGH + pushable type → push
    if importance == "high" and thought_type in ("dialogue_reflection", "drive_suggestion"):
        return PushDecision(
            decision="push",
            priority_score=0.9,
            reasoning=f"HIGH importance + type={thought_type} → should push",
            alternative_action="log_to_stream",
        )

    # MEDIUM + duplicate → push (remind user)
    if importance == "medium":
        # Check for duplicates
        normalized = re.sub(r"[^\w\u4e00-\u9fff]", "", content).lower()
        for recent in recent_contents[:5]:
            recent_norm = re.sub(r"[^\w\u4e00-\u9fff]", "", recent).lower()
            if normalized and normalized == recent_norm:
                return PushDecision(
                    decision="push",
                    priority_score=0.7,
                    reasoning="MEDIUM importance + duplicate of recent thought → push as reminder",
                    alternative_action="skip_generation",
                )
        return PushDecision(
            decision="internal",
            priority_score=0.4,
            reasoning="MEDIUM importance + no duplicate → store internally",
            alternative_action="log_to_stream",
        )

    # LOW → internal
    return PushDecision(
        decision="internal",
        priority_score=0.2,
        reasoning=f"LOW importance → store internally",
        alternative_action="log_to_stream",
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

PUSH_PROMPT = """你是 anan L4 意识层的 Output Gate 顾问。

## 待评估思考
TYPE: {thought_type}
IMPORTANCE: {importance}
CONTENT: {content}

## 最近 5 条思考内容
RECENT_THOUGHTS:
{recent_thoughts}

## 判断标准
- 推送给用户 = 打扰用户，只有高价值想法才推送
- 内部笔记 = 不打扰用户，常规思考存为笔记即可
- "internal" 并不意味放弃这条思考——只是存在 stream 里不弹出

## 输出格式（严格 JSON）
{{
  "decision": "push"|"internal",
  "priority_score": 0.0-1.0,
  "reasoning": "判断理由（1-2句）",
  "alternative_action": "log_to_stream"|"skip_generation"|"none"
}}"""


# ---------------------------------------------------------------------------
# Advisor
# ---------------------------------------------------------------------------

class OutputGateAdvisor:
    """Subagent for thought push decisions.

    Usage:
        advisor = OutputGateAdvisor(delegate_fn=delegate_task)
        decision = await advisor.decide(
            thought_type="dialogue_reflection",
            importance="medium",
            content="我刚才的回答可以更好...",
            recent_thoughts=["内容1", "内容2"],
        )
    """

    def __init__(self, delegate_fn: Optional[callable] = None):
        self._delegate_fn = delegate_fn

    def set_delegate(self, fn: callable) -> None:
        self._delegate_fn = fn

    async def decide(
        self,
        thought_type: str,
        importance: str,
        content: str,
        recent_thoughts: Optional[list[str]] = None,
    ) -> PushDecision:
        """Decide whether to push a thought to the user."""
        recent = recent_thoughts or []
        recent_text = "\n".join(f"- {t[:100]}" for t in recent[-5:])

        prompt = PUSH_PROMPT.format(
            thought_type=thought_type,
            importance=importance,
            content=content[:300],
            recent_thoughts=recent_text or "(no recent thoughts)",
        )

        if not self._delegate_fn:
            return fallback_decision(thought_type, importance, content, recent)

        try:
            result_text = await self._delegate_fn(
                goal="思考推送决策 — Output Gate",
                context=prompt,
                skills=["agent"],
            )
            return self._parse_response(result_text, thought_type, importance, content, recent)
        except Exception as exc:
            logger.warning("OutputGateAdvisor subagent failed: %s, fallback", exc)
            return fallback_decision(thought_type, importance, content, recent)

    def _parse_response(
        self,
        text: str,
        thought_type: str,
        importance: str,
        content: str,
        recent_thoughts: list[str],
    ) -> PushDecision:
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                return PushDecision(
                    decision=data.get("decision", "internal"),
                    priority_score=float(data.get("priority_score", 0.5)),
                    reasoning=data.get("reasoning", ""),
                    alternative_action=data.get("alternative_action", "log_to_stream"),
                )
            except (json.JSONDecodeError, ValueError):
                pass

        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                return PushDecision(
                    decision=data.get("decision", "internal"),
                    priority_score=float(data.get("priority_score", 0.5)),
                    reasoning=data.get("reasoning", ""),
                    alternative_action=data.get("alternative_action", "log_to_stream"),
                )
            except (json.JSONDecodeError, ValueError):
                pass

        logger.warning("OutputGateAdvisor: could not parse: %s", text[:200])
        return fallback_decision(thought_type, importance, content, recent_thoughts)
