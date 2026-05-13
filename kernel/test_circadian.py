"""Tests for kernel.circadian — the main heartbeat loop."""

from __future__ import annotations

import asyncio

import pytest

from kernel.circadian import CircadianConfig, CircadianLoop
from kernel.event_bus import Event, EventBus


@pytest.fixture
def fresh_bus():
    return EventBus()


# Sleep functions for testing
async def _no_op_sleep(day, bus, cycle):
    return 0


async def _counting_sleep(day, bus, cycle):
    """Returns cycle number * 2 to make it easy to verify."""
    return cycle * 2


async def _crashing_sleep(day, bus, cycle):
    raise RuntimeError(f"sleep failed in cycle {cycle}")


class TestCircadianBasic:
    @pytest.mark.asyncio
    async def test_runs_max_cycles_and_stops(self, fresh_bus):
        loop = CircadianLoop(
            sleep_fn=_no_op_sleep,
            config=CircadianConfig(
                tick_interval_s=0.001, fatigue_per_tick=2.0,
                sleep_threshold=4.0, max_cycles=2,
            ),
            bus=fresh_bus,
        )
        log = await loop.run()
        assert len(log) == 2
        assert log[0]["cycle"] == 1
        assert log[1]["cycle"] == 2

    @pytest.mark.asyncio
    async def test_emits_lifecycle_events_per_cycle(self, fresh_bus):
        seen: list[str] = []
        fresh_bus.subscribe("L0.circadian.*", lambda e: seen.append(e.topic))

        loop = CircadianLoop(
            sleep_fn=_no_op_sleep,
            config=CircadianConfig(
                tick_interval_s=0.001, fatigue_per_tick=5.0,
                sleep_threshold=4.0, max_cycles=1,
            ),
            bus=fresh_bus,
        )
        await loop.run()
        await asyncio.sleep(0.02)

        # Should see: wake → tick(s) → bedtime → asleep
        assert "L0.circadian.wake" in seen
        assert "L0.circadian.tick" in seen
        assert "L0.circadian.bedtime" in seen
        assert "L0.circadian.asleep" in seen
        # Order matters: wake before bedtime
        assert seen.index("L0.circadian.wake") < seen.index("L0.circadian.bedtime")

    @pytest.mark.asyncio
    async def test_fatigue_resets_each_cycle(self, fresh_bus):
        loop = CircadianLoop(
            sleep_fn=_no_op_sleep,
            config=CircadianConfig(
                tick_interval_s=0.001, fatigue_per_tick=1.0,
                sleep_threshold=3.0, max_cycles=2,
            ),
            bus=fresh_bus,
        )
        await loop.run()
        # After loop ends, fatigue is 0 (reset at start of next cycle which never began)
        # but each completed cycle should have ~3 ticks
        assert loop._cycle_log[0]["ticks"] >= 3
        assert loop._cycle_log[1]["ticks"] >= 3

    @pytest.mark.asyncio
    async def test_dream_facts_count_propagates(self, fresh_bus):
        seen: list = []
        fresh_bus.subscribe("L0.circadian.asleep", lambda e: seen.append(e.payload))

        loop = CircadianLoop(
            sleep_fn=_counting_sleep,
            config=CircadianConfig(
                tick_interval_s=0.001, fatigue_per_tick=5.0,
                sleep_threshold=4.0, max_cycles=2,
            ),
            bus=fresh_bus,
        )
        await loop.run()
        await asyncio.sleep(0.02)

        assert seen[0]["dream_facts_count"] == 2  # cycle 1 * 2
        assert seen[1]["dream_facts_count"] == 4  # cycle 2 * 2


class TestCircadianStop:
    @pytest.mark.asyncio
    async def test_stop_during_active_phase_skips_sleep(self, fresh_bus):
        loop = CircadianLoop(
            sleep_fn=_no_op_sleep,
            config=CircadianConfig(
                tick_interval_s=0.01, fatigue_per_tick=0.1,  # slow fatigue
                sleep_threshold=100.0,                       # never naturally sleeps
                max_cycles=10,
            ),
            bus=fresh_bus,
        )
        task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.05)
        loop.stop()
        log = await task
        # Should have cancelled mid-cycle, NO completed cycles in log
        assert log == []

    @pytest.mark.asyncio
    async def test_zero_max_cycles_runs_nothing(self, fresh_bus):
        loop = CircadianLoop(
            sleep_fn=_no_op_sleep,
            config=CircadianConfig(max_cycles=0),
            bus=fresh_bus,
        )
        log = await loop.run()
        assert log == []


class TestCircadianFailureIsolation:
    @pytest.mark.asyncio
    async def test_sleep_crash_does_not_kill_loop(self, fresh_bus):
        loop = CircadianLoop(
            sleep_fn=_crashing_sleep,
            config=CircadianConfig(
                tick_interval_s=0.001, fatigue_per_tick=5.0,
                sleep_threshold=4.0, max_cycles=2,
            ),
            bus=fresh_bus,
        )
        log = await loop.run()
        assert len(log) == 2
        # Both cycles marked as failed (-1) but loop survived
        assert log[0]["dream_facts_count"] == -1
        assert log[1]["dream_facts_count"] == -1
