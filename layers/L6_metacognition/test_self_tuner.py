"""
L6 SelfTuner 测试套件
======================

覆盖 SelfTuner 的：
  - 初始化参数
  - 准确率低 → 提高 min_lift + 延长 horizon
  - 准确率高但预测量少 → 降低 min_lift
  - stale link 复活
  - suggest() 报告
  - auto_apply vs manual 模式
"""

import pytest

from kernel.event_bus import EventBus, Event
from layers.L6_metacognition.self_tuner import SelfTuner, TuningAction


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

        # Some normal links, some stale
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
        assert st._auto is False

    def test_custom_params(self):
        st = SelfTuner(
            accuracy_low_threshold=0.4,
            accuracy_high_threshold=0.85,
            min_lift_adjust_step=0.3,
            horizon_adjust_step=1.0,
            auto_apply=True,
        )
        assert st._acc_low == 0.4
        assert st._acc_high == 0.85
        assert st._lift_step == 0.3
        assert st._horizon_step == 1.0
        assert st._auto is True


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

        # Should have 2 suggestions: min_lift up, horizon up
        assert len(st._suggestions) >= 1
        lift_action = next((a for a in st._suggestions if a.target == "min_lift"), None)
        assert lift_action is not None
        assert lift_action.new_value > lift_action.old_value   # 1.5 → 1.7
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

        lift_action = next((a for a in st._suggestions if a.target == "min_lift"), None)
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
        assert len(st._suggestions) == 2
        await st.detach()


class TestAutoApply:
    @pytest.mark.asyncio
    async def test_auto_apply_executes_action(self):
        bus = EventBus()
        pred = MockPredictor()
        pred._min_lift = 1.5
        st = SelfTuner(bus=bus, predictor=pred, auto_apply=True)
        await st.attach()

        await bus.publish(Event(
            topic="L6.metacognition.warn",
            source="test",
            payload={"issues": ["预测准确率严重低迷"]},
        ))
        await st._review_stale_links()

        # auto_apply=True → actions should be in _applied
        assert len(st._applied) >= 1

        # Verify min_lift was actually changed
        assert pred._min_lift > 1.5
        await st.detach()


class TestSuggest:
    def test_empty_suggest(self):
        st = SelfTuner()
        assert "暂无" in st.suggest()

    @pytest.mark.asyncio
    async def test_suggest_shows_pending(self):
        bus = EventBus()
        pred = MockPredictor()
        st = SelfTuner(bus=bus, predictor=pred)
        await st.attach()

        await st._tune_l5_for_accuracy()

        report = st.suggest()
        assert "L5" in report
        assert "min_lift" in report
        await st.detach()

    def test_clear_suggestions(self):
        st = SelfTuner()
        st._suggestions.append(
            TuningAction("L5", "min_lift", 1.5, 1.7, "test")
        )
        st.clear_suggestions()
        assert len(st._suggestions) == 0


class TestStats:
    def test_stats(self):
        st = SelfTuner(auto_apply=True)
        assert st.stats()["auto_apply"] is True
        assert st.stats()["suggestions_pending"] == 0
        assert st.stats()["actions_applied"] == 0
