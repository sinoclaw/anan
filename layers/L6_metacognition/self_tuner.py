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

Usage:
    tuner = SelfTuner(
        bus=bus,
        predictor=predictive_reasoner,
        regulator=regulator,
    )
    await tuner.attach()   # subscribes to L5/L6/L7 事件
    report = tuner.suggest()  # 打印调参建议（不自动执行，危险操作需人类审批）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.L6.self_tuner")


# Default thresholds
DEFAULT_MIN_LIFT = 1.5
DEFAULT_HORIZON_S = 3.0


@dataclass
class TuningAction:
    layer: str        # "L5" or "L7"
    target: str       # e.g. "min_lift", "horizon_s"
    old_value: float
    new_value: float
    reason: str


class SelfTuner:
    """L6 元认知自我调参器。

    分析 L5/L7 的历史表现，自动建议（或执行）参数调整。
    所有调整默认不自动执行，通过 suggest() 报告给人工审批。
    设置 auto_apply=True 可自动执行（生产环境建议关闭）。

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
        auto_apply: bool = False,            # DANGEROUS: 自动执行调参
    ):
        self._bus = bus or get_bus()
        self._pred = predictor
        self._miner = pattern_miner
        self._reg = regulator
        self._acc_low = accuracy_low_threshold
        self._acc_high = accuracy_high_threshold
        self._lift_step = min_lift_adjust_step
        self._horizon_step = horizon_adjust_step
        self._auto = auto_apply

        self._unsub: list[Callable[[], None]] = []

        # Tuning state
        self._suggestions: list[TuningAction] = []
        self._applied: list[TuningAction] = []

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    async def attach(self) -> None:
        async def on_warning(event: Event):
            await self._on_meta_warning(event)

        async def on_report(event: Event):
            await self._on_meta_report(event)

        # SelfTuner responds to warn events (PredictionMonitor accuracy alerts)
        self._unsub.append(
            self._bus.subscribe("L6.metacognition.warn", on_warning)
        )
        # Also track report events (Mirror health reports) for trend analysis
        self._unsub.append(
            self._bus.subscribe("L6.metacognition.report", on_report)
        )

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

        # If healthy but with suggestions, consider applying them proactively
        if score >= 0.6 and suggestions:
            logger.debug(
                "SelfTuner: health report score=%.2f, %d suggestions available",
                score, len(suggestions),
            )
        # If unhealthy, trigger full tuning review
        elif score < 0.6 and issues:
            logger.info("SelfTuner: unhealthy report (score=%.2f), reviewing tuning", score)
            await self._tune_l5_for_accuracy()

    async def _on_meta_warning(self, event: Event) -> None:
        """收到 L6 告警时，分析是否需要调参。"""
        issues: list[str] = event.payload.get("issues", [])
        logger.debug("SelfTuner received warning: %s", issues)

        # Parse issue and trigger appropriate tuning
        for issue in issues:
            if "预测准确率" in issue or "prediction accuracy" in issue.lower():
                await self._tune_l5_for_accuracy()
            if "链路" in issue or "link" in issue.lower():
                await self._review_stale_links()

    # ------------------------------------------------------------------
    # Tuning logic
    # ------------------------------------------------------------------

    async def _tune_l5_for_accuracy(self) -> None:
        """根据 L5 准确率调整预测参数。"""
        if self._pred is None:
            return

        stats = self._pred.stats()
        accuracy = stats.get("accuracy", 1.0)
        min_lift = getattr(self._pred, "_min_lift", DEFAULT_MIN_LIFT)
        horizon = getattr(self._pred, "_horizon_s", DEFAULT_HORIZON_S)

        actions: list[TuningAction] = []

        if accuracy < self._acc_low:
            # 准确率太低 → 提高 min_lift（更保守）+ 延长 horizon
            new_lift = min_lift + self._lift_step
            new_horizon = horizon + self._horizon_step

            actions.append(TuningAction(
                layer="L5", target="min_lift",
                old_value=min_lift, new_value=new_lift,
                reason=f"准确率 {accuracy:.0%} 低于阈值 {self._acc_low:.0%}，提高置信度门槛",
            ))
            if new_horizon <= 15.0:   # cap at 15s
                actions.append(TuningAction(
                    layer="L5", target="horizon_s",
                    old_value=horizon, new_value=new_horizon,
                    reason=f"延长预测窗口以减少误判为 failed 的好预测",
                ))

        elif accuracy > self._acc_high:
            # 准确率很高但可能预测量少 → 降低 min_lift（更激进）
            pending = stats.get("pending", 0)
            if pending < 3:
                new_lift = max(1.0, min_lift - self._lift_step)
                actions.append(TuningAction(
                    layer="L5", target="min_lift",
                    old_value=min_lift, new_value=new_lift,
                    reason=f"准确率 {accuracy:.0%} 高但预测量少，降低门槛释放更多预测",
                ))

        for action in actions:
            self._suggestions.append(action)
            logger.info(
                "SelfTuner suggestion: %s %s %.2f → %.2f (%s)",
                action.layer, action.target,
                action.old_value, action.new_value, action.reason,
            )

            if self._auto:
                await self._apply(action)

    async def _review_stale_links(self) -> None:
        """检查是否有链路被过度衰减，给予复活机会。"""
        if self._pred is None:
            return

        stale_count = 0
        for (cause, effect), link in list(self._pred._links.items()):
            lift = getattr(link, "probability_boost", None)
            if lift is not None and lift < 0.5:
                # 重置为阈值的 60%
                new_lift = max(0.5, self._acc_low * 0.6)
                action = TuningAction(
                    layer="L5", target=f"link_lift:{cause[:20]}→{effect[:20]}",
                    old_value=lift, new_value=new_lift,
                    reason="链路 lift < 0.5 已衰减过度，给予复活机会",
                )
                self._suggestions.append(action)
                logger.info("SelfTuner: reviving stale link %.2f → %.2f", lift, new_lift)

                if self._auto:
                    link.probability_boost = new_lift
                    link.confidence = max(link.confidence, 0.2)
                stale_count += 1

        if stale_count > 0:
            logger.info("SelfTuner revived %d stale links", stale_count)

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    async def _apply(self, action: TuningAction) -> None:
        """执行一个调参动作。"""
        if action.layer == "L5" and self._pred is not None:
            if action.target == "min_lift":
                # 同步更新 PredictiveReasoner（影响新预测）
                self._pred._min_lift = action.new_value
                # 同步更新 PatternMiner（影响新挖掘结果）
                if self._miner is not None:
                    self._miner.set_min_lift(action.new_value)
            elif action.target == "horizon_s":
                self._pred._horizon_s = action.new_value
            elif action.target.startswith("link_lift:"):
                # link already modified in _review_stale_links when auto=True
                pass

        self._applied.append(action)
        logger.info(
            "SelfTuner APPLIED: %s %s %.2f → %.2f",
            action.layer, action.target,
            action.old_value, action.new_value,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def suggest(self) -> str:
        """返回所有待执行的调参建议（供人类审批）。"""
        if not self._suggestions:
            return "SelfTuner: 暂无调参建议"

        lines = ["SelfTuner 调参建议:"]
        for i, a in enumerate(self._suggestions, 1):
            lines.append(
                f"  [{i}] {a.layer}.{a.target}: "
                f"{a.old_value:.2f} → {a.new_value:.2f}"
            )
            lines.append(f"      原因: {a.reason}")

        if self._applied:
            lines.append(f"\n已自动执行: {len(self._applied)} 项")
        if not self._auto:
            lines.append("\n(自动执行已关闭，如需开启设置 auto_apply=True)")

        return "\n".join(lines)

    def clear_suggestions(self) -> None:
        """清除所有建议。"""
        self._suggestions.clear()

    def stats(self) -> dict:
        return {
            "suggestions_pending": len(self._suggestions),
            "actions_applied": len(self._applied),
            "auto_apply": self._auto,
        }
