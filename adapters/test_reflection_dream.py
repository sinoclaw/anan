"""Tests for adapters.reflection_dream — heuristic reflection from bus history."""

from __future__ import annotations

import asyncio

import pytest

from adapters.reflection_dream import (
    reflect_deep,
    reflect_light,
    reflect_rem,
    reflective_sleep_cycle,
)
from kernel.event_bus import Event, EventBus


@pytest.fixture
def fresh_bus():
    return EventBus()


async def _seed_one_cycle(bus: EventBus, cycle: int, ticks: int = 5):
    """Inject a fake cycle's worth of events into the bus."""
    await bus.publish(Event(
        topic="L0.circadian.wake", source="test",
        payload={"cycle": cycle, "day": "2026-05-14"},
    ))
    for i in range(ticks):
        await bus.publish(Event(
            topic="L0.circadian.tick", source="test",
            payload={"cycle": cycle, "fatigue": float(i + 1), "elapsed_s": (i + 1) * 0.05, "ticks": i + 1},
        ))
    await bus.publish(Event(
        topic="L0.circadian.bedtime", source="test",
        payload={"cycle": cycle, "fatigue": float(ticks)},
    ))


class TestReflectLight:
    @pytest.mark.asyncio
    async def test_counts_events_in_cycle(self, fresh_bus):
        await _seed_one_cycle(fresh_bus, cycle=1, ticks=3)
        result = reflect_light(fresh_bus, day="2026-05-14", cycle=1)
        assert result["phase"] == "light"
        # 1 wake + 3 ticks + 1 bedtime = 5 events
        assert result["recall_count"] == 5
        # First fact should mention the count
        assert "经历了 5 个事件" in result["consolidated_facts"][0]

    @pytest.mark.asyncio
    async def test_ignores_other_cycles(self, fresh_bus):
        await _seed_one_cycle(fresh_bus, cycle=1, ticks=2)
        await _seed_one_cycle(fresh_bus, cycle=2, ticks=4)
        result = reflect_light(fresh_bus, day="2026-05-14", cycle=2)
        # cycle 2 = 1 wake + 4 ticks + 1 bedtime = 6
        assert result["recall_count"] == 6


class TestReflectRem:
    @pytest.mark.asyncio
    async def test_narrative_includes_fatigue_and_ticks(self, fresh_bus):
        await _seed_one_cycle(fresh_bus, cycle=1, ticks=5)
        result = reflect_rem(fresh_bus, day="2026-05-14", cycle=1)
        assert result["phase"] == "rem"
        joined = " ".join(result["consolidated_facts"])
        assert "5 个心跳" in joined
        assert "5.0" in joined  # max_fatigue
        assert result["dream"]
        assert "周期 1" in result["dream"]

    @pytest.mark.asyncio
    async def test_empty_cycle_falls_back_gracefully(self, fresh_bus):
        result = reflect_rem(fresh_bus, day="2026-05-14", cycle=999)
        assert result["consolidated_facts"]
        assert "没什么可叙事的" in result["consolidated_facts"][0]


class TestReflectDeep:
    @pytest.mark.asyncio
    async def test_first_cycle_is_first_cycle(self, fresh_bus):
        await fresh_bus.publish(Event(
            topic="L0.circadian.asleep", source="t",
            payload={"cycle": 1, "dream_facts_count": 3},
        ))
        result = reflect_deep(fresh_bus, day="d", cycle=1)
        joined = " ".join(result["consolidated_facts"])
        assert "第一个完整周期" in joined
        # Always includes the vision fact
        assert "核心愿景" in joined

    @pytest.mark.asyncio
    async def test_multiple_cycles_makes_identity_claim(self, fresh_bus):
        for c in range(1, 4):
            await fresh_bus.publish(Event(
                topic="L0.circadian.asleep", source="t",
                payload={"cycle": c, "dream_facts_count": 1},
            ))
        result = reflect_deep(fresh_bus, day="d", cycle=3)
        joined = " ".join(result["consolidated_facts"])
        assert "3 个完整周期" in joined
        assert "陈亦安" in joined


class TestReflectiveSleepCycle:
    """End-to-end: reflective_sleep_cycle goes through run_with_awareness
    so L1.sleep.* events fire and L2 can pick them up."""

    @pytest.mark.asyncio
    async def test_emits_three_phase_events(self, fresh_bus):
        await _seed_one_cycle(fresh_bus, cycle=1, ticks=3)
        seen: list[str] = []
        fresh_bus.subscribe("L1.sleep.*", lambda e: seen.append(e.topic))

        # Publish completion event so deep can see at least one cycle done
        await fresh_bus.publish(Event(
            topic="L0.circadian.asleep", source="t",
            payload={"cycle": 1, "dream_facts_count": 0},
        ))

        total = await reflective_sleep_cycle("2026-05-14", fresh_bus, cycle=1)
        await asyncio.sleep(0.05)

        # All three phases fired start + consolidated
        assert "L1.sleep.light.start" in seen
        assert "L1.sleep.light.consolidated" in seen
        assert "L1.sleep.rem.start" in seen
        assert "L1.sleep.rem.consolidated" in seen
        assert "L1.sleep.deep.start" in seen
        assert "L1.sleep.deep.consolidated" in seen
        # Returned a positive count
        assert total > 0
