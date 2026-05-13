"""Tests for L3 working memory."""

from __future__ import annotations

import asyncio
import time

import pytest

from kernel.event_bus import Event, EventBus
from layers.L3_working_memory import WorkingMemory, default_salience


@pytest.fixture
def fresh_bus():
    return EventBus()


class TestSalienceScoring:
    def test_tick_is_low(self):
        e = Event(topic="L0.circadian.tick", source="t", payload={})
        assert default_salience(e) < 0.2

    def test_self_layer_is_top(self):
        e = Event(topic="L9.self.updated", source="t", payload={})
        assert default_salience(e) >= 0.9

    def test_l1_sleep_high(self):
        e = Event(topic="L1.sleep.rem.consolidated", source="t", payload={})
        assert default_salience(e) > 0.7


class TestWorkingMemoryBasic:
    @pytest.mark.asyncio
    async def test_captures_attached_events(self, fresh_bus):
        wm = WorkingMemory(capacity=10)
        await wm.attach(fresh_bus)
        await fresh_bus.publish(Event(topic="L1.sleep.rem.consolidated",
                                      source="t", payload={"x": 1}))
        await asyncio.sleep(0.01)
        assert wm.stats()["size"] == 1
        assert wm.stats()["captured_total"] == 1
        await wm.detach()

    @pytest.mark.asyncio
    async def test_low_salience_dropped(self, fresh_bus):
        wm = WorkingMemory(
            capacity=10,
            salience_fn=lambda e: 0.0,    # everything scores 0
            min_salience=0.1,
        )
        await wm.attach(fresh_bus)
        await fresh_bus.publish(Event(topic="L1.sleep.rem.consolidated",
                                      source="t", payload={}))
        await asyncio.sleep(0.01)
        assert wm.stats()["size"] == 0
        await wm.detach()

    @pytest.mark.asyncio
    async def test_does_not_capture_own_events(self, fresh_bus):
        wm = WorkingMemory(capacity=10)
        await wm.attach(fresh_bus)
        # Trigger some captures so meta-events fire
        for _ in range(3):
            await fresh_bus.publish(Event(topic="L1.sleep.rem.consolidated",
                                          source="t", payload={}))
        await asyncio.sleep(0.02)
        # Only the L1 events should be in WM, not the L3 meta events
        topics = [e.event.topic for e in wm.snapshot()]
        assert all(not t.startswith("L3.") for t in topics)
        assert len(topics) == 3
        await wm.detach()


class TestEviction:
    @pytest.mark.asyncio
    async def test_capacity_enforced(self, fresh_bus):
        wm = WorkingMemory(capacity=3)
        await wm.attach(fresh_bus)
        for i in range(5):
            await fresh_bus.publish(Event(topic="L1.sleep.rem.consolidated",
                                          source="t", payload={"i": i}))
        await asyncio.sleep(0.02)
        assert wm.stats()["size"] == 3
        assert wm.stats()["evicted_total"] == 2

    @pytest.mark.asyncio
    async def test_evicts_lowest_salience_first(self, fresh_bus):
        # All events have different salience; lowest should go
        def scorer(e):
            return e.payload.get("score", 0.5)

        wm = WorkingMemory(capacity=2, salience_fn=scorer, min_salience=0.0)
        await wm.attach(fresh_bus)
        await fresh_bus.publish(Event(topic="X", source="t", payload={"score": 0.9, "tag": "high"}))
        await fresh_bus.publish(Event(topic="X", source="t", payload={"score": 0.1, "tag": "low"}))
        await fresh_bus.publish(Event(topic="X", source="t", payload={"score": 0.7, "tag": "mid"}))
        await asyncio.sleep(0.02)
        # capacity=2, "low" should have been evicted
        tags = sorted(e.event.payload["tag"] for e in wm.snapshot())
        assert tags == ["high", "mid"]


class TestRecallAndDecay:
    @pytest.mark.asyncio
    async def test_recall_returns_top_n(self, fresh_bus):
        wm = WorkingMemory(capacity=10)
        await wm.attach(fresh_bus)
        for topic in ("L0.circadian.tick", "L1.sleep.rem.consolidated", "L9.self.updated"):
            await fresh_bus.publish(Event(topic=topic, source="t", payload={}))
        await asyncio.sleep(0.02)
        top = wm.recall_recent(n=2)
        # L9 has highest salience, then L1
        assert top[0].event.topic == "L9.self.updated"
        assert top[1].event.topic == "L1.sleep.rem.consolidated"

    @pytest.mark.asyncio
    async def test_decay_lowers_weight_over_time(self, fresh_bus):
        wm = WorkingMemory(capacity=10, half_life_s=0.05)  # 50ms half-life
        await wm.attach(fresh_bus)
        await fresh_bus.publish(Event(topic="L1.sleep.rem.consolidated",
                                      source="t", payload={}))
        await asyncio.sleep(0.01)
        entry = wm.snapshot()[0]
        w_now = entry.weight(now=time.time(), half_life_s=0.05)
        # After 2 half-lives (100ms), weight should be ~25% of original
        await asyncio.sleep(0.1)
        w_later = entry.weight(now=time.time(), half_life_s=0.05)
        assert w_later < w_now * 0.5


class TestStats:
    @pytest.mark.asyncio
    async def test_stats_reports_correctly(self, fresh_bus):
        wm = WorkingMemory(capacity=2)
        await wm.attach(fresh_bus)
        for _ in range(4):
            await fresh_bus.publish(Event(topic="L1.sleep.rem.consolidated",
                                          source="t", payload={}))
        await asyncio.sleep(0.02)
        s = wm.stats()
        assert s["size"] == 2
        assert s["captured_total"] == 4
        assert s["evicted_total"] == 2
        assert s["capacity"] == 2

    def test_invalid_capacity_rejected(self):
        with pytest.raises(ValueError):
            WorkingMemory(capacity=0)
