"""
L6 PredictionMonitor 测试套件
=============================

覆盖 PredictionMonitor 的：
  - 监听 L5.prediction.confirmed / failed 事件
  - 滑动窗口准确率计算
  - 预测失败触发链路衰减（_decay_link）
  - 持续低迷触发 L6.metacognition.warn
  - 准确率无数据时返回 1.0（healthy default）
"""

import pytest

from kernel.event_bus import EventBus, Event
from layers.L6_metacognition.prediction_monitor import PredictionMonitor


class MockPredictor:
    """Mock PredictiveReasoner with _decay_link."""

    def __init__(self):
        self.decayed_links: list[tuple[str, str]] = []
        # Simulate a causal link with lift=2.0
        self._links: dict[tuple, object] = {}

        class FakeLink:
            def __init__(self):
                self.probability_boost = 2.0
                self.confidence = 0.8

        self._links[("A", "B")] = FakeLink()

    def _decay_link(self, cause: str, effect: str, decay: float = 0.15) -> bool:
        key = (cause, effect)
        if key not in self._links:
            return False
        link = self._links[key]
        old_lift = link.probability_boost
        link.probability_boost = max(0.1, old_lift - decay * old_lift)
        link.confidence = max(0.0, link.confidence - 0.05)
        self.decayed_links.append((cause, effect))
        return True


# --------------------------------------------------------------------------


class TestPredictionMonitorInit:
    def test_defaults(self):
        pm = PredictionMonitor()
        assert pm._acc_thresh == 0.4
        assert pm._severe_thresh == 0.25
        assert pm._window == 20
        assert pm._pred is None

    def test_custom_params(self):
        pm = PredictionMonitor(accuracy_threshold=0.5, severe_threshold=0.2, window=10)
        assert pm._acc_thresh == 0.5
        assert pm._severe_thresh == 0.2
        assert pm._window == 10


class TestAccuracyCalculation:
    @pytest.mark.asyncio
    async def test_empty_returns_one(self):
        pm = PredictionMonitor()
        await pm.attach()
        assert pm.accuracy() == 1.0
        await pm.detach()

    @pytest.mark.asyncio
    async def test_all_confirmed(self):
        bus = EventBus()
        pm = PredictionMonitor(bus=bus)
        await pm.attach()

        for _ in range(5):
            await bus.publish(Event(
                topic="L5.prediction.confirmed",
                source="test",
                payload={"cause": "A", "effect": "B"},
            ))

        assert pm.accuracy() == 1.0
        assert pm.stats()["confirmed"] == 5
        assert pm.stats()["failed"] == 0
        await pm.detach()

    @pytest.mark.asyncio
    async def test_all_failed(self):
        bus = EventBus()
        pm = PredictionMonitor(bus=bus)
        await pm.attach()

        for _ in range(4):
            await bus.publish(Event(
                topic="L5.prediction.failed",
                source="test",
                payload={"cause": "A", "predicted_effect": "B"},
            ))

        assert pm.accuracy() == 0.0
        assert pm.stats()["failed"] == 4
        await pm.detach()

    @pytest.mark.asyncio
    async def test_mixed(self):
        bus = EventBus()
        pm = PredictionMonitor(bus=bus)
        await pm.attach()

        # 3 confirmed, 1 failed → 75%
        for _ in range(3):
            await bus.publish(Event(topic="L5.prediction.confirmed", source="test", payload={"cause": "X", "effect": "Y"}))
        await bus.publish(Event(topic="L5.prediction.failed", source="test", payload={"cause": "X", "predicted_effect": "Y"}))

        assert pm.accuracy() == 0.75
        await pm.detach()


class TestSlidingWindow:
    @pytest.mark.asyncio
    async def test_window_caps_at_size(self):
        bus = EventBus()
        pm = PredictionMonitor(bus=bus, window=5)
        await pm.attach()

        # Add 7 outcomes (window=5)
        for i in range(7):
            payload = {"cause": f"E{i}", "effect": f"R{i}"}
            if i % 2 == 0:
                await bus.publish(Event(topic="L5.prediction.confirmed", source="test", payload=payload))
            else:
                await bus.publish(Event(topic="L5.prediction.failed", source="test", payload=payload))

        # Window is capped at 5
        assert len(pm._outcomes) == 5
        # 4 confirmed (0,2,4,6), 1 failed (1,3,5) → window has 3 confirmed, 2 failed
        # But since old outcomes rotate out... let me check
        stats = pm.stats()
        assert stats["confirmed"] + stats["failed"] == 5
        await pm.detach()


class TestDecayLink:
    @pytest.mark.asyncio
    async def test_failed_triggers_decay(self):
        bus = EventBus()
        pred = MockPredictor()
        pm = PredictionMonitor(bus=bus, predictor=pred)
        await pm.attach()

        await bus.publish(Event(
            topic="L5.prediction.failed",
            source="test",
            payload={"cause": "A", "predicted_effect": "B"},
        ))

        assert len(pred.decayed_links) == 1
        assert pred.decayed_links[0] == ("A", "B")
        await pm.detach()

    @pytest.mark.asyncio
    async def test_confirmed_does_not_decay(self):
        bus = EventBus()
        pred = MockPredictor()
        pm = PredictionMonitor(bus=bus, predictor=pred)
        await pm.attach()

        await bus.publish(Event(
            topic="L5.prediction.confirmed",
            source="test",
            payload={"cause": "A", "effect": "B"},
        ))

        assert len(pred.decayed_links) == 0
        await pm.detach()

    @pytest.mark.asyncio
    async def test_unknown_link_no_decay_crash(self):
        bus = EventBus()
        pred = MockPredictor()
        pm = PredictionMonitor(bus=bus, predictor=pred)
        await pm.attach()

        # Unknown link should not crash — _decay_link returns False
        await bus.publish(Event(
            topic="L5.prediction.failed",
            source="test",
            payload={"cause": "UNKNOWN", "predicted_effect": "LINK"},
        ))

        assert len(pred.decayed_links) == 0  # no crash, no decay
        await pm.detach()


class TestSevereWarning:
    @pytest.mark.asyncio
    async def test_severe_accuracy_triggers_warn(self):
        bus = EventBus()
        pm = PredictionMonitor(bus=bus, predictor=MockPredictor(), severe_threshold=0.4, window=5)
        await pm.attach()

        warns = []
        bus.subscribe("L6.metacognition.warn", lambda e: warns.append(e.payload))

        # 5 failed in a row → accuracy = 0% < severe_threshold(0.4)
        for i in range(5):
            await bus.publish(Event(
                topic="L5.prediction.failed",
                source="test",
                payload={"cause": f"F{i}", "predicted_effect": f"T{i}"},
            ))

        assert len(warns) >= 1
        assert "准确率" in warns[-1]["issues"][0]
        await pm.detach()

    @pytest.mark.asyncio
    async def test_healthy_accuracy_no_warn(self):
        bus = EventBus()
        pm = PredictionMonitor(bus=bus, predictor=MockPredictor(), severe_threshold=0.3, window=5)
        await pm.attach()

        warns = []
        bus.subscribe("L6.metacognition.warn", lambda e: warns.append(e.payload))

        # 4 confirmed, 1 failed → accuracy = 80% > severe_threshold(0.3)
        for i in range(4):
            await bus.publish(Event(topic="L5.prediction.confirmed", source="test", payload={"cause": f"C{i}", "effect": f"D{i}"}))
        await bus.publish(Event(topic="L5.prediction.failed", source="test", payload={"cause": "X", "predicted_effect": "Y"}))

        assert len(warns) == 0
        await pm.detach()


class TestDetach:
    @pytest.mark.asyncio
    async def test_detach_clears_subscriptions(self):
        bus = EventBus()
        pm = PredictionMonitor(bus=bus)
        await pm.attach()
        await pm.detach()

        # After detach, events should not be processed
        await bus.publish(Event(topic="L5.prediction.confirmed", source="test", payload={"cause": "A", "effect": "B"}))
        assert pm.accuracy() == 1.0  # No change — subscription cleared
