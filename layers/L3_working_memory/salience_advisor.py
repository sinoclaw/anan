"""
L3 Working Memory — Salience Advisor (Subagent)
================================================
评估事件的 salience（重要性），决定是否纳入 Working Memory。

设计原则：
- Handler: WorkingMemory 管 buffer 和 eviction
- Subagent: 给定事件 + 上下文，评估 salience 分数

为什么用 subagent：
- "这个事件重不重要"是上下文相关的判断
- 相同 topic 的事件，在不同情境下 salience 可能差很多
- 例如：L0.circadian.tick 通常是 0.1，但如果 anan 正在处理紧急问题，
  tick 事件可能携带了关键时间信息
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("anan.L3.salience")

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class SalienceScore:
    score: float              # 0.0-1.0
    reasoning: str           # 判断理由
    override: bool           # 是否强制覆盖默认分

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "reasoning": self.reasoning,
            "override": self.override,
        }


# ---------------------------------------------------------------------------
# Fallback handler
# ---------------------------------------------------------------------------

def fallback_score(event_topic: str, event_payload: dict, context_tags: Optional[list[str]] = None) -> SalienceScore:
    """Rule-based salience when subagent is unavailable.

    Mirrors the existing default_salience() logic but as a standalone function.
    """
    t = event_topic
    tags = context_tags or []

    # High-value signals
    if t == "L0.circadian.tick":
        base = 0.1
    elif t.startswith("L0.circadian."):
        base = 0.7
    elif t.startswith("L1.sleep."):
        base = 0.8
    elif t.startswith("L2.memory."):
        base = 0.85
    elif t.startswith("L9."):
        base = 0.95
    elif t.startswith("L6.metacognition."):
        base = 0.8
    elif t.startswith("L5."):
        base = 0.65
    elif t.startswith("L7."):
        base = 0.7
    elif t.startswith("L8."):
        base = 0.55
    elif t.startswith("L4."):
        base = 0.5
    elif t.startswith("L3."):
        base = 0.3
    else:
        base = 0.5

    # Context boost
    combined = " ".join(tags).lower()
    if any(kw in combined for kw in ["紧急", "重要", "critical", "爸爸", "问题"]):
        base = min(1.0, base + 0.15)

    return SalienceScore(
        score=min(1.0, base),
        reasoning=f"fallback(topic={t})",
        override=False,
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SALIENT_PROMPT = """你是 anan 的 Working Memory Salience 评估器。

## 待评估事件
TOPIC: {topic}
PAYLOAD: {payload}

## 当前上下文标签
CONTEXT_TAGS: {context_tags}

## 评估标准
salience 0.0-1.0:
- 0.1: 背景心跳事件（如 L0.circadian.tick）
- 0.3: 低优先级内部事件
- 0.5: 普通事件
- 0.7: 重要状态变化（如睡眠阶段切换、目标达成）
- 0.85: 记忆持久化、因果链路发现
- 0.95: 身份/自我相关事件（L9）
- 1.0: 紧急/关键事件

override: 如果觉得这个事件明显比平时重要或不重要，返回 override=true

## 输出格式（严格 JSON）
{{
  "score": 0.0-1.0,
  "reasoning": "判断理由（1-2句）",
  "override": true|false
}}"""


# ---------------------------------------------------------------------------
# Advisor
# ---------------------------------------------------------------------------

class SalienceAdvisor:
    """Subagent for event salience scoring.

    Usage:
        advisor = SalienceAdvisor(delegate_fn=delegate_task)
        score = await advisor.score(
            event_topic="L5.prediction.confirmed",
            event_payload={"cause": "X", "effect": "Y", "lift": 2.3},
            context_tags=["causal", "learning"],
        )
    """

    def __init__(self, delegate_fn: Optional[callable] = None):
        self._delegate_fn = delegate_fn

    def set_delegate(self, fn: callable) -> None:
        self._delegate_fn = fn

    async def score(
        self,
        event_topic: str,
        event_payload: Optional[dict] = None,
        context_tags: Optional[list[str]] = None,
    ) -> SalienceScore:
        """Score an event's salience for Working Memory."""
        payload_str = json.dumps(event_payload or {}, ensure_ascii=False, default=str)[:200]
        prompt = SALIENT_PROMPT.format(
            topic=event_topic,
            payload=payload_str,
            context_tags=json.dumps(context_tags or [], ensure_ascii=False),
        )

        if not self._delegate_fn:
            return fallback_score(event_topic, event_payload, context_tags)

        try:
            result_text = await self._delegate_fn(
                goal="事件 salience 评估 — Working Memory",
                context=prompt,
                skills=["agent"],
            )
            return self._parse_response(result_text, event_topic, event_payload, context_tags)
        except Exception as exc:
            logger.warning("SalienceAdvisor subagent failed: %s, fallback", exc)
            return fallback_score(event_topic, event_payload, context_tags)

    def _parse_response(
        self,
        text: str,
        event_topic: str,
        event_payload: Optional[dict],
        context_tags: Optional[list[str]],
    ) -> SalienceScore:
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                return SalienceScore(
                    score=float(data.get("score", 0.5)),
                    reasoning=data.get("reasoning", ""),
                    override=bool(data.get("override", False)),
                )
            except (json.JSONDecodeError, ValueError):
                pass

        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                return SalienceScore(
                    score=float(data.get("score", 0.5)),
                    reasoning=data.get("reasoning", ""),
                    override=bool(data.get("override", False)),
                )
            except (json.JSONDecodeError, ValueError):
                pass

        logger.warning("SalienceAdvisor: could not parse: %s", text[:200])
        return fallback_score(event_topic, event_payload, context_tags)
