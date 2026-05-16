"""
L6 SelfTuner 测试套件
======================

覆盖 SelfTuner 的：
  - 初始化和默认参数
  - _tune_l5_for_accuracy() 低准确率 → 创建 pending action
  - _on_meta_warning() → _tune_l5_for_accuracy()
  - _housekeeping() → auto_approve 超时 pending actions
  - approve() → _apply() → pred._min_lift 更新
  - reject() → 移入 _rejected
  - _review_stale_links() → 链路复活 pending action
  - L6.tuning.pending 事件发布
  - L6.tuning.applied 事件发布
"""

import asyncio
import pytest
from datetime import datetime, timedelta

from kernel.event_bus import EventBus, Event
from layers.L6_metacognition.self_tuner import (
    SelfTuner, TuningAction, TuningStatus, DEFAULT_MIN_LIFT, DEFAULT_HORIZON_S
)


class MockPredictor:
    """Mock PredictiveReasoner for SelfTuner tests."""

    def __init__(self, min_lift: float = 1.5, horizon_s: float = 3.0):
        self._min_lift = min_lift
        self._horizon_s = horizon_s
        self._links = {}
        self.stats_return = {"accuracy": 0.8, "confirmed": 8, "failed": 2, "pending": 1}

        # Pre-populate a stale link (lift < 0.5)
        class FakeLink:
            def __init__(self):
                self.probability_boost = 0.3  # < 0.5 → stale
                self.confidence = 0.1

        self._links[("stale_cause", "stale_effect")] = FakeLink()

    def stats(self):
        return self.stats_return


class MockPatternMiner:
    """Mock PatternMiner for set_min_lift call tracking."""

    def __init__(self):
        self.last_min_lift: float | None = None

    def set_min_lift(self, value: float):
        self.last_min_lift = value


# ---------------------------------------------------------------------------
# Init & Defaults
# ---------------------------------------------------------------------------

class TestSelfTunerInit:
    def test_defaults(self):
        st = SelfTuner()
        assert st._acc_low == 0.35
        assert st._acc_high == 0.90
        assert st._lift_step == 0.2
        assert st._horizon_step == 0.5
        assert st._auto_approve_age == 60.0
        assert st._pending == []
        assert st._applied == []

    def test_custom_params(self):
        st = SelfTuner(
            accuracy_low_threshold=0.4,
            accuracy_high_threshold=0.95,
            min_lift_adjust_step=0.3,
            horizon_adjust_step=1.0,
            auto_approve_age_s=30.0,
        )
        assert st._acc_low == 0.4
        assert st._acc_high == 0.95
        assert st._lift_step == 0.3
        assert st._horizon_step == 1.0
        assert st._auto_approve_age == 30.0


# ---------------------------------------------------------------------------
# _tune_l5_for_accuracy — low accuracy → pending action
# ---------------------------------------------------------------------------

class TestTuneL5ForAccuracy:
    @pytest.mark.asyncio
    async def test_low_accuracy_creates_pending_min_lift(self):
        bus = EventBus()
        pred = MockPredictor(min_lift=1.5)
        pred.stats_return = {"accuracy": 0.20, "confirmed": 4, "failed": 16, "pending": 0}
        miner = MockPatternMiner()
        st = SelfTuner(bus=bus, predictor=pred, pattern_miner=miner)
        await st.attach()

        pending_events = []
        bus.subscribe("L6.tuning.pending", lambda e: pending_events.append(e.payload))

        await st._tune_l5_for_accuracy()

        # Low accuracy creates both min_lift and horizon_s actions (horizon=3.5 ≤ 15.0)
        assert len(st._pending) == 2
        action = next(a for a in st._pending if a.target == "min_lift")
        assert action.layer == "L5"
        assert action.old_value == 1.5
        assert action.new_value == 1.7  # 1.5 + 0.2 step
        assert "准确率" in action.reason

        # Event published (2 events: min_lift + horizon_s)
        assert len(pending_events) == 2
        assert pending_events[0]["action_id"] == action.id

        await st.detach()

    @pytest.mark.asyncio
    async def test_low_accuracy_also_creates_horizon_action(self):
        """When accuracy < acc_low AND new_horizon <= 15.0, horizon action also created."""
        bus = EventBus()
        pred = MockPredictor(min_lift=1.5, horizon_s=3.0)
        pred.stats_return = {"accuracy": 0.20, "confirmed": 4, "failed": 16, "pending": 0}
        miner = MockPatternMiner()
        st = SelfTuner(bus=bus, predictor=pred, pattern_miner=miner)
        await st.attach()

        await st._tune_l5_for_accuracy()

        # min_lift action + horizon_s action
        assert len(st._pending) == 2
        targets = {a.target for a in st._pending}
        assert "min_lift" in targets
        assert "horizon_s" in targets

        horizon_action = next(a for a in st._pending if a.target == "horizon_s")
        assert horizon_action.old_value == 3.0
        assert horizon_action.new_value == 3.5  # 3.0 + 0.5 step

        await st.detach()

    @pytest.mark.asyncio
    async def test_high_accuracy_low_volume_creates_lower_min_lift(self):
        """accuracy > acc_high but pending < 3 → lower min_lift to release more predictions."""
        bus = EventBus()
        pred = MockPredictor(min_lift=1.5)
        pred.stats_return = {"accuracy": 0.95, "confirmed": 19, "failed": 1, "pending": 2}
        miner = MockPatternMiner()
        st = SelfTuner(bus=bus, predictor=pred, pattern_miner=miner)
        await st.attach()

        await st._tune_l5_for_accuracy()

        assert len(st._pending) == 1
        action = st._pending[0]
        assert action.target == "min_lift"
        assert action.new_value == 1.3  # 1.5 - 0.2 step

        await st.detach()

    @pytest.mark.asyncio
    async def test_no_predicator_no_crash(self):
        """_tune_l5_for_accuracy with no predictor injected should not crash."""
        bus = EventBus()
        st = SelfTuner(bus=bus)
        await st.attach()

        await st._tune_l5_for_accuracy()  # should not raise

        assert st._pending == []
        await st.detach()

    @pytest.mark.asyncio
    async def test_healthy_accuracy_creates_nothing(self):
        """accuracy in healthy range (0.35-0.90) → no pending actions."""
        bus = EventBus()
        pred = MockPredictor(min_lift=1.5)
        pred.stats_return = {"accuracy": 0.55, "confirmed": 11, "failed": 9, "pending": 3}
        st = SelfTuner(bus=bus, predictor=pred)
        await st.attach()

        await st._tune_l5_for_accuracy()

        assert st._pending == []
        await st.detach()


# ---------------------------------------------------------------------------
# _on_meta_warning → _tune_l5_for_accuracy
# ---------------------------------------------------------------------------

class TestOnMetaWarning:
    @pytest.mark.asyncio
    async def test_warning_with_prediction_accuracy_issue_triggers_tuning(self):
        bus = EventBus()
        pred = MockPredictor(min_lift=1.5)
        pred.stats_return = {"accuracy": 0.10, "confirmed": 2, "failed": 18, "pending": 0}
        st = SelfTuner(bus=bus, predictor=pred)
        await st.attach()

        await bus.publish(Event(
            topic="L6.metacognition.warn",
            source="L6.prediction_monitor",
            payload={
                "issues": ["预测准确率 10% 严重低迷（阈值 25%）"],
                "context": {"confirmed": 2, "failed": 18, "window": 20},
            },
        ))

        # Allow async handler to fire
        await asyncio.sleep(0.01)

        # Low accuracy → min_lift + horizon_s
        assert len(st._pending) == 2
        await st.detach()

    @pytest.mark.asyncio
    async def test_warning_with_link_issue_triggers_review_stale_links(self):
        bus = EventBus()
        pred = MockPredictor(min_lift=1.5)
        # stale link already in pred._links: (stale_cause, stale_effect) with lift=0.3
        st = SelfTuner(bus=bus, predictor=pred)
        await st.attach()

        await bus.publish(Event(
            topic="L6.metacognition.warn",
            source="L6.prediction_monitor",
            payload={
                "issues": ["链路效果衰减过度"],
                "context": {},
            },
        ))

        await asyncio.sleep(0.01)

        assert len(st._pending) >= 1
        # Should have revived the stale link
        revived = [a for a in st._pending if "stale_cause" in a.target]
        assert len(revived) == 1

        await st.detach()


# ---------------------------------------------------------------------------
# approve() → _apply()
# ---------------------------------------------------------------------------

class TestApprove:
    @pytest.mark.asyncio
    async def test_approve_executes_min_lift_change(self):
        bus = EventBus()
        pred = MockPredictor(min_lift=1.5)
        miner = MockPatternMiner()
        st = SelfTuner(bus=bus, predictor=pred, pattern_miner=miner)
        await st.attach()

        # Directly add a pending action (simulate what _tune_l5_for_accuracy does)
        action = st._make_action(
            layer="L5", target="min_lift",
            old_value=1.5, new_value=1.7,
            reason="测试：准确率低迷提高门槛",
        )
        st._pending.append(action)

        applied_events = []
        bus.subscribe("L6.tuning.applied", lambda e: applied_events.append(e.payload))

        result = await st.approve(action.id)

        assert result is True
        assert action.status == TuningStatus.APPLIED
        assert pred._min_lift == 1.7
        assert miner.last_min_lift == 1.7
        assert len(st._pending) == 0
        assert action in st._applied

        # Applied event published
        assert len(applied_events) == 1
        assert applied_events[0]["action_id"] == action.id
        assert applied_events[0]["new_value"] == 1.7

        await st.detach()

    @pytest.mark.asyncio
    async def test_approve_horizon_s_updates_pred(self):
        bus = EventBus()
        pred = MockPredictor(horizon_s=3.0)
        st = SelfTuner(bus=bus, predictor=pred)
        await st.attach()

        action = st._make_action(
            layer="L5", target="horizon_s",
            old_value=3.0, new_value=8.0,
            reason="测试：延长预测窗口",
        )
        st._pending.append(action)

        await st.approve(action.id)

        assert pred._horizon_s == 8.0
        await st.detach()

    @pytest.mark.asyncio
    async def test_approve_unknown_id_returns_false(self):
        bus = EventBus()
        st = SelfTuner(bus=bus)
        await st.attach()

        result = await st.approve("nonexistent_id")

        assert result is False
        await st.detach()

    @pytest.mark.asyncio
    async def test_approve_all(self):
        bus = EventBus()
        pred = MockPredictor(min_lift=1.5)
        st = SelfTuner(bus=bus, predictor=pred)
        await st.attach()

        action1 = st._make_action("L5", "min_lift", 1.5, 1.7, "test1")
        action2 = st._make_action("L5", "min_lift", 1.7, 1.9, "test2")
        st._pending.extend([action1, action2])

        count = await st.approve_all()

        assert count == 2
        assert len(st._pending) == 0
        assert pred._min_lift == 1.9  # last one applied wins

        await st.detach()


# ---------------------------------------------------------------------------
# reject()
# ---------------------------------------------------------------------------

class TestReject:
    @pytest.mark.asyncio
    async def test_reject_moves_to_rejected(self):
        bus = EventBus()
        st = SelfTuner(bus=bus)
        await st.attach()

        action = st._make_action("L5", "min_lift", 1.5, 1.7, "test")
        st._pending.append(action)

        result = await st.reject(action.id)

        assert result is True
        assert action.status == TuningStatus.REJECTED
        assert action not in st._pending
        assert action in st._rejected

        await st.detach()

    @pytest.mark.asyncio
    async def test_reject_unknown_id_returns_false(self):
        bus = EventBus()
        st = SelfTuner(bus=bus)
        await st.attach()

        result = await st.reject("nonexistent")

        assert result is False
        await st.detach()


# ---------------------------------------------------------------------------
# _housekeeping → auto_approve
# ---------------------------------------------------------------------------

class TestHousekeeping:
    @pytest.mark.asyncio
    async def test_auto_approve_old_pending_actions(self):
        """Actions older than auto_approve_age_s are auto-approved."""
        bus = EventBus()
        pred = MockPredictor(min_lift=1.5)
        st = SelfTuner(bus=bus, predictor=pred, auto_approve_age_s=60.0)
        await st.attach()

        # Create action with old timestamp (manually set created_at to 61s ago)
        action = st._make_action("L5", "min_lift", 1.5, 1.7, "auto-approve test")
        old_time = (datetime.now() - timedelta(seconds=61)).isoformat()
        action.created_at = old_time
        st._pending.append(action)

        applied_events = []
        bus.subscribe("L6.tuning.applied", lambda e: applied_events.append(e.payload))

        await st._housekeeping()

        assert len(st._pending) == 0
        assert action.status == TuningStatus.APPLIED
        assert pred._min_lift == 1.7
        assert len(applied_events) == 1

        await st.detach()

    @pytest.mark.asyncio
    async def test_auto_approve_disabled_when_age_zero(self):
        """auto_approve_age_s=0 → housekeeping does nothing."""
        bus = EventBus()
        st = SelfTuner(bus=bus, auto_approve_age_s=0.0)
        await st.attach()

        action = st._make_action("L5", "min_lift", 1.5, 1.7, "should not auto-approve")
        old_time = (datetime.now() - timedelta(seconds=999)).isoformat()
        action.created_at = old_time
        st._pending.append(action)

        await st._housekeeping()

        assert len(st._pending) == 1  # still pending
        await st.detach()

    @pytest.mark.asyncio
    async def test_housekeeping_called_by_on_warning(self):
        """_on_meta_warning calls _housekeeping to auto-approve stale actions."""
        bus = EventBus()
        pred = MockPredictor(min_lift=1.5)
        st = SelfTuner(bus=bus, predictor=pred, auto_approve_age_s=60.0)
        await st.attach()

        # Pre-populate a stale action
        action = st._make_action("L5", "min_lift", 1.5, 1.7, "stale")
        old_time = (datetime.now() - timedelta(seconds=61)).isoformat()
        action.created_at = old_time
        st._pending.append(action)

        # Send a warning — triggers _housekeeping via _on_meta_warning
        await bus.publish(Event(
            topic="L6.metacognition.warn",
            source="test",
            payload={"issues": ["test issue"]},
        ))

        await asyncio.sleep(0.01)

        # Stale action should be auto-approved
        assert len(st._pending) == 0
        assert pred._min_lift == 1.7

        await st.detach()


# ---------------------------------------------------------------------------
# _review_stale_links
# ---------------------------------------------------------------------------

class TestReviewStaleLinks:
    @pytest.mark.asyncio
    async def test_stale_link_creates_revive_action(self):
        bus = EventBus()
        pred = MockPredictor()
        # pred._links already has stale link: (stale_cause, stale_effect) lift=0.3
        st = SelfTuner(bus=bus, predictor=pred)
        await st.attach()

        pending_events = []
        bus.subscribe("L6.tuning.pending", lambda e: pending_events.append(e.payload))

        await st._review_stale_links()

        assert len(st._pending) == 1
        action = st._pending[0]
        assert action.layer == "L5"
        assert "stale_cause" in action.target
        assert action.old_value == 0.3
        assert action.new_value > 0.3  # revived to higher value
        assert "复活" in action.reason or "衰减" in action.reason

        # Event published
        assert len(pending_events) == 1

        await st.detach()

    @pytest.mark.asyncio
    async def test_no_stale_links_no_pending(self):
        bus = EventBus()

        class FreshLinkPredictor:
            def __init__(self):
                self._links = {}

                class FreshLink:
                    probability_boost = 2.0
                    confidence = 0.8
                self._links[("good_cause", "good_effect")] = FreshLink()

        pred = FreshLinkPredictor()
        st = SelfTuner(bus=bus, predictor=pred)
        await st.attach()

        await st._review_stale_links()

        assert st._pending == []
        await st.detach()


# ---------------------------------------------------------------------------
# pending_report and stats
# ---------------------------------------------------------------------------

class TestPublicAPI:
    @pytest.mark.asyncio
    async def test_pending_report_empty(self):
        st = SelfTuner()
        report = st.pending_report()
        assert "暂无" in report

    @pytest.mark.asyncio
    async def test_pending_report_with_actions(self):
        bus = EventBus()
        st = SelfTuner(bus=bus)
        await st.attach()

        action = st._make_action("L5", "min_lift", 1.5, 1.7, "测试原因")
        st._pending.append(action)

        report = st.pending_report()
        assert "1 个待审批" in report
        assert "min_lift" in report
        assert "1.50 → 1.70" in report
        assert "测试原因" in report

        await st.detach()

    @pytest.mark.asyncio
    async def test_stats(self):
        st = SelfTuner()
        stats = st.stats()
        assert stats["pending"] == 0
        assert stats["applied"] == 0
        assert stats["rejected"] == 0

    @pytest.mark.asyncio
    async def test_clear_pending(self):
        bus = EventBus()
        st = SelfTuner(bus=bus)
        await st.attach()

        action = st._make_action("L5", "min_lift", 1.5, 1.7, "test")
        st._pending.append(action)

        st.clear_pending()
        assert st._pending == []

        await st.detach()


# ---------------------------------------------------------------------------
# Integration: full warn → pending → approve → apply chain
# ---------------------------------------------------------------------------

class TestFullChain:
    @pytest.mark.asyncio
    async def test_warn_triggers_pending_then_manual_approve(self):
        """Full chain: L6.metacognition.warn → pending action → manual approve → apply."""
        bus = EventBus()
        pred = MockPredictor(min_lift=1.5)
        pred.stats_return = {"accuracy": 0.10, "confirmed": 2, "failed": 18, "pending": 0}
        miner = MockPatternMiner()
        st = SelfTuner(bus=bus, predictor=pred, pattern_miner=miner)
        await st.attach()

        pending_events = []
        applied_events = []
        bus.subscribe("L6.tuning.pending", lambda e: pending_events.append(e.payload))
        bus.subscribe("L6.tuning.applied", lambda e: applied_events.append(e.payload))

        # Step 1: warn fired (simulating PredictionMonitor with 20 failed predictions)
        await bus.publish(Event(
            topic="L6.metacognition.warn",
            source="L6.prediction_monitor",
            payload={
                "issues": ["预测准确率 10% 严重低迷（阈值 25%）"],
                "context": {"confirmed": 2, "failed": 18, "window": 20},
            },
        ))
        await asyncio.sleep(0.01)

        # Step 2: pending actions created (min_lift + horizon_s)
        assert len(st._pending) == 2
        assert len(pending_events) == 2
        action_id = pending_events[0]["action_id"]

        # Step 3: manual approval — approve the min_lift action
        min_lift_action = next(a for a in st._pending if a.target == "min_lift")
        await st.approve(min_lift_action.id)

        # Step 4: action applied
        assert len(st._applied) == 1
        assert len(applied_events) == 1
        assert pred._min_lift == 1.7
        assert miner.last_min_lift == 1.7
        assert len(st._pending) == 1  # horizon_s still pending

        await st.detach()

    @pytest.mark.asyncio
    async def test_auto_approve_chain(self):
        """Full chain: warn → stale action → auto_approve after 60s → apply."""
        bus = EventBus()
        pred = MockPredictor(min_lift=1.5)
        pred.stats_return = {"accuracy": 0.10, "confirmed": 2, "failed": 18, "pending": 0}
        st = SelfTuner(bus=bus, predictor=pred, auto_approve_age_s=60.0)
        await st.attach()

        applied_events = []
        bus.subscribe("L6.tuning.applied", lambda e: applied_events.append(e.payload))

        # Step 1: warn creates a pending action
        await bus.publish(Event(
            topic="L6.metacognition.warn",
            source="L6.prediction_monitor",
            payload={
                "issues": ["预测准确率 10% 严重低迷（阈值 25%）"],
                "context": {},
            },
        ))
        await asyncio.sleep(0.01)
        assert len(st._pending) == 2  # min_lift + horizon_s
        action = next(a for a in st._pending if a.target == "min_lift")
        assert action.status == TuningStatus.PENDING

        # Step 2: manually age both actions past auto_approve threshold
        old_time = (datetime.now() - timedelta(seconds=61)).isoformat()
        for a in st._pending:
            a.created_at = old_time

        # Step 3: any subsequent warn triggers housekeeping → auto-approve both
        await bus.publish(Event(
            topic="L6.metacognition.warn",
            source="test",
            payload={"issues": ["another warning"]},
        ))
        await asyncio.sleep(0.01)

        # Action auto-approved (both min_lift + horizon_s)
        assert len(st._pending) == 0
        assert action.status == TuningStatus.APPLIED
        assert pred._min_lift == 1.7
        assert len(applied_events) == 2  # both actions auto-approved

        await st.detach()
