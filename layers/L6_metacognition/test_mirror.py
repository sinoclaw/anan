"""Tests for L6 metacognition Mirror."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import List

import pytest

from kernel.event_bus import Event, EventBus
from layers.L6_metacognition import Mirror


@pytest.fixture
def fresh_bus():
    return EventBus()


@dataclass
class FakeSelfModel:
    """Stand-in for L9 SelfModel — only the attrs L6 reads."""
    identity_facts: List[str] = field(default_factory=list)
    vision_facts: List[str] = field(default_factory=list)
    history_facts: List[str] = field(default_factory=list)


@dataclass
class FakeWMEntry:
    event: Event


class FakeWM:
    """Stand-in for L3 WorkingMemory."""
    def __init__(self, entries=None):
        self._entries = entries or []
    def snapshot(self):
        return list(self._entries)
    def stats(self):
        return {"size": len(self._entries), "capacity": 100,
                "captured_total": len(self._entries), "evicted_total": 0,
                "half_life_s": 60.0}


class TestBusHealth:
    @pytest.mark.asyncio
    async def test_no_errors_is_healthy(self, fresh_bus):
        # publish a few clean events
        for _ in range(5):
            await fresh_bus.publish(Event(topic="X", source="t", payload={}))
        m = Mirror(bus=fresh_bus)
        report = m.reflect()
        assert report.metrics["bus"]["error_rate"] == 0.0
        assert report.healthy

    @pytest.mark.asyncio
    async def test_high_error_rate_flagged(self, fresh_bus):
        # Manually set stats to simulate errors
        fresh_bus._stats["published"] = 100
        fresh_bus._stats["errors"] = 10  # 10% error rate
        m = Mirror(bus=fresh_bus)
        report = m.reflect()
        assert report.metrics["bus"]["error_rate"] == 0.1
        assert any("严重" in i for i in report.issues)
        assert not report.healthy


class TestSelfStagnation:
    @pytest.mark.asyncio
    async def test_growth_resets_stagnation(self, fresh_bus):
        sm = FakeSelfModel(identity_facts=["I am anan"])
        m = Mirror(bus=fresh_bus, self_model=sm, identity_stagnation_cycles=2)
        m.reflect()  # first one — baseline
        sm.identity_facts.append("I am autonomous")
        r2 = m.reflect()
        assert r2.metrics["self"]["stagnation_streak"] == 0

    @pytest.mark.asyncio
    async def test_stagnation_eventually_flagged(self, fresh_bus):
        sm = FakeSelfModel(identity_facts=["I am anan"])
        m = Mirror(bus=fresh_bus, self_model=sm, identity_stagnation_cycles=2)
        m.reflect()                       # baseline (streak=0)
        m.reflect()                       # streak=1 — quiet
        report = m.reflect()              # streak=2 — flagged
        assert report.metrics["self"]["stagnation_streak"] >= 2
        assert any("身份" in i for i in report.issues)

    @pytest.mark.asyncio
    async def test_no_identity_at_all_warns(self, fresh_bus):
        sm = FakeSelfModel()  # empty
        m = Mirror(bus=fresh_bus, self_model=sm)
        report = m.reflect()
        assert any("我是谁" in i for i in report.issues)


class TestAttention:
    @pytest.mark.asyncio
    async def test_balanced_attention_ok(self, fresh_bus):
        entries = [FakeWMEntry(Event(topic=f"L{i}.x", source="t", payload={}))
                   for i in (1, 2, 3, 9)]
        m = Mirror(bus=fresh_bus, working_memory=FakeWM(entries))
        report = m.reflect()
        assert "注意力" not in " ".join(report.issues)

    @pytest.mark.asyncio
    async def test_skewed_attention_flagged(self, fresh_bus):
        # 9 out of 10 entries from L9 → 90% > 70% threshold
        entries = [FakeWMEntry(Event(topic="L9.self.updated", source="t", payload={}))
                   for _ in range(9)]
        entries.append(FakeWMEntry(Event(topic="L0.tick", source="t", payload={})))
        m = Mirror(bus=fresh_bus, working_memory=FakeWM(entries))
        report = m.reflect()
        joined = " ".join(report.issues)
        assert "注意力倾斜" in joined
        assert "L9" in joined

    @pytest.mark.asyncio
    async def test_empty_wm_warns(self, fresh_bus):
        m = Mirror(bus=fresh_bus, working_memory=FakeWM())
        report = m.reflect()
        assert any("空" in i for i in report.issues)


class TestEmitting:
    @pytest.mark.asyncio
    async def test_attach_subscribes_to_asleep(self, fresh_bus):
        m = Mirror(bus=fresh_bus)
        await m.attach()
        captured = []
        fresh_bus.subscribe("L6.metacognition.report", lambda e: captured.append(e))

        await fresh_bus.publish(Event(topic="L0.circadian.asleep",
                                      source="t", payload={"cycle": 1}))
        await asyncio.sleep(0.02)

        assert len(captured) == 1
        assert "score" in captured[0].payload
        await m.detach()

    @pytest.mark.asyncio
    async def test_warn_event_when_unhealthy(self, fresh_bus):
        # Force unhealthy: very high error rate
        fresh_bus._stats["published"] = 100
        fresh_bus._stats["errors"] = 10
        m = Mirror(bus=fresh_bus)
        warns = []
        fresh_bus.subscribe("L6.metacognition.warn", lambda e: warns.append(e))
        await m.reflect_and_emit()
        await asyncio.sleep(0.01)
        assert len(warns) == 1


class TestHistory:
    @pytest.mark.asyncio
    async def test_history_accumulates(self, fresh_bus):
        m = Mirror(bus=fresh_bus)
        for _ in range(3):
            m.reflect()
        assert len(m.history()) == 3
        assert m.latest() is m.history()[-1]
