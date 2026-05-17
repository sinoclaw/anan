"""
L1SleepAdvisor — Subagent for DreamingPlugin phase planning.

Responsible for LLM-driven decisions about:
  1. Which data sources to prioritize in light sleep given recent activity
  2. Whether REM phase is warranted (cross-session pattern potential)
  3. How aggressive deep sleep promotion should be (thresholds, limits)
  4. Narrative style hints for the dream diary entry

Falls back to rule-based heuristics when delegate is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("anan.L1.advisor")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class PhaseDecision(str, Enum):
    """What L1 should do for the current phase."""
    EXPAND = "expand"      # Go deeper: more data, more iterations, richer narrative
    NORMAL = "normal"      # Standard run
    REDUCE = "reduce"      # Abbreviated: fewer items, shorter narrative
    SKIP = "skip"          # Not worth running this phase right now


class NarrativeStyle(str, Enum):
    """Dream narrative tone/style suggestions."""
    REFLECTIVE = "reflective"    # Quiet, contemplative
    CURIOUS = "curious"          # Exploratory, questioning
    WHIMSICAL = "whimsical"      # Playful, surprising connections
    TECHNICAL = "technical"       # Code and systems metaphors
    SENSORY = "sensory"          # Rich sensory detail


@dataclass
class SleepPhaseContext:
    """Context about the current sleep/dream cycle."""
    phase: str                      # "light" | "rem" | "deep"
    workspace_dir: str
    recent_session_count: int = 0    # sessions in last lookback window
    recent_memory_lines: int = 0     # daily memory lines added recently
    recall_entries_total: int = 0    # short-term recall store size
    recall_entries_promoted: int = 0 # already promoted to long-term
    last_light_run_days_ago: float = 999.0
    last_rem_run_days_ago: float = 999.0
    last_deep_run_days_ago: float = 999.0
    light_sources_active: list[str] = field(default_factory=lambda: ["daily", "sessions", "recall"])
    narrative_history: list[str] = field(default_factory=list)  # recent diary snippets


@dataclass
class SleepPhaseAdvice:
    """Output from L1SleepAdvisor — guidance for the current phase."""
    decision: PhaseDecision
    narrative_style: NarrativeStyle
    source_weights: dict[str, float]        # e.g. {"sessions": 1.2, "recall": 0.8}
    deep_limit_override: Optional[int] = None
    deep_min_score_override: Optional[float] = None
    skip_reason: str = ""
    evidence: str = ""
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Rule-based fallback advisor
# ---------------------------------------------------------------------------

def _fallback_advice(ctx: SleepPhaseContext) -> SleepPhaseAdvice:
    """Rule-based fallback when LLM is unavailable."""
    now = datetime.now()

    if ctx.phase == "light":
        # If light ran recently, reduce scope
        if ctx.last_light_run_days_ago < 0.5:
            decision = PhaseDecision.REDUCE
            reason = "Light sleep ran within 12h"
        elif ctx.recent_session_count > 20:
            decision = PhaseDecision.EXPAND
            reason = "High session activity — expand ingestion"
        else:
            decision = PhaseDecision.NORMAL
            reason = "Normal session activity"

        # Weight sessions more if active
        weights = {"daily": 0.8, "sessions": 1.0, "recall": 0.9}
        if ctx.recent_session_count > 15:
            weights["sessions"] = 1.4
            weights["recall"] = 1.2

        style = NarrativeStyle.REFLECTIVE
        if ctx.narrative_history:
            last = ctx.narrative_history[-1]
            if len(last) > 200:
                style = NarrativeStyle.TECHNICAL

    elif ctx.phase == "rem":
        # REM weekly is usually warranted unless nothing happened
        if ctx.last_rem_run_days_ago < 6:
            decision = PhaseDecision.SKIP
            reason = f"REM ran {ctx.last_rem_run_days_ago:.1f} days ago"
        elif ctx.recent_session_count < 3:
            decision = PhaseDecision.REDUCE
            reason = "Not enough sessions for cross-patterns"
        else:
            decision = PhaseDecision.NORMAL
            reason = "Weekly REM warranted"
        weights = {"memory": 1.0, "daily": 0.7, "sessions": 0.9}
        style = NarrativeStyle.CURIOUS

    else:  # deep
        # Adjust promotion aggressiveness based on recall pressure
        if ctx.recall_entries_total == 0:
            decision = PhaseDecision.SKIP
            reason = "No short-term recalls to promote"
        elif ctx.recall_entries_total > 50 and ctx.recall_entries_promoted < 5:
            decision = PhaseDecision.EXPAND
            reason = "Recall backlog high — expand promotion"
            weights = {"recall": 1.5}
        elif ctx.last_deep_run_days_ago < 1:
            decision = PhaseDecision.REDUCE
            reason = "Deep sleep ran today"
        else:
            decision = PhaseDecision.NORMAL
            reason = "Normal promotion"
        weights = {}
        style = NarrativeStyle.SENSORY

    return SleepPhaseAdvice(
        decision=decision,
        narrative_style=style,
        source_weights=weights,
        skip_reason=reason,
        evidence=f"Fallback: {reason}",
        detail={
            "fallback": True,
            "recall_total": ctx.recall_entries_total,
            "recent_sessions": ctx.recent_session_count,
        },
    )


# ---------------------------------------------------------------------------
# Main advisor
# ---------------------------------------------------------------------------

class L1SleepAdvisor:
    """LLM-driven sleep phase advisor with rule-based fallback.

    Usage:
        advisor = L1SleepAdvisor()
        advisor.set_delegate(delegate_fn)   # MindStackRunner injects this
        advice = await advisor.evaluate(ctx)
    """

    def __init__(self, adaptation_history: Optional[list] = None):
        self._delegate_fn: Optional[Callable] = None
        self._adaptation_history = adaptation_history or []

    def set_delegate(self, fn: Callable) -> None:
        """MindStackRunner calls this to inject the async delegate."""
        self._delegate_fn = fn

    async def evaluate(self, ctx: SleepPhaseContext) -> SleepPhaseAdvice:
        """Evaluate the sleep phase and return structured advice.

        Tries LLM first (via delegate_task), falls back to rule-based advisor.
        """
        if self._delegate_fn is None:
            return _fallback_advice(ctx)

        prompt = self._build_prompt(ctx)

        try:
            result = await self._delegate_fn(
                goal="L1SleepAdvisor: decide sleep phase strategy",
                context=prompt,
            )
            advice = self._parse_result(result, ctx)
            if advice:
                return advice
        except Exception as exc:
            logger.debug("L1SleepAdvisor LLM call failed: %s", exc)

        return _fallback_advice(ctx)

    def _build_prompt(self, ctx: SleepPhaseContext) -> str:
        recent_narratives = (
            "\n".join(f"  - {s[:100]}" for s in ctx.narrative_history[-3:])
            or "  (no recent narratives)"
        )
        return f"""You are the L1 Sleep Advisor for anan's dreaming system.

You must respond in JSON with this exact structure:
{{"decision": "expand|normal|reduce|skip", "narrative_style": "reflective|curious|whimsical|technical|sensory", "source_weights": {{"daily": 1.0, "sessions": 1.0, "recall": 1.0}}, "deep_limit_override": null, "deep_min_score_override": null, "skip_reason": "", "evidence": "why you decided this", "detail": {{}}}}

## Current Phase
{ctx.phase.upper()}

## Workspace
{ctx.workspace_dir}

## Recent Activity
- Sessions in lookback window: {ctx.recent_session_count}
- Daily memory lines recently: {ctx.recent_memory_lines}
- Short-term recall store: {ctx.recall_entries_total} entries total, {ctx.recall_entries_promoted} already promoted
- Last light sleep: {ctx.last_light_run_days_ago:.1f} days ago
- Last REM sleep: {ctx.last_rem_run_days_ago:.1f} days ago
- Last deep sleep: {ctx.last_deep_run_days_ago:.1f} days ago
- Active sources: {ctx.light_sources_active}

## Recent Dream Narratives (last 3)
{recent_narratives}

## Your Task
Decide:
1. **decision**: Should this phase be expanded, run normally, reduced, or skipped?
2. **narrative_style**: What tone should the dream diary entry take?
3. **source_weights**: For light sleep — weight each source (1.0 = normal, >1 = more attention, <1 = less)
4. **deep_limit_override**: For deep sleep — override the default promotion limit (null = use config)
5. **deep_min_score_override**: For deep sleep — override min promotion score (null = use config)

Consider:
- Light: High session count → weight sessions; recent narratives guide style
- REM: Runs weekly; skip if ran < 6 days ago; reduce if few sessions
- Deep: Skip if no recalls; expand if recall backlog is high

Respond ONLY with the JSON object, no commentary."""

    def _parse_result(self, result: Any, ctx: SleepPhaseContext) -> Optional[SleepPhaseAdvice]:
        """Parse LLM result into SleepPhaseAdvice."""
        text: Optional[str] = None
        if isinstance(result, dict):
            text = result.get("content") or result.get("text") or result.get("response")
        elif isinstance(result, str):
            text = result

        if not text:
            return None

        # Extract JSON from the response
        import json as _json
        try:
            # Try direct parse first
            data = _json.loads(text)
        except Exception:
            # Try extracting from markdown code block
            import re
            m = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
            if m:
                try:
                    data = _json.loads(m.group(0))
                except Exception:
                    return None
            else:
                return None

        try:
            decision = PhaseDecision(data.get("decision", "normal"))
        except ValueError:
            decision = PhaseDecision.NORMAL

        try:
            style = NarrativeStyle(data.get("narrative_style", "reflective"))
        except ValueError:
            style = NarrativeStyle.REFLECTIVE

        return SleepPhaseAdvice(
            decision=decision,
            narrative_style=style,
            source_weights=data.get("source_weights", {}),
            deep_limit_override=data.get("deep_limit_override"),
            deep_min_score_override=data.get("deep_min_score_override"),
            skip_reason=data.get("skip_reason", ""),
            evidence=data.get("evidence", ""),
            detail=data.get("detail", {}),
        )
