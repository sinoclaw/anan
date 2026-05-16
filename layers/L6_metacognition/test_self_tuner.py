"""
L6 SelfTuner 测试套件
======================

覆盖 SelfTuner 的：
  - 初始化参数
  - 准确率低 → 提高 min_lift + 延长 horizon
  - 准确率高但预测量少 → 降低 min_lift
  - stale link 复活
  - pending_report() 报告
  - approve()/reject() 审批流程
  - approve_all() 批量审批
  - stats()
"""

import pytest

from kernel.event_bus import EventBus, Event
from layers.L6_metacognition.self_tuner import (
    SelfTuner, TuningAction, TuningStatus, DEFAULT_MIN_LIFT, DEFAULT_HORIZON_S,
)


class MockPredictor:
    """Mock PredictiveReasoner with tunable _min_lift and _horizon_s."""

    def __init__(self):
        self._min_lift = 1.5
        self._horizon_s = 3.0
        self._links: dict = {}

        class FakeLink:
            def __init__(self, lift=1.2, conf=0.5):
                self.probability_boost = lift
                self.confidence = conf

        self._links[("A", "B")] = FakeLink(2.0, 0.8)
        self._links[("X", "Y")] = FakeLink(0.3, 0.1)  # stale
        self._links[("P", "Q")] = FakeLink(0.4, 0.1)  # stale

    def stats(self):
        return {
            "accuracy": 0.25,   # low — triggers tuning
            "pending": 2,
        }


# --------------------------------------------------------------------------


class TestInit:
    def test_defaults(self):
        st = SelfTuner()
        assert st._acc_low == 0.35
        assert st._acc_high == 0.90
        assert st._lift_step == 0.2
        assert st._horizon_step == 0.5

    def test_custom_params(self):
        st = SelfTuner(
            accuracy_low_threshold=0.4,
            accuracy_high_threshold=0.85,
            min_lift_adjust_step=0.3,
            horizon_adjust_step=1.0,
        )
        assert st._acc_low == 0.4
        assert st._acc_high == 0.85
        assert st._lift_step == 0.3
        assert st._horizon_step == 1.0


class TestTuneForAccuracy:
    @pytest.mark.asyncio
    async def test_low_accuracy_raises_min_lift(self):
        bus = EventBus()
        pred = MockPredictor()
        pred._min_lift = 1.5
        st = SelfTuner(bus=bus, predictor=pred)
        await st.attach()

        await bus.publish(Event(
            topic="L6.metacognition.warn",
            source="test",
            payload={"issues": ["预测准确率严重低迷"]},
        ))

        # Should have pending actions
        assert len(st._pending) >= 1
        lift_action = next((a for a in st._pending if a.target == "min_lift"), None)
        assert lift_action is not None
        assert lift_action.new_value > lift_action.old_value   # 1.5 → 1.7
        assert lift_action.status == TuningStatus.PENDING
        assert lift_action.id != ""
        await st.detach()

    @pytest.mark.asyncio
    async def test_high_accuracy_low_volume_lowers_lift(self):
        bus = EventBus()
        pred = MockPredictor()
        pred._min_lift = 1.5
        pred._horizon_s = 3.0
        st = SelfTuner(bus=bus, predictor=pred)
        # Override stats to simulate high accuracy + low pending
        pred.stats = lambda: {"accuracy": 0.95, "pending": 1}
        await st.attach()

        await bus.publish(Event(
            topic="L6.metacognition.warn",
            source="test",
            payload={"issues": ["预测准确率 95% 高但预测量少"]},
        ))

        lift_action = next((a for a in st._pending if a.target == "min_lift"), None)
        assert lift_action is not None
        assert lift_action.new_value < lift_action.old_value   # 1.5 → 1.3
        await st.detach()


class TestStaleLinks:
    @pytest.mark.asyncio
    async def test_review_stale_links(self):
        bus = EventBus()
        pred = MockPredictor()
        st = SelfTuner(bus=bus, predictor=pred)

        await st._review_stale_links()

        # 2 stale links found (X→Y and P→Q with lift < 0.5)
        assert len(st._pending) == 2
        for a in st._pending:
            assert a.status == TuningStatus.PENDING
        await st.detach()


class TestApproval:
    @pytest.mark.asyncio
    async def test_approve_executes_action(self):
        bus = EventBus()
        pred = MockPredictor()
        pred._min_lift = 1.5
        st = SelfTuner(bus=bus, predictor=pred)
        await st.attach()

        # Trigger tuning to create pending action
        await st._tune_l5_for_accuracy()
        assert len(st._pending) >= 1
        action = st._pending[0]
        old_val = action.old_value

        # Approve it
        result = await st.approve(action.id)
        assert result is True
        assert action.status == TuningStatus.APPLIED
        assert action in st._applied
        assert action not in st._pending

        await st.detach()

    @pytest.mark.asyncio
    async def test_reject_removes_action(self):
        bus = EventBus()
        pred = MockPredictor()
        st = SelfTuner(bus=bus, predictor=pred)
        await st.attach()

        await st._tune_l5_for_accuracy()
        assert len(st._pending) >= 1
        action = st._pending[0]

        result = await st.reject(action.id)
        assert result is True
        assert action.status == TuningStatus.REJECTED
        assert action in st._rejected
        assert action not in st._pending

        await st.detach()

    @pytest.mark.asyncio
    async def test_approve_unknown_id_returns_false(self):
        st = SelfTuner()
        result = await st.approve("does-not-exist")
        assert result is False

    @pytest.mark.asyncio
    async def test_approve_all(self):
        bus = EventBus()
        pred = MockPredictor()
        pred._min_lift = 1.5
        st = SelfTuner(bus=bus, predictor=pred)
        await st.attach()

        await st._tune_l5_for_accuracy()
        await st._review_stale_links()
        initial_pending = len(st._pending)
        assert initial_pending >= 1

        count = await st.approve_all()
        assert count == initial_pending
        assert len(st._pending) == 0
        assert len(st._applied) == initial_pending

        await st.detach()


class TestPendingReport:
    def test_empty_report(self):
        st = SelfTuner()
        assert "暂无" in st.pending_report()

    @pytest.mark.asyncio
    async def test_report_shows_pending(self):
        bus = EventBus()
        pred = MockPredictor()
        st = SelfTuner(bus=bus, predictor=pred)
        await st.attach()

        await st._tune_l5_for_accuracy()

        report = st.pending_report()
        assert "L5" in report
        assert "min_lift" in report
        assert "pending" in report.lower() or "待审批" in report
        await st.detach()

    def test_clear_pending(self):
        st = SelfTuner()
        st._pending.append(
            TuningAction(id="test", layer="L5", target="min_lift",
                         old_value=1.5, new_value=1.7, reason="test")
        )
        st.clear_pending()
        assert len(st._pending) == 0


class TestStats:
    def test_stats(self):
        st = SelfTuner()
        s = st.stats()
        assert s["pending"] == 0
        assert s["applied"] == 0
        assert s["rejected"] == 0
        assert "auto_apply" not in s  # removed in new API

    @pytest.mark.asyncio
    async def test_stats_after_approve(self):
        bus = EventBus()
        pred = MockPredictor()
        pred._min_lift = 1.5
        st = SelfTuner(bus=bus, predictor=pred)
        await st.attach()

        await st._tune_l5_for_accuracy()
        initial_pending = len(st._pending)
        assert initial_pending >= 1

        await st.approve_all()
        s = st.stats()
        assert s["pending"] == 0
        assert s["applied"] == initial_pending

        await st.detach()
