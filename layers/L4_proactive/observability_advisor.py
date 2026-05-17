"""
L4 ProactiveObserver — Observability Advisor (Subagent)
========================================================
评估通用意图的满足状态（无专用 probe 时使用）。

设计原则：
- Handler: ProactiveObserver 管 probe 调度和事件广播
- Subagent: 给定意图描述 + 系统上下文，判断意图是否被满足

数据流：
  ProactiveObserver.observe_now()
    → 无专用 probe 的 intent
      → ObservabilityAdvisor.evaluate(intent_key, description, ctx)
        → delegate_task LLM 判断
          → 意图满足状态（verified/falsified/inconclusive）
            → ProactiveObserver._react() → L4.observation.* 事件

为什么用 subagent：
- "多和爸爸聊天" / "记住 sinoclaw 项目" 这种模糊意图，启发式无法判断
- subagent 能结合事件历史、working_memory、self_model 上下文综合推理
- fallback 保证 LLM 不可用时也能用启发式勉强运转
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from layers.L4_proactive.protocols import ProbeContext, ProbeResult

logger = logging.getLogger("anan.L4.observability_advisor")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class OBSResult:
    """Verdict + evidence for an intent observation."""
    verdict: str           # "verified" | "falsified" | "inconclusive"
    evidence: str          # human-readable explanation
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

_OBS_EVALUATION_PROMPT = """\
你是一个意图求证专家（Intent Verifier）。

给定一个意图（Intent）和系统当前状态，判断该意图当前是否被满足。

## 意图
- key: {intent_key}
- description: {intent_description}

## 系统状态
### 最近事件（最近 {limit} 条）
{recent_events}

### Working Memory 顶层 Layer 占比
{wm_top_layers}

### Self-Model Identity Facts
identity_facts 数量: {identity_count}

## 判断规则
- **verified**: 有明确证据表明意图已被满足（持续做/做到了/避免了）
- **falsified**: 有明确证据表明意图未被满足（未做/反而恶化了）
- **inconclusive**: 证据不足以判断（数据不足/意图过于模糊/时间太短）

## 输出格式
严格返回以下 JSON（不要有其他内容）：
{{
  "verdict": "verified" | "falsified" | "inconclusive",
  "evidence": "你的判断理由（1-2句话）",
  "detail": {{"relevant_events": ["事件列表"], "reason": "判断依据"}}
}}

请直接输出 JSON，不要 markdown 包裹。\
"""


# ---------------------------------------------------------------------------
# Fallback (rule-based heuristics — same as existing probe_catchall)
# ---------------------------------------------------------------------------

def fallback_observe(intent_key: str, intent_description: str, ctx: ProbeContext) -> OBSResult:
    """Rule-based fallback when subagent is unavailable.

    Mirrors the existing probe_catchall logic:
      - keep_triggering_X  → seen in recent bus history → verified
      - avoid_X            → NOT seen in recent bus history → verified
      - keep_doing_X       → seen in recent bus history → verified
    Falls back to inconclusive when heuristics can't determine.
    """
    history = ctx.bus.history(limit=30)

    if intent_key.startswith("keep_triggering_"):
        target = intent_key.replace("keep_triggering_", "").replace("_", ".")
        found = any(
            target in e.topic or target in str(e.payload)
            for e in history
        )
        if found:
            return OBSResult("verified", f"最近 30 事件中出现过 {target}")
        return OBSResult("inconclusive", f"最近 30 事件中未出现 {target}，需更多信息")

    if intent_key.startswith("avoid_"):
        action = intent_key.replace("avoid_", "").replace("_", ".")
        found = any(
            action in e.topic or action in str(e.payload)
            for e in history
        )
        if not found:
            return OBSResult("verified", f"{action} 未出现，避开了")
        return OBSResult("falsified", f"{action} 仍在发生")

    if intent_key.startswith("keep_doing_"):
        action = intent_key.replace("keep_doing_", "").replace("_", ".")
        found = any(
            action in e.topic or action in str(e.payload)
            for e in history
        )
        if found:
            return OBSResult("verified", f"{action} 仍在做")
        return OBSResult("inconclusive", f"{action} 最近未观察到")

    return OBSResult("inconclusive", "无法判断（无启发式规则匹配）")


# ---------------------------------------------------------------------------
# Advisor
# ---------------------------------------------------------------------------

class ObservabilityAdvisor:
    """Subagent for evaluating intent satisfaction when no built-in probe exists.

    Usage:
        advisor = ObservabilityAdvisor(delegate_fn=delegate_task)
        advisor.set_delegate(delegate_task_fn)   # injected by MindStackRunner
        result = await advisor.evaluate(intent_key, description, ctx)

    The evaluate() method matches the llm_probe_fn signature expected by
    ProactiveObserver, so it can be passed directly as llm_probe_fn.
    """

    def __init__(self, delegate_fn: Optional[callable] = None):
        self._delegate_fn = delegate_fn

    def set_delegate(self, fn: callable) -> None:
        """MindStackRunner calls this to inject the async delegate."""
        self._delegate_fn = fn

    async def evaluate(
        self,
        intent_key: str,
        intent_description: str,
        ctx: ProbeContext,
    ) -> ProbeResult:
        """Evaluate whether an intent has been satisfied.

        This method signature matches llm_probe_fn expected by ProactiveObserver,
        so it can be used directly: llm_probe_fn=advisor.evaluate
        """
        # Build context summary for LLM
        limit = 30
        recent_events = self._summarize_history(ctx.bus, limit)
        wm_info = self._summarize_wm(ctx.working_memory)
        identity_count = self._get_identity_count(ctx.self_model)

        prompt = _OBS_EVALUATION_PROMPT.format(
            intent_key=intent_key,
            intent_description=intent_description,
            recent_events=recent_events or "（无最近事件）",
            wm_top_layers=wm_info or "（无 WM 数据）",
            identity_count=identity_count,
            limit=limit,
        )

        if not self._delegate_fn:
            logger.debug("ObservabilityAdvisor: no delegate_fn, using fallback")
            obs = fallback_observe(intent_key, intent_description, ctx)
            return ProbeResult(verdict=obs.verdict, evidence=obs.evidence, detail=obs.detail)

        try:
            result_text = await self._delegate_fn(
                goal="intent 求证评估",
                context=prompt,
                parent_agent=None,
            )
            parsed = self._parse_response(result_text)
            logger.info(
                "ObservabilityAdvisor: key=%s → verdict=%s evidence=%s",
                intent_key[:40], parsed.verdict, parsed.evidence[:60],
            )
            return ProbeResult(
                verdict=parsed.verdict,
                evidence=parsed.evidence,
                detail=parsed.detail,
            )
        except Exception as exc:
            logger.warning(
                "ObservabilityAdvisor subagent failed: %s, falling back", exc,
            )
            obs = fallback_observe(intent_key, intent_description, ctx)
            return ProbeResult(verdict=obs.verdict, evidence=obs.evidence, detail=obs.detail)

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    def _summarize_history(self, bus, limit: int = 30) -> str:
        """Build a readable summary of recent bus events."""
        if bus is None:
            return ""
        try:
            history = bus.history(limit=limit)
        except Exception:
            return ""
        lines = []
        for entry in history:
            topic = entry.topic
            payload_short = str(entry.payload)[:60]
            lines.append(f"  - [{topic}] {payload_short}")
        return "\n".join(lines) if lines else ""

    def _summarize_wm(self, wm) -> str:
        """Summarize top layer share in working memory."""
        if wm is None:
            return ""
        try:
            snapshot = wm.snapshot()
            if not snapshot:
                return "（WM 为空）"
            layer_counts = Counter(e.event.topic.split(".")[0] for e in snapshot)
            total = sum(layer_counts.values())
            top_layer, top_count = layer_counts.most_common(1)[0]
            top_share = top_count / total
            return (
                f"top layer: {top_layer} 占 {top_share:.0%} "
                f"(共 {total} 条记忆, {len(layer_counts)} 个层)"
            )
        except Exception:
            return ""

    def _get_identity_count(self, sm) -> int:
        """Get identity_facts count from self_model."""
        if sm is None:
            return 0
        try:
            return len(sm.identity_facts)
        except Exception:
            return 0

    @staticmethod
    def _parse_response(text: str) -> OBSResult:
        """Parse subagent text response into OBSResult."""
        # Strategy 1: ```json ... ```
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                return ObservabilityAdvisor._from_data(data)
            except json.JSONDecodeError:
                pass

        # Strategy 2: raw {...}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                return ObservabilityAdvisor._from_data(data)
            except json.JSONDecodeError:
                pass

        # Strategy 3: field-by-field extraction
        verdict_m = re.search(r'"verdict"\s*:\s*"([^"]+)"', text)
        evidence_m = re.search(r'"evidence"\s*:\s*"([^"]+)"', text)
        detail_m = re.search(r'"detail"\s*:\s*(\{[^}]+\})', text)
        if verdict_m and evidence_m:
            detail = {}
            if detail_m:
                try:
                    detail = json.loads(detail_m.group(1))
                except Exception:
                    pass
            return OBSResult(
                verdict=verdict_m.group(1),
                evidence=evidence_m.group(1),
                detail=detail,
            )

        # Fallback: inconclusive
        return OBSResult(
            "inconclusive",
            f"无法解析 LLM 响应（返回内容：{text[:100]}）",
        )

    @staticmethod
    def _from_data(data: dict) -> OBSResult:
        """Build OBSResult from parsed dict, with validation."""
        verdict = data.get("verdict", "inconclusive")
        if verdict not in ("verified", "falsified", "inconclusive"):
            verdict = "inconclusive"
        return OBSResult(
            verdict=verdict,
            evidence=data.get("evidence", ""),
            detail=data.get("detail", {}),
        )
