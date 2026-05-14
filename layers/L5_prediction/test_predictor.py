"""
Tests for L5 PredictiveReasoner
"""
import asyncio
import time

import pytest

from kernel.event_bus import EventBus, Event
from layers.L5_prediction.predictor import PredictiveReasoner, Prediction


class FakeCausalLink:
    def __init__(self, cause, effect, lift, confidence=0.8):
        self.cause = cause
        self.effect = effect
        self.lift = lift
        self.confidence = confidence


class TestPrediction:
    def test_is_expired(self):
        pred = Prediction(
            cause="L7.regulator.acted",
            effect="L6.metacognition.report",
            probability_boost=2.0,
            confidence=0.9,
            issued_at=time.time() - 10.0,
            horizon_s=3.0,
        )
        assert pred.is_expired() is True
        assert pred.is_failed() is True

    def test_is_pending(self):
        pred = Prediction(
            cause="L7.regulator.acted",
            effect="L6.metacognition.report",
            probability_boost=2.0,
            confidence=0.9,
            issued_at=time.time(),
            horizon_s=10.0,
        )
        assert pred.is_pending() is True
        assert pred.is_confirmed() is False


class TestPredictiveReasoner:
    @pytest.fixture
    def bus(self):
        return EventBus()

    @pytest.fixture
    def causal_links(self):
        return [
            FakeCausalLink("L7.regulator.acted", "L6.metacognition.report", lift=2.5),
            FakeCausalLink("L9.self.updated", "L8.intent.proposed", lift=1.8),
        ]

    @pytest.fixture
    def reasoner(self, bus, causal_links):
        r = PredictiveReasoner(
            bus=bus,
            causal_links_fn=lambda: causal_links,
            horizon_s=2.0,
            min_lift=1.5,
        )
        return r

    @pytest.mark.asyncio
    async def test_emits_upcoming_on_cause(self, reasoner, bus):
        await reasoner.attach()
        predictions = []

        async def catch(event):
            if event.topic == "L5.prediction.upcoming":
                predictions.append(event.payload)

        bus.subscribe("L5.prediction.upcoming", catch)

        await bus.publish(Event(topic="L7.regulator.acted", source="test", payload={}))

        # Let async handlers run
        await asyncio.sleep(0.05)

        assert len(predictions) == 1
        assert predictions[0]["cause"] == "L7.regulator.acted"
        assert predictions[0]["predicted_effect"] == "L6.metacognition.report"
        assert predictions[0]["probability_boost"] == 2.5

        await reasoner.detach()

    @pytest.mark.asyncio
    async def test_confirms_prediction(self, reasoner, bus):
        await reasoner.attach()

        confirmed = []
        async def catch(event):
            if event.topic == "L5.prediction.confirmed":
                confirmed.append(event.payload)

        bus.subscribe("L5.prediction.confirmed", catch)

        # First the cause
        await bus.publish(Event(topic="L7.regulator.acted", source="test", payload={}))
        await asyncio.sleep(0.05)
        # Then the predicted effect
        await bus.publish(Event(topic="L6.metacognition.report", source="test", payload={}))
        await asyncio.sleep(0.05)

        assert len(confirmed) == 1
        assert confirmed[0]["cause"] == "L7.regulator.acted"
        assert confirmed[0]["effect"] == "L6.metacognition.report"

        await reasoner.detach()

    @pytest.mark.asyncio
    async def test_stats_tracks_pending_confirmed_failed(self, reasoner, bus):
        await reasoner.attach()

        # Emit cause → predicted effect
        await bus.publish(Event(topic="L7.regulator.acted", source="test", payload={}))
        await asyncio.sleep(0.05)

        stats = reasoner.stats()
        assert stats["pending"] == 1

        # Confirm it
        await bus.publish(Event(topic="L6.metacognition.report", source="test", payload={}))
        await asyncio.sleep(0.05)

        stats = reasoner.stats()
        assert stats["confirmed"] == 1
        assert stats["pending"] == 0

        await reasoner.detach()

    def test_accuracy(self, reasoner):
        # Manually add resolved predictions to the deque
        reasoner._pending.append(Prediction(
            cause="A", effect="B", probability_boost=2.0,
            confidence=0.8, issued_at=time.time(), horizon_s=1.0, outcome="confirmed",
        ))
        reasoner._pending.append(Prediction(
            cause="C", effect="D", probability_boost=2.0,
            confidence=0.8, issued_at=time.time(), horizon_s=1.0, outcome="confirmed",
        ))
        reasoner._pending.append(Prediction(
            cause="E", effect="F", probability_boost=2.0,
            confidence=0.8, issued_at=time.time(), horizon_s=1.0, outcome="failed",
        ))
        # 2 confirmed, 1 failed → 66.7%
        assert reasoner.accuracy() == pytest.approx(2/3, rel=0.01)
