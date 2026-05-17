"""
L6 Metacognition — Tuning Advisor (Subagent)
=============================================
补充 SelfTuner 的调参建议评估：判断 tuning action 是否有效、是否需要 rollback。

设计原则：
- Handler: SelfTuner 管理 pending actions 和 applied history
- Subagent: 给定调参历史 + 当前指标，评估调参效果、建议 rollback 或调整阈值

为什么需要这个：
- SelfTuner 生成 pending actions，但没有机制评估效果
- auto_approve 只处理超时，不处理"调了之后有没有用"
- Mirror 的阈值是静态的，系统行为变了阈值就不适用了

数据流：
  SelfTuner._apply() → 发布 L6.tuning.applied
    → MetacognitionAdvisor 订阅
    → subagent 评估效果
    → 若有害：建议 rollback + 调整 Mirror/PM 阈值
    → 发布 L6.tuning.evaluation 事件
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger("anan.L6.tuning_advisor")

# ---------------------------------------------------------------------------
# Tuning Evaluation Result
# ---------------------------------------------------------------------------

@dataclass
class TuningEvaluation:
    action_id: str
    effective: bool          # 调参是否有效
    reasoning: str           # 判断理由
    rollback_recommended: bool = False
    new_suggestion: Optional[str] = None   # 替代方案

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "effective": self.effective,
            "reasoning": self.reasoning,
            "rollback_recommended": self.rollback_recommended,
            "new_suggestion": self.new_suggestion,
        }


# ---------------------------------------------------------------------------
# Subagent prompt
# ---------------------------------------------------------------------------

TUNING_EVALUATION_PROMPT = """你是一个 AI 系统元认知调参顾问。为已执行的调参动作评估效果，并决定是否需要 rollback。

## 你的任务
分析以下调参记录和系统指标，判断这次调参是否有效，是否需要回滚或调整阈值。

## 调参记录
TUNING_ACTION:
- ID: {action_id}
- 层级: {layer}
- 目标参数: {target}
- 旧值: {old_value}
- 新值: {new_value}
- 原因: {reason}
- 执行时间: {applied_at}

## 系统指标变化
METRICS_AFTER:
- 准确率变化: {accuracy_before} → {accuracy_after}
- 预测量变化: {predictions_before} → {predictions_after}
- 链路数量变化: {links_before} → {links_after}
- pending actions 队列长度: {pending_count}

## 最近 applied actions 历史
APPLIED_HISTORY:
{applied_history}

## 评估标准
1. 调参后准确率提升 ≥ 5% → 有效
2. 调参后准确率下降 ≥ 5% → 有害，建议 rollback
3. 调参后 pending actions 持续堆积 → 阈值可能太激进，建议放宽
4. 连续3次同方向调参 → 可能陷入震荡，建议暂停该方向
5. 新值超出合理范围（如 min_lift > 3.0 或 < 1.0）→ 立即 rollback

## 输出格式（严格 JSON）
{{
  "effective": true|false,
  "reasoning": "判断理由（1-3句）",
  "rollback_recommended": true|false,
  "new_suggestion": "替代方案描述，或null"
}}"""


# ---------------------------------------------------------------------------
# Fallback handler
# ---------------------------------------------------------------------------

def fallback_evaluate(action, metrics_before, metrics_after, applied_history) -> TuningEvaluation:
    """Rule-based evaluation when subagent is unavailable.

    Args:
        metrics_before: MetricsSnapshot or dict with accuracy field
        metrics_after: MetricsSnapshot or dict with accuracy field
        applied_history: list of TuningAction

    Strategy:
    - If accuracy improved: effective=True
    - If accuracy dropped > 5%: rollback_recommended=True
    - If new_value out of reasonable range: rollback_recommended=True
    """
    acc_before = getattr(metrics_before, "accuracy", 0.5) if metrics_before else 0.5
    acc_after = getattr(metrics_after, "accuracy", 0.5) if metrics_after else 0.5
    delta = acc_after - acc_before

    old_val = action.old_value
    new_val = action.new_value
    target = action.target

    # Out of reasonable range check
    if target == "min_lift" and (new_val > 3.0 or new_val < 1.0):
        return TuningEvaluation(
            action_id=action.id,
            effective=False,
            reasoning=f"Fallback: min_lift={new_val} 超出合理范围 [1.0, 3.0]",
            rollback_recommended=True,
            new_suggestion=f"恢复 min_lift={old_val}",
        )

    if target == "horizon_s" and new_val > 20.0:
        return TuningEvaluation(
            action_id=action.id,
            effective=False,
            reasoning=f"Fallback: horizon_s={new_val}s 过长",
            rollback_recommended=True,
            new_suggestion=f"恢复 horizon_s={old_val}",
        )

    if delta >= 0.05:
        return TuningEvaluation(
            action_id=action.id,
            effective=True,
            reasoning=f"Fallback: 准确率提升 {delta:+.1%}",
        )

    if delta <= -0.05:
        return TuningEvaluation(
            action_id=action.id,
            effective=False,
            reasoning=f"Fallback: 准确率下降 {delta:+.1%}",
            rollback_recommended=True,
            new_suggestion=f"恢复 {target}={old_val}",
        )

    # No significant change
    return TuningEvaluation(
        action_id=action.id,
        effective=True,
        reasoning=f"Fallback: 准确率变化 {delta:+.1%}，无显著影响，维持现状",
    )


# ---------------------------------------------------------------------------
# Metrics Tracker (handler side, records state snapshots)
# ---------------------------------------------------------------------------

@dataclass
class MetricsSnapshot:
    timestamp: str
    accuracy: float
    predictions: int
    links: int
    pending_count: int = 0

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "accuracy": round(self.accuracy, 4),
            "predictions": self.predictions,
            "links": self.links,
            "pending_count": self.pending_count,
        }


class MetricsTracker:
    """Handler-side metrics tracker. Records snapshots before/after tuning.

    Used by MetacognitionAdvisor to fetch pre/post comparison data.
    """

    def __init__(self):
        self._snapshots: list[MetricsSnapshot] = []
        self._before_tuning: Optional[MetricsSnapshot] = None

    def snapshot(self, predictor=None, pending_count: int = 0) -> MetricsSnapshot:
        """Capture current metrics snapshot."""
        acc = 0.5
        preds = 0
        links = 0

        if predictor is not None:
            try:
                stats = predictor.stats()
                acc = stats.get("accuracy", 0.5)
                preds = stats.get("total_predictions", 0)
            except Exception:
                pass
            try:
                links = len(getattr(predictor, "_links", {}))
            except Exception:
                pass

        snap = MetricsSnapshot(
            timestamp=datetime.now().isoformat(),
            accuracy=acc,
            predictions=preds,
            links=links,
            pending_count=pending_count,
        )
        self._snapshots.append(snap)
        return snap

    def record_before(self, predictor=None, pending_count: int = 0) -> None:
        """Record snapshot BEFORE a tuning action is applied."""
        self._before_tuning = self.snapshot(predictor, pending_count)

    def get_before(self) -> Optional[MetricsSnapshot]:
        return self._before_tuning

    def latest(self) -> Optional[MetricsSnapshot]:
        return self._snapshots[-1] if self._snapshots else None


# ---------------------------------------------------------------------------
# Metacognition Advisor (subagent wrapper)
# ---------------------------------------------------------------------------

class MetacognitionAdvisor:
    """Subagent for evaluating tuning action effectiveness.

    Usage:
        advisor = MetacognitionAdvisor(
            delegate_fn=delegate_task,
            metrics_tracker=tracker,
        )
        # SelfTuner._apply() 之前：tracker.record_before()
        # SelfTuner._apply() 之后：advisor.evaluate(action)
    """

    def __init__(
        self,
        delegate_fn: Optional[callable] = None,
        metrics_tracker: Optional[MetricsTracker] = None,
        applied_history: Optional[list] = None,
    ):
        self._delegate_fn = delegate_fn
        self._tracker = metrics_tracker or MetricsTracker()
        self._applied_history = applied_history or []

    def set_delegate(self, fn: callable) -> None:
        self._delegate_fn = fn

    async def evaluate(self, action) -> TuningEvaluation:
        """Evaluate a tuning action's effectiveness using subagent.

        Compares metrics_before (captured at record_before) vs current metrics.
        Falls back to rule-based evaluation if subagent unavailable.
        """
        metrics_before = self._tracker.get_before()
        metrics_after = self._tracker.latest()

        # Build applied history text
        history_lines = []
        for a in self._applied_history[-5:]:
            history_lines.append(
                f"  - [{a.id}] {a.layer} {a.target}: {a.old_value} → {a.new_value} "
                f"({a.reason[:40]})"
            )
        applied_history_text = "\n".join(history_lines) or "  (无历史)"

        # Build metrics strings
        def fmt_metrics(snap):
            if snap is None:
                return "N/A"
            return f"accuracy={snap.accuracy:.2f}, predictions={snap.predictions}, links={snap.links}"

        prompt = TUNING_EVALUATION_PROMPT.format(
            action_id=action.id,
            layer=action.layer,
            target=action.target,
            old_value=action.old_value,
            new_value=action.new_value,
            reason=action.reason,
            applied_at=action.created_at,
            accuracy_before=fmt_metrics(metrics_before),
            accuracy_after=fmt_metrics(metrics_after),
            predictions_before=metrics_before.predictions if metrics_before else "N/A",
            predictions_after=metrics_after.predictions if metrics_after else "N/A",
            links_before=metrics_before.links if metrics_before else "N/A",
            links_after=metrics_after.links if metrics_after else "N/A",
            pending_count=metrics_after.pending_count if metrics_after else 0,
            applied_history=applied_history_text,
        )

        if not self._delegate_fn:
            logger.debug(
                "MetacognitionAdvisor: no delegate_fn, using fallback for action=%s",
                action.id,
            )
            return fallback_evaluate(action, metrics_before, metrics_after, self._applied_history)

        try:
            result_text = await self._delegate_fn(
                goal=f"评估调参效果: {action.target} {action.old_value}→{action.new_value}",
                context=prompt,
                skills=["agent"],
            )

            parsed = self._parse_response(result_text, action.id)
            logger.info(
                "MetacognitionAdvisor: action=%s effective=%s rollback=%s",
                action.id, parsed.effective, parsed.rollback_recommended,
            )
            return parsed

        except Exception as exc:
            logger.warning(
                "MetacognitionAdvisor subagent failed for action=%s: %s, falling back",
                action.id, exc,
            )
            return fallback_evaluate(action, metrics_before, metrics_after, self._applied_history)

    @staticmethod
    def _parse_response(text: str, action_id: str) -> TuningEvaluation:
        """Parse subagent text response into TuningEvaluation."""
        # Strategy 1: ```json ... ```
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                return MetacognitionAdvisor._from_data(data, action_id)
            except json.JSONDecodeError:
                pass

        # Strategy 2: raw {...}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                return MetacognitionAdvisor._from_data(data, action_id)
            except json.JSONDecodeError:
                pass

        logger.warning(
            "MetacognitionAdvisor: could not parse subagent response: %s",
            text[:200],
        )
        return TuningEvaluation(
            action_id=action_id,
            effective=True,
            reasoning="解析失败，维持现状",
        )

    @staticmethod
    def _from_data(data: dict, action_id: str) -> TuningEvaluation:
        return TuningEvaluation(
            action_id=action_id,
            effective=bool(data.get("effective", True)),
            reasoning=data.get("reasoning", ""),
            rollback_recommended=bool(data.get("rollback_recommended", False)),
            new_suggestion=data.get("new_suggestion"),
        )
