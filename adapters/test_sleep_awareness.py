"""Tests for adapters.sleep_awareness — anan's first cognitive signal layer."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kernel.event_bus import Event, EventBus
from adapters.sleep_awareness import make_aware, run_with_awareness


# ---------------------------------------------------------------------------
# run_with_awareness — direct invocation
# ---------------------------------------------------------------------------


class TestRunWithAwareness:
    @pytest.mark.asyncio
    async def test_emits_start_and_consolidated(self):
        bus = EventBus()
        events = []
        bus.subscribe("L1.sleep.*", lambda ev: _record(events, ev))

        async def fake_phase():
            return {"recall_count": 7}

        result = await run_with_awareness("light", fake_phase, _anan_day="2026-05-14", _anan_bus=bus)

        assert result == {"recall_count": 7}
        topics = [e.topic for e in events]
        assert topics == ["L1.sleep.light.start", "L1.sleep.light.consolidated"]
        assert events[1].payload["recall_count"] == 7
        assert events[1].payload["day"] == "2026-05-14"

    @pytest.mark.asyncio
    async def test_emits_failed_and_reraises(self):
        bus = EventBus()
        events = []
        bus.subscribe("L1.sleep.*", lambda ev: _record(events, ev))

        async def crashing_phase():
            raise RuntimeError("dream interrupted")

        with pytest.raises(RuntimeError, match="dream interrupted"):
            await run_with_awareness("rem", crashing_phase, _anan_day="2026-05-14", _anan_bus=bus)

        topics = [e.topic for e in events]
        assert topics == ["L1.sleep.rem.start", "L1.sleep.rem.failed"]
        assert "dream interrupted" in events[1].payload["error"]

    @pytest.mark.asyncio
    async def test_recall_count_is_none_for_non_dict_result(self):
        bus = EventBus()
        events = []
        bus.subscribe("L1.sleep.*.consolidated", lambda ev: _record(events, ev))

        async def fake_phase():
            return "just a string, no dict"

        await run_with_awareness("deep", fake_phase, _anan_bus=bus)
        assert events[0].payload["recall_count"] is None

    @pytest.mark.asyncio
    async def test_duration_is_recorded(self):
        bus = EventBus()
        events = []
        bus.subscribe("L1.sleep.*.consolidated", lambda ev: _record(events, ev))

        async def slow_phase():
            await asyncio.sleep(0.05)
            return {}

        await run_with_awareness("light", slow_phase, _anan_bus=bus)
        assert events[0].payload["duration_s"] >= 0.05

    @pytest.mark.asyncio
    async def test_uses_global_bus_by_default(self):
        from kernel.event_bus import get_bus
        global_bus = get_bus()
        global_bus.clear()

        async def fake_phase():
            return {}

        await run_with_awareness("light", fake_phase)
        hist = global_bus.history(topic_pattern="L1.sleep.*")
        # At least the start + consolidated for our phase
        assert any(e.topic == "L1.sleep.light.start" for e in hist)
        assert any(e.topic == "L1.sleep.light.consolidated" for e in hist)


# ---------------------------------------------------------------------------
# make_aware — decorator pattern
# ---------------------------------------------------------------------------


class TestMakeAware:
    @pytest.mark.asyncio
    async def test_decorated_function_emits_events(self):
        bus = EventBus()
        events = []
        bus.subscribe("L1.sleep.*", lambda ev: _record(events, ev))

        async def fake_run_light(config, day=None):
            return {"phase": "light", "day": day, "recall_count": 3}

        aware = make_aware("light", fake_run_light, bus=bus)
        result = await aware({"key": "val"}, day="2026-05-14")

        assert result["recall_count"] == 3
        topics = [e.topic for e in events]
        assert topics == ["L1.sleep.light.start", "L1.sleep.light.consolidated"]

    @pytest.mark.asyncio
    async def test_wrapped_preserves_metadata(self):
        async def fake_run_deep():
            """Deep sleep mechanics."""
            return {}

        aware = make_aware("deep", fake_run_deep)
        assert aware.__name__ == "aware_deep_fake_run_deep"
        assert "Deep sleep mechanics" in aware.__doc__


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _record(target_list, event):
    """Async handler that records events into a list."""
    target_list.append(event)
