"""Tests for anan.kernel.event_bus

Coverage targets:
- Event topic matching (literal, wildcard segment, full wildcard)
- Subscribe / publish / unsubscribe lifecycle
- Concurrent delivery to multiple handlers
- Failure isolation (crashing handler doesn't affect others)
- History ring buffer and filtering
- Singleton get_bus() returns same instance
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Make anan/kernel importable when running pytest from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kernel.event_bus import Event, EventBus, get_bus


# ---------------------------------------------------------------------------
# Event.matches — topic pattern matching
# ---------------------------------------------------------------------------


class TestEventMatches:
    def test_exact_match(self):
        e = Event(topic="L1.sleep.start")
        assert e.matches("L1.sleep.start")

    def test_full_wildcard(self):
        e = Event(topic="anything.goes.here")
        assert e.matches("*")

    def test_wildcard_suffix(self):
        e = Event(topic="L1.sleep.consolidated")
        assert e.matches("L1.sleep.*")
        assert e.matches("L1.*.consolidated")

    def test_wildcard_prefix(self):
        e = Event(topic="L1.sleep.consolidated")
        assert e.matches("*.sleep.consolidated")

    def test_wildcard_middle(self):
        e = Event(topic="L1.sleep.consolidated")
        assert e.matches("L1.*.consolidated")

    def test_no_match_different_segments(self):
        e = Event(topic="L1.sleep.start")
        assert not e.matches("L2.sleep.start")
        assert not e.matches("L1.attention.start")

    def test_no_match_different_length(self):
        e = Event(topic="L1.sleep.start")
        # No wildcards, segment counts differ
        assert not e.matches("L1.sleep")
        assert not e.matches("L1.sleep.start.extra")

    def test_trailing_wildcard_matches_any_depth(self):
        e1 = Event(topic="L1.sleep")
        e2 = Event(topic="L1.sleep.start")
        e3 = Event(topic="L1.sleep.consolidated.deep")
        # "L1.*" should match all three (one or more segments under L1)
        assert e1.matches("L1.*")
        assert e2.matches("L1.*")
        assert e3.matches("L1.*")

    def test_trailing_wildcard_requires_at_least_one_segment(self):
        e = Event(topic="L1")
        # "L1.*" requires at least one segment after L1
        assert not e.matches("L1.*")


# ---------------------------------------------------------------------------
# Event construction defaults
# ---------------------------------------------------------------------------


class TestEventDefaults:
    def test_payload_defaults_to_empty_dict(self):
        e = Event(topic="x.y")
        assert e.payload == {}

    def test_event_id_is_unique(self):
        a = Event(topic="x")
        b = Event(topic="x")
        assert a.event_id != b.event_id
        assert len(a.event_id) == 12

    def test_timestamp_is_set(self):
        e = Event(topic="x")
        assert e.ts > 0

    def test_event_is_frozen(self):
        e = Event(topic="x")
        with pytest.raises(Exception):  # FrozenInstanceError
            e.topic = "y"  # type: ignore


# ---------------------------------------------------------------------------
# EventBus core flow
# ---------------------------------------------------------------------------


class TestEventBusCore:
    @pytest.mark.asyncio
    async def test_subscribe_and_publish(self):
        bus = EventBus()
        received = []

        async def handler(ev):
            received.append(ev.topic)

        bus.subscribe("L1.sleep.*", handler)
        delivered = await bus.publish(Event(topic="L1.sleep.start"))

        assert delivered == 1
        assert received == ["L1.sleep.start"]

    @pytest.mark.asyncio
    async def test_no_subscribers_returns_zero(self):
        bus = EventBus()
        n = await bus.publish(Event(topic="L9.self.update"))
        assert n == 0

    @pytest.mark.asyncio
    async def test_pattern_does_not_match(self):
        bus = EventBus()
        called = []

        async def handler(ev):
            called.append(ev)

        bus.subscribe("L1.*", handler)
        await bus.publish(Event(topic="L2.memory.write"))
        assert called == []

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_invoked(self):
        bus = EventBus()
        counters = {"a": 0, "b": 0, "c": 0}

        async def make_handler(name):
            async def _h(ev):
                counters[name] += 1
            return _h

        bus.subscribe("L1.*", await make_handler("a"))
        bus.subscribe("L1.sleep.*", await make_handler("b"))
        bus.subscribe("*", await make_handler("c"))

        delivered = await bus.publish(Event(topic="L1.sleep.start"))
        assert delivered == 3
        assert counters == {"a": 1, "b": 1, "c": 1}

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        received = []

        async def handler(ev):
            received.append(ev.topic)

        unsub = bus.subscribe("L1.*", handler)
        await bus.publish(Event(topic="L1.sleep.start"))
        unsub()
        await bus.publish(Event(topic="L1.sleep.end"))

        assert received == ["L1.sleep.start"]


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


class TestSyncHandlerSupport:
    """Sync handlers must work too — async-only would be a footgun."""

    @pytest.mark.asyncio
    async def test_sync_handler_invoked(self):
        bus = EventBus()
        seen = []
        # NOTE: plain `def`, not `async def`
        def handler(event):
            seen.append(event)
        bus.subscribe("L1.*", handler)
        await bus.publish(Event(topic="L1.foo", source="t", payload={"x": 1}))
        assert len(seen) == 1
        assert seen[0].payload == {"x": 1}
        # delivered count must reflect the sync handler too
        assert bus.stats()["errors"] == 0
        assert bus.stats()["delivered"] == 1

    @pytest.mark.asyncio
    async def test_sync_and_async_handlers_coexist(self):
        bus = EventBus()
        sync_seen = []
        async_seen = []
        def sync_h(e):
            sync_seen.append(e.topic)
        async def async_h(e):
            async_seen.append(e.topic)
        bus.subscribe("L1.*", sync_h)
        bus.subscribe("L1.*", async_h)
        await bus.publish(Event(topic="L1.x", source="t"))
        assert sync_seen == ["L1.x"]
        assert async_seen == ["L1.x"]


class TestEventBusFailureIsolation:
    @pytest.mark.asyncio
    async def test_crashing_handler_does_not_affect_others(self):
        bus = EventBus()
        received = []

        async def crashing(ev):
            raise RuntimeError("boom")

        async def working(ev):
            received.append(ev.topic)

        bus.subscribe("*", crashing)
        bus.subscribe("*", working)

        delivered = await bus.publish(Event(topic="L1.sleep.start"))

        assert received == ["L1.sleep.start"]
        # Only the working handler counts as delivered
        assert delivered == 1
        assert bus.stats()["errors"] == 1

    @pytest.mark.asyncio
    async def test_crashing_handler_does_not_kill_publisher(self):
        bus = EventBus()

        async def crashing(ev):
            raise ValueError("nope")

        bus.subscribe("*", crashing)

        # Publish should NOT raise
        await bus.publish(Event(topic="x"))
        await bus.publish(Event(topic="y"))

        assert bus.stats()["errors"] == 2
        assert bus.stats()["published"] == 2


# ---------------------------------------------------------------------------
# History ring buffer
# ---------------------------------------------------------------------------


class TestEventBusHistory:
    @pytest.mark.asyncio
    async def test_history_records_published_events(self):
        bus = EventBus()
        await bus.publish(Event(topic="L1.sleep.start"))
        await bus.publish(Event(topic="L2.memory.write"))

        hist = bus.history()
        assert len(hist) == 2
        assert hist[0].topic == "L1.sleep.start"
        assert hist[1].topic == "L2.memory.write"

    @pytest.mark.asyncio
    async def test_history_filtered_by_pattern(self):
        bus = EventBus()
        await bus.publish(Event(topic="L1.sleep.start"))
        await bus.publish(Event(topic="L2.memory.write"))
        await bus.publish(Event(topic="L1.sleep.end"))

        hist = bus.history(topic_pattern="L1.*")
        assert [e.topic for e in hist] == ["L1.sleep.start", "L1.sleep.end"]

    @pytest.mark.asyncio
    async def test_history_ring_buffer_bounded(self):
        bus = EventBus(history_size=3)
        for i in range(5):
            await bus.publish(Event(topic=f"e.{i}"))

        hist = bus.history()
        # Only the last 3 retained
        assert [e.topic for e in hist] == ["e.2", "e.3", "e.4"]

    @pytest.mark.asyncio
    async def test_history_limit(self):
        bus = EventBus()
        for i in range(10):
            await bus.publish(Event(topic=f"e.{i}"))

        hist = bus.history(limit=3)
        assert len(hist) == 3
        assert [e.topic for e in hist] == ["e.7", "e.8", "e.9"]


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestEventBusStats:
    @pytest.mark.asyncio
    async def test_stats_count_published_and_delivered(self):
        bus = EventBus()

        async def h(ev): pass

        bus.subscribe("*", h)
        bus.subscribe("*", h)

        await bus.publish(Event(topic="x"))
        await bus.publish(Event(topic="y"))

        stats = bus.stats()
        assert stats["published"] == 2
        assert stats["delivered"] == 4  # 2 events × 2 handlers
        assert stats["errors"] == 0

    @pytest.mark.asyncio
    async def test_clear_resets_everything(self):
        bus = EventBus()

        async def h(ev): pass

        bus.subscribe("*", h)
        await bus.publish(Event(topic="x"))

        bus.clear()
        assert bus.history() == []
        assert bus.stats() == {"published": 0, "delivered": 0, "errors": 0}

        # No subscribers anymore
        n = await bus.publish(Event(topic="y"))
        assert n == 0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_bus_returns_same_instance(self):
        bus1 = get_bus()
        bus2 = get_bus()
        assert bus1 is bus2

    def test_singleton_is_eventbus(self):
        assert isinstance(get_bus(), EventBus)
