"""
L9 Self — Self Evaluator Layer
================================
周期性评估 anan 全系统健康状态，发布 L9.self.evaluation 事件。

这是 L9 SelfModel 的"自我评估"组件：
- SelfModel: 管事实存储（handler）
- SelfEvaluator: 管健康评估（subagent）

每隔 N 个 circadian tick 做一次评估，综合：
- L6 metacognition.report → health score
- L7 goal_engine → active goals / avg progress
- L5 pattern_miner → recent pattern count
- SelfModel → identity / wisdom fact counts

评估结果发布 L9.self.evaluation 事件，供其他层消费或 debug。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from kernel.event_bus import Event, EventBus, get_bus
from layers.L9_self.self_evaluation_advisor import SelfEvaluationAdvisor, SelfEvaluation

logger = logging.getLogger("anan.L9.evaluator")


class SelfEvaluator:
    """L9 self-evaluation layer — runs periodic overall health assessments.

    Usage:
        evaluator = SelfEvaluator(bus=bus, self_model=model)
        evaluator.set_delegate(delegate_task)
        await evaluator.attach()

    Events published:
        - L9.self.evaluation: full SelfEvaluation result (JSON-serializable payload)
    """

    def __init__(
        self,
        bus: Optional[EventBus] = None,
        self_model=None,
        tick_interval: int = 6,  # evaluate every N circadian ticks
    ):
        self._bus = bus or get_bus()
        self._model = self_model  # SelfModel data class (for fact counts)
        self._tick_interval = tick_interval
        self._tick_count = 0
        self._last_eval: Optional[SelfEvaluation] = None
        self._last_eval_time: float = 0.0
        self._unsub: list[Callable[[], None]] = []
        self._active = False

        # Subagent
        self._advisor = SelfEvaluationAdvisor()

        # State snapshot collected from events
        self._health_score: Optional[float] = None
        self._issue_count = 0
        self._active_goals = 0
        self._avg_progress = 0.0
        self._completed_milestones = 0
        self._patterns_recent = 0

    def set_delegate(self, fn: Callable) -> None:
        """Inject delegate_task for SelfEvaluationAdvisor subagent calls."""
        self._advisor.set_delegate(fn)

    @property
    def is_attached(self) -> bool:
        return self._active

    @property
    def last_evaluation(self) -> Optional[SelfEvaluation]:
        return self._last_eval

    async def attach(self) -> None:
        if self._active:
            return
        self._active = True

        # Collect state from these events
        self._unsub.append(
            self._bus.subscribe("L6.metacognition.report", self._on_l6_report)
        )
        self._unsub.append(
            self._bus.subscribe("L6.metacognition.warn", self._on_l6_warn)
        )
        self._unsub.append(
            self._bus.subscribe("L7.goal.progress_updated", self._on_goal_progress)
        )
        self._unsub.append(
            self._bus.subscribe("L7.goal.achieved", self._on_goal_achieved)
        )
        self._unsub.append(
            self._bus.subscribe("L7.goal.milestone_completed", self._on_milestone)
        )
        self._unsub.append(
            self._bus.subscribe("L5.pattern.discovered", self._on_pattern)
        )
        # Trigger evaluation on circadian tick
        self._unsub.append(
            self._bus.subscribe("L0.circadian.tick", self._on_tick)
        )
        logger.info("SelfEvaluator attached (tick_interval=%d)", self._tick_interval)

    async def detach(self) -> None:
        for u in self._unsub:
            u()
        self._unsub.clear()
        self._active = False

    # ------------------------------------------------------------------ event handlers
    async def _on_l6_report(self, event: Event) -> None:
        payload = event.payload or {}
        score = payload.get("score")
        if score is not None:
            self._health_score = float(score)

    async def _on_l6_warn(self, event: Event) -> None:
        # Count unique warn events as unresolved issues
        self._issue_count += 1

    async def _on_goal_progress(self, event: Event) -> None:
        # Update running tally of goal state
        # When L7.goal.progress_updated fires, we know goals are being tracked
        # We just count it as one active goal signal
        payload = event.payload or {}
        progress = payload.get("progress", 0.0)
        self._active_goals = max(self._active_goals, 1)
        # Maintain running average
        n = max(self._active_goals, 1)
        self._avg_progress = (self._avg_progress * (n - 1) + progress) / n

    async def _on_goal_achieved(self, event: Event) -> None:
        self._active_goals = max(0, self._active_goals - 1)

    async def _on_milestone(self, event: Event) -> None:
        self._completed_milestones += 1

    async def _on_pattern(self, event: Event) -> None:
        self._patterns_recent += 1

    async def _on_tick(self, event: Event) -> None:
        self._tick_count += 1
        if self._tick_count % self._tick_interval == 0:
            await self._do_evaluate()

    # ------------------------------------------------------------------ evaluation
    async def _do_evaluate(self) -> None:
        """Run self-evaluation and publish result."""
        try:
            identity_count = 0
            wisdom_count = 0
            if self._model is not None:
                identity_count = len(getattr(self._model, 'identity_facts', []))
                wisdom_count = len(getattr(self._model, 'wisdom_facts', []))

            evaluation = await self._advisor.evaluate(
                health_score=self._health_score,
                issue_count=self._issue_count,
                active_goals=self._active_goals,
                avg_progress=self._avg_progress,
                completed_milestones=self._completed_milestones,
                patterns_recent=self._patterns_recent,
                identity_count=identity_count,
                wisdom_count=wisdom_count,
            )

            self._last_eval = evaluation
            self._last_eval_time = time.time()
            self._issue_count = 0  # reset after evaluating

            # Publish evaluation result
            await self._bus.publish(Event(
                topic="L9.self.evaluation",
                source="L9.evaluator",
                payload={
                    "overall_score": evaluation.overall_score,
                    "health_dimension": evaluation.health_dimension,
                    "goal_dimension": evaluation.goal_dimension,
                    "pattern_dimension": evaluation.pattern_dimension,
                    "identity_dimension": evaluation.identity_dimension,
                    "status_label": evaluation.status_label,
                    "top_strengths": evaluation.top_strengths,
                    "top_concerns": evaluation.top_concerns,
                    "recommendations": evaluation.recommendations,
                    "reasoning": evaluation.reasoning,
                    "identity_count": identity_count,
                    "wisdom_count": wisdom_count,
                    "tick_count": self._tick_count,
                },
            ))

            logger.info(
                "L9.self.evaluation: overall=%.1f (%s) | health=%.1f goal=%.1f "
                "pattern=%.1f identity=%.1f | strengths=%s",
                evaluation.overall_score, evaluation.status_label,
                evaluation.health_dimension, evaluation.goal_dimension,
                evaluation.pattern_dimension, evaluation.identity_dimension,
                evaluation.top_strengths[:2],
            )
        except Exception as exc:
            logger.debug("L9 self-evaluation failed (non-fatal): %s", exc)
