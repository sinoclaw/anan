"""
L5 PredictiveReasoner 完整测试套件
===================================
真实 API（从 predictor.py 读取）：
  - PredictiveReasoner(bus, causal_links_fn, self_model, horizon_s, min_lift, max_pending)
  - causal_links_fn: Callable returning list[tuple[cause, effect]]
  - stats() = {links_cached, pending, confirmed, failed, accuracy, horizon_s}
  - attach/detach（无幂等保护）
  - PredictiveReasoner 不发自己的 L5.prediction.* 事件，只订阅
"""
import asyncio
import time
import pytest
from kernel.event_bus import EventBus, Event
from layers.L5_prediction.predictor import Prediction, PredictiveReasoner


# ---------------------------------------------------------------------------
# Prediction dataclass
# ---------------------------------------------------------------------------

class TestPrediction:
    def test_is_pending(self):
        p = Prediction("A", "B", 2.0, 0.5, time.time(), 10.0)
        assert p.is_pending()
        assert not p.is_confirmed()
        assert not p.is_failed()

    def test_is_confirmed(self):
        p = Prediction("A", "B", 2.0, 0.5, time.time(), 10.0, outcome="confirmed")
        assert not p.is_pending()
        assert p.is_confirmed()
        assert not p.is_failed()

    def test_is_failed_explicit(self):
        p = Prediction("A", "B", 2.0, 0.5, time.time(), 10.0, outcome="failed")
        assert not p.is_pending()
        assert not p.is_confirmed()
        assert p.is_failed()

    def test_is_failed_expired(self):
        old = time.time() - 20.0
        p = Prediction("A", "B", 2.0, 0.5, old, 10.0)  # issued 20s ago, horizon=10s
        assert p.is_failed()

    def test_is_expired(self):
        old = time.time() - 20.0
        p = Prediction("A", "B", 2.0, 0.5, old, 10.0)
        assert p.is_expired()
        fresh = Prediction("A", "B", 2.0, 0.5, time.time(), 10.0)
        assert not fresh.is_expired()


# ---------------------------------------------------------------------------
# PredictiveReasoner init
# ---------------------------------------------------------------------------

class TestPredictiveReasonerInit:
    def test_defaults(self):
        pr = PredictiveReasoner()
        assert pr._horizon_s == 3.0
        assert pr._min_lift == 1.5
        assert pr._max_pending == 50
        assert pr._get_links() == []

    def test_custom_params(self):
        pr = PredictiveReasoner(
            horizon_s=10.0,
            min_lift=2.0,
            max_pending=100,
        )
        assert pr._horizon_s == 10.0
        assert pr._min_lift == 2.0
        assert pr._max_pending == 100

    def test_causal_links_fn(self):
        links_fn = lambda: [("A", "B"), ("X", "Y")]
        pr = PredictiveReasoner(causal_links_fn=links_fn)
        assert pr._get_links() == [("A", "B"), ("X", "Y")]

    def test_bus_assignment(self):
        bus = EventBus()
        pr = PredictiveReasoner(bus=bus)
        assert pr._bus is bus


# ---------------------------------------------------------------------------
# attach/detach
# ---------------------------------------------------------------------------

class TestPredictiveReasonerAttach:
    @pytest.mark.asyncio
    async def test_detach_clears_unsubs(self):
        bus = EventBus()
        pr = PredictiveReasoner(bus=bus)
        await pr.attach()
        await pr.detach()
        assert len(pr._unsubs) == 0

    @pytest.mark.asyncio
    async def test_attach_subscribes_to_layers(self):
        """attach() subscribes to all layer wildcard topics."""
        bus = EventBus()
        pr = PredictiveReasoner(bus=bus, causal_links_fn=lambda: [("A", "B")])
        await pr.attach()
        # Should have subscribed to ~9 layer wildcards + 1 link_discovered
        assert len(pr._unsubs) >= 10
        await pr.detach()


# ---------------------------------------------------------------------------
# Prediction lifecycle
# ---------------------------------------------------------------------------

class TestPredictionLifecycle:
    @pytest.mark.asyncio
    async def test_causal_link_added_to_links_cache(self):
        """L5.causal.link_discovered event populates _links cache."""
        bus = EventBus()
        pr = PredictiveReasoner(bus=bus, min_lift=1.0)
        await pr.attach()

        await bus.publish(Event(
            topic="L5.causal.link_discovered",
            source="test",
            payload={"cause": "A", "effect": "B", "lift": 2.0, "confidence": 0.8},
        ))
        await asyncio.sleep(0.05)

        assert ("A", "B") in pr._links
        link = pr._links[("A", "B")]
        assert link.probability_boost == 2.0
        assert link.confidence == 0.8

        await pr.detach()

    @pytest.mark.asyncio
    async def test_low_lift_link_ignored(self):
        """Links below min_lift are not cached."""
        bus = EventBus()
        pr = PredictiveReasoner(bus=bus, min_lift=2.0)
        await pr.attach()

        await bus.publish(Event(
            topic="L5.causal.link_discovered",
            source="test",
            payload={"cause": "A", "effect": "B", "lift": 1.5, "confidence": 0.5},
        ))
        await asyncio.sleep(0.05)

        assert ("A", "B") not in pr._links

        await pr.detach()

    @pytest.mark.asyncio
    async def test_effect_event_marks_prediction_confirmed(self):
        """When predicted effect fires, pending prediction is confirmed."""
        bus = EventBus()
        pr = PredictiveReasoner(
            bus=bus,
            causal_links_fn=lambda: [("L7.regulator.acted", "L6.metacognition.report", 2.0, 0.8)],
            min_lift=1.0,
        )
        await pr.attach()

        # Trigger the cause event (L7.* topic matching the layer wildcard)
        await bus.publish(Event(topic="L7.regulator.acted", source="test", payload={}))
        await asyncio.sleep(0.05)

        # Effect fires → should confirm
        await bus.publish(Event(topic="L6.metacognition.report", source="test", payload={}))
        await asyncio.sleep(0.05)

        confirmed = pr.confirmed_predictions()
        assert len(confirmed) >= 1

        await pr.detach()

    @pytest.mark.asyncio
    async def test_effect_event_confirms_from_links_cache(self):
        """Effect fires → finds matching pending prediction from _links cache."""
        bus = EventBus()
        pr = PredictiveReasoner(bus=bus, min_lift=1.0)
        await pr.attach()

        # Pre-populate links cache via event
        await bus.publish(Event(
            topic="L5.causal.link_discovered",
            source="test",
            payload={"cause": "L7.regulator.acted", "effect": "L6.metacognition.report", "lift": 2.0, "confidence": 0.8},
        ))
        await asyncio.sleep(0.05)

        # Now fire the cause (L7.* matches layer subscription)
        await bus.publish(Event(topic="L7.regulator.acted", source="test", payload={}))
        await asyncio.sleep(0.05)

        # And then the effect
        await bus.publish(Event(topic="L6.metacognition.report", source="test", payload={}))
        await asyncio.sleep(0.05)

        confirmed = pr.confirmed_predictions()
        assert len(confirmed) >= 1

        await pr.detach()

    @pytest.mark.asyncio
    async def test_pending_predictions_tracked(self):
        """After cause fires, prediction appears in pending."""
        bus = EventBus()
        pr = PredictiveReasoner(
            bus=bus,
            causal_links_fn=lambda: [("L7.regulator.acted", "L6.metacognition.report", 2.0, 0.8)],
            min_lift=1.0,
        )
        await pr.attach()

        await bus.publish(Event(topic="L7.regulator.acted", source="test", payload={}))
        await asyncio.sleep(0.05)

        pending = pr.pending_predictions()
        assert len(pending) >= 1

        await pr.detach()


# ---------------------------------------------------------------------------
# Accuracy
# ---------------------------------------------------------------------------

class TestAccuracy:
    @pytest.mark.asyncio
    async def test_accuracy_zero_when_no_predictions(self):
        pr = PredictiveReasoner(bus=EventBus())
        assert pr.accuracy() == 0.0

    @pytest.mark.asyncio
    async def test_accuracy_after_confirmed_and_failed(self):
        bus = EventBus()
        pr = PredictiveReasoner(
            bus=bus,
            causal_links_fn=lambda: [("L7.regulator.acted", "L6.metacognition.report", 2.0, 0.8)],
            min_lift=1.0,
            horizon_s=0.05,
        )
        await pr.attach()

        # Confirmed
        await bus.publish(Event(topic="L7.regulator.acted", source="test", payload={}))
        await asyncio.sleep(0.02)
        await bus.publish(Event(topic="L6.metacognition.report", source="test", payload={}))
        await asyncio.sleep(0.08)

        # Failed
        await bus.publish(Event(topic="L7.regulator.acted", source="test", payload={}))
        await asyncio.sleep(0.1)  # expire

        acc = pr.accuracy()
        assert 0.3 <= acc <= 0.7  # ~50%

        await pr.detach()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class TestStats:
    @pytest.mark.asyncio
    async def test_stats_keys(self):
        bus = EventBus()
        pr = PredictiveReasoner(bus=bus)
        await pr.attach()

        stats = pr.stats()
        assert "links_cached" in stats
        assert "pending" in stats
        assert "confirmed" in stats
        assert "failed" in stats
        assert "accuracy" in stats
        assert "horizon_s" in stats

        await pr.detach()


# ---------------------------------------------------------------------------
# Getters
# ---------------------------------------------------------------------------

class TestGetters:
    @pytest.mark.asyncio
    async def test_pending_predictions_empty_initially(self):
        pr = PredictiveReasoner(bus=EventBus())
        assert pr.pending_predictions() == []

    @pytest.mark.asyncio
    async def test_confirmed_predictions_empty_initially(self):
        pr = PredictiveReasoner(bus=EventBus())
        assert pr.confirmed_predictions() == []

    @pytest.mark.asyncio
    async def test_failed_predictions_empty_initially(self):
        pr = PredictiveReasoner(bus=EventBus())
        assert pr.failed_predictions() == []


# ---------------------------------------------------------------------------
# what_do_i_predict
# ---------------------------------------------------------------------------

class TestWhatDoIPredict:
    def test_empty_when_no_predictions(self):
        pr = PredictiveReasoner()
        result = pr.what_do_i_predict()
        assert "暂无" in result

    @pytest.mark.asyncio
    async def test_shows_pending_predictions(self):
        bus = EventBus()
        pr = PredictiveReasoner(
            bus=bus,
            causal_links_fn=lambda: [("L7.regulator.acted", "L6.metacognition.report", 2.0, 0.8)],
            min_lift=1.0,
        )
        await pr.attach()

        await bus.publish(Event(topic="L7.regulator.acted", source="test", payload={}))
        await asyncio.sleep(0.01)

        result = pr.what_do_i_predict()
        assert "L7" in result or "L6" in result

        await pr.detach()
