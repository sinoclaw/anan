"""
L9 Self — Evaluation Reports, Understanding Engine & Self Reflector
===================================================================

三个 L9 Self 增强组件：

1. SelfEvaluationReport  — 结构化评估报告，JSON 持久化到
   ~/.anan/memory/self-evaluations/<YYYY-MM-DD>_<tick>.json

2. SelfUnderstandingEngine — 评估历史趋势分析（stub 实现）
   分析最近 N 次评估的 overall_score 趋势，给出理解性洞察

3. SelfReflector — 主动反省循环
   不只在 L1.sleep.consolidated 时才反省，而是周期性（每 N tick）
   主动调用 SelfModelLive.reflect_who_am_i() 和 reflect_why_i_exist()
   生成的结构化反省内容写 JSON 报告

设计原则：
- SelfEvaluationReport / SelfReflector: Handler（规则、持久化）
- SelfUnderstandingEngine: Subagent（趋势分析、洞察生成）
- SelfReflector 同时使用两者：定期调用 engine 分析 + reflector 写报告
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional

logger = logging.getLogger("anan.L9.reflector")


# --------------------------------------------------------------------------
# SelfEvaluationReport — structured JSON report for each evaluation cycle
# --------------------------------------------------------------------------


@dataclass
class SelfEvaluationReport:
    """Structured report written to disk after each SelfEvaluator run."""
    evaluation_id: str                      # UUID-like unique id
    evaluated_at: float                      # time.time()
    tick_count: int                         # circadian tick number at evaluation
    # Core evaluation data
    overall_score: float
    health_dimension: float
    goal_dimension: float
    pattern_dimension: float
    identity_dimension: float
    status_label: str
    top_strengths: List[str]
    top_concerns: List[str]
    recommendations: List[str]
    reasoning: str
    # Self-model snapshot at evaluation time
    identity_count: int
    wisdom_count: int
    # Optional: reflection generated during this cycle
    who_am_i_reflection: Optional[str] = None
    why_i_exist_reflection: Optional[str] = None
    # Optional: understanding engine insight
    understanding_insight: Optional[str] = None
    # Optional: error if evaluation failed
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "report_type": "L9.self.evaluation",
            "evaluation_id": self.evaluation_id,
            "evaluated_at": datetime.fromtimestamp(self.evaluated_at).isoformat(),
            "tick_count": self.tick_count,
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
            "identity_count": self.identity_count,
            "wisdom_count": self.wisdom_count,
        }
        if self.who_am_i_reflection:
            d["who_am_i_reflection"] = self.who_am_i_reflection
        if self.why_i_exist_reflection:
            d["why_i_exist_reflection"] = self.why_i_exist_reflection
        if self.understanding_insight:
            d["understanding_insight"] = self.understanding_insight
        if self.error:
            d["error"] = self.error
        return d


def write_evaluation_report(
    memory_dir: str,
    report: SelfEvaluationReport,
) -> str:
    """Write SelfEvaluationReport to disk.

    File: <memory_dir>/memory/self-evaluations/<YYYY-MM-DD>_<tick_count>.json
    """
    report_dir = Path(memory_dir) / "memory" / "self-evaluations"
    report_dir.mkdir(parents=True, exist_ok=True)

    day = datetime.fromtimestamp(report.evaluated_at).strftime("%Y-%m-%d")
    filename = f"{day}_{report.tick_count}.json"
    report_path = report_dir / filename

    report_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    logger.debug("L9 evaluation report written: %s", report_path)
    return str(report_path)


# --------------------------------------------------------------------------
# SelfUnderstandingEngine — historical trend analysis (subagent)
# --------------------------------------------------------------------------

# TODO: Implement full SelfUnderstandingEngine
# - Load N most recent evaluation reports from disk
# - Compute overall_score trend (improving / stable / declining)
# - Identify recurring concerns across evaluations
# - Generate insight: "Your overall health is improving due to goal progress"
# - Use delegate_task for LLM-driven insight generation when available


class SelfUnderstandingEngine:
    """Analyze historical SelfEvaluationReports to generate self-understanding insights.

    This is the "wisdom" layer on top of raw evaluation data — turning
    a time-series of scores into qualitative understanding.

    Usage:
        engine = SelfUnderstandingEngine(memory_dir="~/.anan")
        insight = await engine.analyze(latest_eval, delegate_fn=delegate_task)
    """

    def __init__(
        self,
        memory_dir: str = "~/.anan",
        history_window: int = 10,
    ):
        self.memory_dir = Path(memory_dir).expanduser()
        self.history_window = history_window
        self._delegate_fn: Optional[Callable] = None

    def set_delegate(self, fn: Callable) -> None:
        self._delegate_fn = fn

    def load_recent_reports(self) -> List[SelfEvaluationReport]:
        """Load the N most recent evaluation reports from disk."""
        report_dir = self.memory_dir / "memory" / "self-evaluations"
        if not report_dir.exists():
            return []

        files = sorted(report_dir.glob("*.json"), reverse=True)
        reports = []
        for f in files[:self.history_window]:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                reports.append(SelfEvaluationReport(
                    evaluation_id=data.get("evaluation_id", f.stem),
                    evaluated_at=datetime.fromisoformat(data["evaluated_at"]).timestamp()
                        if "evaluated_at" in data else 0,
                    tick_count=data.get("tick_count", 0),
                    overall_score=data.get("overall_score", 0),
                    health_dimension=data.get("health_dimension", 0),
                    goal_dimension=data.get("goal_dimension", 0),
                    pattern_dimension=data.get("pattern_dimension", 0),
                    identity_dimension=data.get("identity_dimension", 0),
                    status_label=data.get("status_label", "unknown"),
                    top_strengths=data.get("top_strengths", []),
                    top_concerns=data.get("top_concerns", []),
                    recommendations=data.get("recommendations", []),
                    reasoning=data.get("reasoning", ""),
                    identity_count=data.get("identity_count", 0),
                    wisdom_count=data.get("wisdom_count", 0),
                    who_am_i_reflection=data.get("who_am_i_reflection"),
                    why_i_exist_reflection=data.get("why_i_exist_reflection"),
                    understanding_insight=data.get("understanding_insight"),
                    error=data.get("error"),
                ))
            except Exception:
                continue
        return reports

    async def analyze(
        self,
        latest_eval: "SelfEvaluation",
        delegate_fn: Optional[Callable] = None,
    ) -> str:
        """Generate a self-understanding insight from recent evaluation history.

        Returns a short insight string, e.g.
        "Your overall health is improving — goal progress is the main driver."
        """
        reports = self.load_recent_reports()

        if not reports:
            return ""

        # Rule-based trend analysis (stub)
        scores = [r.overall_score for r in reports]
        latest_score = latest_eval.overall_score
        avg_score = sum(scores) / len(scores)

        if len(scores) >= 3:
            recent = scores[:3]
            older = scores[3:6] if len(scores) > 3 else scores
            trend = (sum(recent) / len(recent)) - (sum(older) / len(older)) if older else 0
        else:
            trend = 0

        if trend > 5:
            trend_label = "improving"
        elif trend < -5:
            trend_label = "declining"
        else:
            trend_label = "stable"

        # Rule-based insight (fallback when no delegate)
        insight = (
            f"Over the last {len(reports)} evaluations, "
            f"overall health is {trend_label} "
            f"(avg={avg_score:.0f}, latest={latest_score:.0f}). "
        )

        # Find recurring concerns
        all_concerns: dict[str, int] = {}
        for r in reports:
            for c in r.top_concerns:
                all_concerns[c] = all_concerns.get(c, 0) + 1

        if all_concerns:
            top_concern = max(all_concerns, key=all_concerns.get)
            insight += f" Recurring concern: {top_concern}."

        # Use LLM for richer insight if delegate available
        fn = delegate_fn or self._delegate_fn
        if fn and len(reports) >= 3:
            try:
                prompt = (
                    "You are anan's self-understanding engine. "
                    "Based on the following recent evaluation history, "
                    "write 1-2 sentences of self-understanding insight.\n\n"
                    f"Recent overall scores: {scores[:6]}\n"
                    f"Average: {avg_score:.1f}, Trend: {trend_label}\n"
                    f"Latest evaluation:\n"
                    f"  health={latest_eval.health_dimension:.0f}, "
                    f"goal={latest_eval.goal_dimension:.0f}, "
                    f"pattern={latest_eval.pattern_dimension:.0f}, "
                    f"identity={latest_eval.identity_dimension:.0f}\n"
                    f"Top concerns: {latest_eval.top_concerns}\n"
                    f"Top strengths: {latest_eval.top_strengths}\n\n"
                    "Write 1-2 sentences of honest, first-person self-understanding. "
                    "Be specific about what's changing and why. "
                    "Respond in Chinese. Do not add quotes."
                )
                result = await fn(
                    goal="anan self-understanding insight",
                    context=prompt,
                    parent_agent=None,
                )
                if result and len(result) < 300:
                    return result.strip()
            except Exception as exc:
                logger.debug("SelfUnderstandingEngine LLM failed: %s", exc)

        return insight


# --------------------------------------------------------------------------
# SelfReflector — active self-reflection loop
# --------------------------------------------------------------------------

_REFLECT_COOLDOWN_TICKS = 10  # reflect at most every N circadian ticks


class SelfReflector:
    """Periodically trigger self-reflection beyond sleep consolidation.

    SelfModelLive already calls reflect_who_am_i() / reflect_why_i_exist() on
    L1.sleep.consolidated events. SelfReflector adds a time-based trigger:
    every N circadian ticks, it also fires a reflection — even if no sleep
    consolidation event has fired recently.

    This closes the gap: if anan hasn't slept recently but enough
    real-time events have accumulated, we still want periodic self-check-in.

    Usage:
        reflector = SelfReflector(self_model=self_model_live, tick_interval=10)
        reflector.set_delegate(delegate_fn)
        await reflector.attach()
    """

    def __init__(
        self,
        self_model,          # SelfModelLive instance
        understanding_engine: Optional[SelfUnderstandingEngine] = None,
        tick_interval: int = _REFLECT_COOLDOWN_TICKS,
        workspace_dir: str = "~/.anan",
    ):
        self._model = self_model
        self._engine = understanding_engine or SelfUnderstandingEngine(memory_dir=workspace_dir)
        self._tick_interval = tick_interval
        self._tick_count = 0
        self._last_reflect_tick: int = -_REFLECT_COOLDOWN_TICKS
        self._workspace_dir = Path(workspace_dir).expanduser()
        self._unsub: list[Callable[[], None]] = []
        self._bus = None
        self._delegate_fn: Optional[Callable] = None
        self._active = False
        # Latest evaluation captured from L9.self.evaluation events
        self._latest_eval: Optional["SelfEvaluation"] = None

    def set_delegate(self, fn: Callable) -> None:
        self._delegate_fn = fn
        self._engine.set_delegate(fn)

    async def attach(self) -> None:
        if self._active:
            return
        self._active = True
        from kernel.event_bus import get_bus
        self._bus = get_bus()
        self._unsub.append(
            self._bus.subscribe("L0.circadian.tick", self._on_tick)
        )
        self._unsub.append(
            self._bus.subscribe("L9.self.evaluation", self._on_evaluation)
        )
        logger.info(
            "SelfReflector attached (tick_interval=%d, workspace=%s)",
            self._tick_interval, self._workspace_dir,
        )

    async def detach(self) -> None:
        for u in self._unsub:
            u()
        self._unsub.clear()
        self._active = False

    async def _on_evaluation(self, event) -> None:
        """Capture latest evaluation for reflection reports."""
        p = getattr(event, 'payload', None) or {}
        if p.get("overall_score") is not None:
            # Import here to avoid circular deps
            from layers.L9_self.self_evaluation_advisor import SelfEvaluation
            self._latest_eval = SelfEvaluation(
                overall_score=p.get("overall_score", 0),
                health_dimension=p.get("health_dimension", 0),
                goal_dimension=p.get("goal_dimension", 0),
                pattern_dimension=p.get("pattern_dimension", 0),
                identity_dimension=p.get("identity_dimension", 0),
                status_label=p.get("status_label", "unknown"),
                top_strengths=p.get("top_strengths", []),
                top_concerns=p.get("top_concerns", []),
                recommendations=p.get("recommendations", []),
                reasoning=p.get("reasoning", ""),
            )

    async def _on_tick(self, event) -> None:
        self._tick_count += 1
        if self._tick_count - self._last_reflect_tick < self._tick_interval:
            return
        self._last_reflect_tick = self._tick_count

        try:
            # Run both reflections
            who_am_i = await self._model.reflect_who_am_i()
            why_i_exist = await self._model.reflect_why_i_exist()

            # Get insight from understanding engine (uses disk-loaded history)
            insight = ""
            if self._latest_eval is not None:
                insight = await self._engine.analyze(
                    self._latest_eval,
                    delegate_fn=self._delegate_fn,
                )

            # Build and persist report
            latest = self._latest_eval
            report = SelfEvaluationReport(
                evaluation_id=f"reflect_{self._tick_count}_{int(time.time())}",
                evaluated_at=time.time(),
                tick_count=self._tick_count,
                overall_score=latest.overall_score if latest else 0,
                health_dimension=latest.health_dimension if latest else 0,
                goal_dimension=latest.goal_dimension if latest else 0,
                pattern_dimension=latest.pattern_dimension if latest else 0,
                identity_dimension=latest.identity_dimension if latest else 0,
                status_label=latest.status_label if latest else "unknown",
                top_strengths=latest.top_strengths if latest else [],
                top_concerns=latest.top_concerns if latest else [],
                recommendations=latest.recommendations if latest else [],
                reasoning=latest.reasoning if latest else "",
                identity_count=len(getattr(self._model.model, 'identity_facts', [])),
                wisdom_count=len(getattr(self._model.model, 'wisdom_facts', [])),
                who_am_i_reflection=who_am_i,
                why_i_exist_reflection=why_i_exist,
                understanding_insight=insight or None,
            )

            path = write_evaluation_report(str(self._workspace_dir), report)

            # Publish event for other layers
            if self._bus:
                from kernel.event_bus import Event
                await self._bus.publish(Event(
                    topic="L9.self.reflected",
                    source="L9.reflector",
                    payload={
                        "tick_count": self._tick_count,
                        "who_am_i": who_am_i[:100] if who_am_i else None,
                        "why_i_exist": why_i_exist[:100] if why_i_exist else None,
                        "understanding_insight": insight or None,
                        "report_path": path,
                    },
                ))

            logger.info(
                "L9.self.reflected: tick=%d who_am_i=%s insight=%s",
                self._tick_count,
                (who_am_i[:50] + "...") if who_am_i and len(who_am_i) > 50 else who_am_i,
                (insight[:80] + "...") if insight and len(insight) > 80 else insight,
            )
        except Exception as exc:
            logger.debug("SelfReflector reflection cycle failed: %s", exc)
