"""
L6 SelfTuner — 元认知自我调参
==============================

anan 的"不知道自己不知道"问题：
  - L5 min_lift 设太低 → 太多噪音预测 → 链路衰减过快
  - L5 min_lift 设太高 → 错过有效链路 → 预测率低
  - L5 horizon_s 设太短 → 很多好预测被误判为 failed
  - L7 SelfRegulator 的衰减率太激进 → 好链路被错误衰减

SelfTuner 的工作方式：
  当 L6 元认知认为"某层行为异常"时，自动调整该层的参数。
  不需要 LLM — 用启发式规则 + 历史数据做决策。

  触发条件：
    - L5 准确率 < accuracy_threshold（持续 window 个预测）
      → 提高 min_lift（更保守，只发高置信度预测）
      → 延长 horizon_s（给效果更多时间发生）

    - L5 准确率很高（> 0.9）但预测量少（< 5）
      → 降低 min_lift（更激进，释放更多预测能力）

    - 链路被过度衰减（lift < 0.5）
      → 重置 lift 到初始值的 50%（给该链路第二次机会）

  审批机制：
    - 调参建议进入 pending_actions 队列
    - 发布 L6.tuning.pending 事件（供人工审批工具消费）
    - 通过 approve(action_id) 或 reject(action_id) 决策
    - approved 的 action 才执行 _apply()

Usage:
    tuner = SelfTuner(
        bus=bus,
        predictor=predictive_reasoner,
        pattern_miner=pattern_miner,
    )
    await tuner.attach()
    # 查询待审批
    tuner.pending_report()  # → str
    # 审批
    await tuner.approve(tuner.pending_actions[0].id)
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Optional

from kernel.event_bus import Event, EventBus, get_bus
from layers.L6_metacognition.tuning_advisor import (
    MetacognitionAdvisor,
    MetricsTracker,
    TuningEvaluation,
)

logger = logging.getLogger("anan.L6.self_tuner")


# Default thresholds
DEFAULT_MIN_LIFT = 1.5
DEFAULT_HORIZON_S = 3.0


class TuningStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"


@dataclass
class TuningAction:
    id: str                      # 唯一 ID，用于 approve/reject
    layer: str        # "L5" or "L7"
    target: str       # e.g. "min_lift", "horizon_s"
    old_value: float
    new_value: float
    reason: str
    status: TuningStatus = TuningStatus.PENDING
    created_at: str = ""   # ISO format, auto-filled by _make_action


def _parse_age_seconds(iso_timestamp: str) -> float:
    """Parse an ISO timestamp to age in seconds. Returns 0 if unparseable."""
    if not iso_timestamp:
        return float('inf')
    try:
        created = datetime.fromisoformat(iso_timestamp)
        return (datetime.now() - created).total_seconds()
    except Exception:
        return float('inf')


class SelfTuner:
    """L6 元认知自我调参器。

    分析 L5/L7 的历史表现，自动建议参数调整。
    所有调整必须经过审批才执行，通过 pending_actions 队列管理。

    需要注入 PredictiveReasoner（用于_decay_link）和 PatternMiner（用于 set_min_lift）。
    """

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        predictor: Optional[object] = None,   # PredictiveReasoner
        pattern_miner: Optional[object] = None,  # PatternMiner — for writing back min_lift
        regulator: Optional[object] = None,  # SelfRegulator
        # Thresholds for triggering tuning
        accuracy_low_threshold: float = 0.35,
        accuracy_high_threshold: float = 0.90,
        min_lift_adjust_step: float = 0.2,
        horizon_adjust_step: float = 0.5,
        # 超过这个秒数的 pending actions 自动 approve（0 = 禁用）
        auto_approve_age_s: float = 60.0,
    ):
        self._bus = bus or get_bus()
        self._pred = predictor
        self._miner = pattern_miner
        self._reg = regulator
        self._acc_low = accuracy_low_threshold
        self._acc_high = accuracy_high_threshold
        self._lift_step = min_lift_adjust_step
        self._horizon_step = horizon_adjust_step
        self._auto_approve_age = auto_approve_age_s

        self._unsub: list[Callable[[], None]] = []
        print(f"[L6 DIAG] SelfTuner.__init__ called, bus={id(self._bus)}", flush=True)

        # Tuning state
        self._pending: list[TuningAction] = []
        self._applied: list[TuningAction] = []
        self._rejected: list[TuningAction] = []

        # Metrics tracker + subagent advisor for tuning evaluation
        self._metrics_tracker = MetricsTracker()
        self._advisor = MetacognitionAdvisor(
            metrics_tracker=self._metrics_tracker,
            applied_history=self._applied,
        )

    def set_delegate(self, fn: callable) -> None:
        """Inject delegate_task for MetacognitionAdvisor subagent calls."""
        self._advisor.set_delegate(fn)

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    async def attach(self) -> None:
        async def on_warning(event: Event):
            await self._on_meta_warning(event)

        async def on_report(event: Event):
            await self._on_meta_report(event)

        self._unsub.append(
            self._bus.subscribe("L6.metacognition.warn", on_warning)
        )
        self._unsub.append(
            self._bus.subscribe("L6.metacognition.report", on_report)
        )

        print(f"[L6 DIAG] SelfTuner.attach() called, bus={id(self._bus)}", flush=True)
        async def on_pattern(event: Event):
            print(f"[L6 DIAG] SelfTuner on_pattern received event!", flush=True)
            logger.warning(f"[L6 SelfTuner] _on_pattern_discovered received: topic={event.topic}, payload={event.payload}")
            await self._on_pattern_discovered(event)

        self._unsub.append(
            self._bus.subscribe("L5.pattern.discovered", on_pattern)
        )
        print("[L6 DIAG] SelfTuner attach() done", flush=True)
        logger.warning("[L6 SelfTuner] attach() done, subscribed to L5.pattern.discovered")

    async def detach(self) -> None:
        for u in self._unsub:
            u()
        self._unsub.clear()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_meta_report(self, event: Event) -> None:
        """Track health report trends — adjust if stagnation or decline detected."""
        payload = event.payload or {}
        score = payload.get("score", 1.0)
        issues = payload.get("issues", [])
        suggestions = payload.get("suggestions", [])

        if score >= 0.6 and suggestions:
            logger.debug(
                "SelfTuner: health report score=%.2f, %d suggestions available",
                score, len(suggestions),
            )
        elif score < 0.6 and issues:
            logger.info("SelfTuner: unhealthy report (score=%.2f), reviewing tuning", score)
            await self._tune_l5_for_accuracy()
        # 自动清理陈旧的 pending actions
        await self._housekeeping()

    async def _on_meta_warning(self, event: Event) -> None:
        """收到 L6 告警时，分析是否需要调参。"""
        issues: list[str] = event.payload.get("issues", [])
        logger.debug("SelfTuner received warning: %s", issues)

        for issue in issues:
            if "预测准确率" in issue or "prediction accuracy" in issue.lower():
                await self._tune_l5_for_accuracy()
            if "链路" in issue or "link" in issue.lower():
                await self._review_stale_links()

        # 自动清理：超时的 pending actions 直接 approve
        await self._housekeeping()

    async def _on_pattern_discovered(self, event: Event) -> None:
        """L5 发现了因果规律 → 据此调整预测链路参数。

        策略：
        - lift > 8：高置信度规律，boost 对应链路 probability_boost
        - lift > 12：极强规律，降低 min_lift 让 L5 更激进挖掘
        """
        p = event.payload or {}
        antecedent = p.get("antecedent", "")
        consequent = p.get("consequent", "")
        lift = p.get("lift", 0.0)
        summary = p.get("summary", "")

        if not antecedent or not consequent:
            return

        logger.info(
            "SelfTuner received pattern: %s → %s (lift=%.2f): %s",
            antecedent, consequent, lift, summary,
        )

        # 高 lift 规律 → boost 该链路的 probability_boost
        if lift > 8.0:
            link_key = f"link_lift:{antecedent[:20]}→{consequent[:20]}"
            # 查当前该链路的 probability_boost（默认 1.0）
            current_boost = 1.0
            if self._pred is not None:
                for (cause, effect), link in getattr(self._pred, "_links", {}).items():
                    if antecedent[:20] in cause and consequent[:20] in effect:
                        current_boost = getattr(link, "probability_boost", 1.0)
                        break

            boost = min(current_boost + 0.3, 3.0)  # 最多加到 3.0
            action = self._make_action(
                layer="L5",
                target=link_key,
                old_value=current_boost,
                new_value=boost,
                reason=f"L5 发现强规律: {summary}",
            )
            self._pending.append(action)
            await self._bus.publish(Event(
                topic="L6.tuning.pending",
                source="L6.self_tuner",
                payload={"action_id": action.id, "reason": summary},
            ))
            logger.info(
                "SelfTuner: queued tuning action [%s] boost link %s→%s by %.2f (lift=%.1f)",
                action.id, antecedent[:20], consequent[:20], boost - current_boost, lift,
            )

        # 极高 lift 规律 → 建议降低 min_lift 更激进挖掘
        if lift > 12.0 and self._pred is not None:
            current_min_lift = getattr(self._pred, "_min_lift", 1.5)
            if current_min_lift > 1.3:
                new_min_lift = max(1.3, current_min_lift - 0.2)
                action = self._make_action(
                    layer="L5",
                    target="min_lift",
                    old_value=current_min_lift,
                    new_value=new_min_lift,
                    reason=f"L5 发现极强规律 (lift={lift:.1f})，适当放宽置信度门槛以捕获更多相关预测",
                )
                self._pending.append(action)
                await self._bus.publish(Event(
                    topic="L6.tuning.pending",
                    source="L6.self_tuner",
                    payload={"action_id": action.id, "reason": f"极强规律放宽 min_lift: {lift:.1f}x"},
                ))
                logger.info(
                    "SelfTuner: queued [%s] reduce min_lift %.2f → %.2f (lift=%.1f)",
                    action.id, current_min_lift, new_min_lift, lift,
                )

        await self._housekeeping()

    async def _housekeeping(self) -> None:
        """清理超时的 pending actions，防止队列膨胀。"""
        if self._auto_approve_age <= 0:
            return
        if not self._pending:
            return
        expired = [
            a for a in self._pending
            if _parse_age_seconds(a.created_at) > self._auto_approve_age
        ]
        if not expired:
            return
        logger.info(
            "SelfTuner: auto-approving %d stale pending actions (age > %.0fs)",
            len(expired), self._auto_approve_age,
        )
        for action in expired:
            self._pending.remove(action)
            await self._apply(action)

    # ------------------------------------------------------------------
    # Tuning logic
    # ------------------------------------------------------------------

    def _make_action(
        self,
        layer: str,
        target: str,
        old_value: float,
        new_value: float,
        reason: str,
    ) -> TuningAction:
        return TuningAction(
            id=str(uuid.uuid4())[:8],
            layer=layer,
            target=target,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
            created_at=datetime.now().isoformat(),
        )

    async def _tune_l5_for_accuracy(self) -> None:
        """根据 L5 准确率调整预测参数。"""
        if self._pred is None:
            return

        stats = self._pred.stats()
        accuracy = stats.get("accuracy", 1.0)
        min_lift = getattr(self._pred, "_min_lift", DEFAULT_MIN_LIFT)
        horizon = getattr(self._pred, "_horizon_s", DEFAULT_HORIZON_S)

        new_actions: list[TuningAction] = []

        if accuracy < self._acc_low:
            new_lift = min_lift + self._lift_step
            new_horizon = horizon + self._horizon_step

            new_actions.append(self._make_action(
                "L5", "min_lift", min_lift, new_lift,
                f"准确率 {accuracy:.0%} 低于阈值 {self._acc_low:.0%}，提高置信度门槛"
            ))
            if new_horizon <= 15.0:
                new_actions.append(self._make_action(
                    "L5", "horizon_s", horizon, new_horizon,
                    "延长预测窗口以减少误判为 failed 的好预测"
                ))

        elif accuracy > self._acc_high:
            pending = stats.get("pending", 0)
            if pending < 3:
                new_lift = max(1.0, min_lift - self._lift_step)
                new_actions.append(self._make_action(
                    "L5", "min_lift", min_lift, new_lift,
                    f"准确率 {accuracy:.0%} 高但预测量少，降低门槛释放更多预测"
                ))

        for action in new_actions:
            self._pending.append(action)
            logger.info(
                "SelfTuner pending: [%s] %s %s %.2f → %.2f (%s)",
                action.id, action.layer, action.target,
                action.old_value, action.new_value, action.reason,
            )
            # 发事件通知审批工具
            await self._bus.publish(Event(
                topic="L6.tuning.pending",
                source="L6.self_tuner",
                payload={
                    "action_id": action.id,
                    "layer": action.layer,
                    "target": action.target,
                    "old_value": action.old_value,
                    "new_value": action.new_value,
                    "reason": action.reason,
                    "created_at": action.created_at,
                    "pending_count": len(self._pending),
                },
            ))

    async def _review_stale_links(self) -> None:
        """检查是否有链路被过度衰减，给予复活机会。"""
        if self._pred is None:
            return

        stale_count = 0
        for (cause, effect), link in list(self._pred._links.items()):
            lift = getattr(link, "probability_boost", None)
            if lift is not None and lift < 0.5:
                new_lift = max(0.5, self._acc_low * 0.6)
                action = self._make_action(
                    "L5", f"link_lift:{cause[:20]}→{effect[:20]}", lift, new_lift,
                    "链路 lift < 0.5 已衰减过度，给予复活机会"
                )
                self._pending.append(action)
                logger.info("SelfTuner pending: [%s] reviving stale link %.2f → %.2f",
                             action.id, lift, new_lift)
                await self._bus.publish(Event(
                    topic="L6.tuning.pending",
                    source="L6.self_tuner",
                    payload={
                        "action_id": action.id,
                        "layer": action.layer,
                        "target": action.target,
                        "old_value": action.old_value,
                        "new_value": action.new_value,
                        "reason": action.reason,
                        "created_at": action.created_at,
                        "pending_count": len(self._pending),
                    },
                ))
                stale_count += 1

        if stale_count > 0:
            logger.info("SelfTuner: %d stale link revive actions pending", stale_count)

    # ------------------------------------------------------------------
    # Approval API — 审批接口
    # ------------------------------------------------------------------

    async def approve(self, action_id: str) -> bool:
        """批准一个调参动作并执行。"""
        action = next((a for a in self._pending if a.id == action_id), None)
        if action is None:
            logger.warning("SelfTuner: action %s not found in pending", action_id)
            return False

        action.status = TuningStatus.APPROVED
        self._pending.remove(action)
        await self._apply(action)
        return True

    async def reject(self, action_id: str) -> bool:
        """拒绝一个调参动作。"""
        action = next((a for a in self._pending if a.id == action_id), None)
        if action is None:
            logger.warning("SelfTuner: action %s not found in pending", action_id)
            return False

        action.status = TuningStatus.REJECTED
        self._pending.remove(action)
        self._rejected.append(action)
        logger.info("SelfTuner rejected: [%s] %s %s", action_id, action.layer, action.target)
        return True

    async def approve_all(self) -> int:
        """批准所有待定动作。"""
        count = 0
        for action in list(self._pending):
            if await self.approve(action.id):
                count += 1
        return count

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    async def _apply(self, action: TuningAction) -> None:
        """执行一个调参动作，然后通过 MetacognitionAdvisor 评估效果。"""
        # 记录调参前的指标快照
        self._metrics_tracker.record_before(
            predictor=self._pred,
            pending_count=len(self._pending),
        )

        # 执行调参
        if action.layer == "L5" and self._pred is not None:
            if action.target == "min_lift":
                self._pred._min_lift = action.new_value
                if self._miner is not None:
                    self._miner.set_min_lift(action.new_value)
            elif action.target == "horizon_s":
                self._pred._horizon_s = action.new_value
            elif action.target.startswith("link_lift:"):
                # Find and update the specific link
                for (cause, effect), link in list(self._pred._links.items()):
                    key = f"link_lift:{cause[:20]}→{effect[:20]}"
                    if key == action.target:
                        link.probability_boost = action.new_value
                        link.confidence = max(link.confidence, 0.2)
                        break

        action.status = TuningStatus.APPLIED
        self._applied.append(action)
        logger.info(
            "SelfTuner APPLIED: [%s] %s %s %.2f → %.2f",
            action.id, action.layer, action.target,
            action.old_value, action.new_value,
        )

        # 记录调参后的指标快照
        after_snap = self._metrics_tracker.snapshot(
            predictor=self._pred,
            pending_count=len(self._pending),
        )

        # 通过 subagent 评估效果
        evaluation = await self._advisor.evaluate(action)

        # 发布评估结果事件
        await self._bus.publish(Event(
            topic="L6.tuning.evaluated",
            source="L6.self_tuner",
            payload={
                "action_id": action.id,
                **evaluation.to_dict(),
            },
        ))

        # 如果 advisor 建议 rollback，自动执行
        if evaluation.rollback_recommended:
            logger.warning(
                "SelfTuner: advisor recommends ROLLBACK for [%s] — %s",
                action.id, evaluation.reasoning,
            )
            action.status = TuningStatus.APPLIED  # mark as applied before rollback
            # Revert
            if action.layer == "L5" and self._pred is not None:
                if action.target == "min_lift":
                    self._pred._min_lift = action.old_value
                    if self._miner is not None:
                        self._miner.set_min_lift(action.old_value)
                elif action.target == "horizon_s":
                    self._pred._horizon_s = action.old_value
            # Queue rollback event
            await self._bus.publish(Event(
                topic="L6.tuning.rollback",
                source="L6.self_tuner",
                payload={
                    "action_id": action.id,
                    "target": action.target,
                    "restored_value": action.old_value,
                    "reasoning": evaluation.reasoning,
                },
            ))

        # 发布 applied 通知（原有的）
        await self._bus.publish(Event(
            topic="L6.tuning.applied",
            source="L6.self_tuner",
            payload={
                "action_id": action.id,
                "layer": action.layer,
                "target": action.target,
                "new_value": action.new_value,
            },
        ))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def pending_actions(self) -> list[TuningAction]:
        """返回所有待审批的动作。"""
        return list(self._pending)

    def pending_report(self) -> str:
        """返回待审批动作的报告（供人类/工具查看）。"""
        if not self._pending:
            return "SelfTuner: 暂无待审批调参"

        lines = [f"SelfTuner: {len(self._pending)} 个待审批调参:"]
        for a in self._pending:
            lines.append(
                f"  [{a.id}] {a.layer}.{a.target}: "
                f"{a.old_value:.2f} → {a.new_value:.2f}"
            )
            lines.append(f"      原因: {a.reason}")
            lines.append(f"      时间: {a.created_at}")
        return "\n".join(lines)

    def clear_pending(self) -> None:
        """清除所有待审批动作（不推荐，会丢失调参机会）。"""
        self._pending.clear()

    def stats(self) -> dict:
        return {
            "pending": len(self._pending),
            "applied": len(self._applied),
            "rejected": len(self._rejected),
        }
