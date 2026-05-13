"""Tests for L8 IntentStack."""

from __future__ import annotations

import asyncio

import pytest

from kernel.event_bus import Event, EventBus
from layers.L8_intent import Intent, IntentStack


@pytest.fixture
def fresh_bus():
    return EventBus()


class TestProposeAndReinforce:
    @pytest.mark.asyncio
    async def test_propose_adds_intent(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.4)
        await l8.propose("foo", "保持foo")
        intent = l8.get("foo")
        assert intent is not None
        assert intent.strength == pytest.approx(0.4)
        assert intent.reinforce_count == 0

    @pytest.mark.asyncio
    async def test_propose_same_key_reinforces(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.4, reinforce_alpha=0.5)
        await l8.propose("foo", "保持foo")
        await l8.propose("foo", "保持foo")
        intent = l8.get("foo")
        # 0.4 + 0.5*(1-0.4) = 0.4 + 0.3 = 0.7
        assert intent.strength == pytest.approx(0.7)
        assert intent.reinforce_count == 1

    @pytest.mark.asyncio
    async def test_reinforcement_saturates_below_one(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.5, reinforce_alpha=0.5)
        await l8.propose("foo", "x")
        for _ in range(20):
            await l8.propose("foo", "x")
        intent = l8.get("foo")
        assert intent.strength <= 1.0
        assert intent.strength > 0.99

    @pytest.mark.asyncio
    async def test_propose_emits_event(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus)
        events = []
        fresh_bus.subscribe("L8.intent.proposed", lambda e: events.append(e))
        await l8.propose("foo", "x")
        await asyncio.sleep(0.01)
        assert len(events) == 1
        assert events[0].payload["key"] == "foo"

    @pytest.mark.asyncio
    async def test_reinforce_emits_event(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus)
        events = []
        fresh_bus.subscribe("L8.intent.reinforced", lambda e: events.append(e))
        await l8.propose("foo", "x")
        await l8.propose("foo", "x")
        await asyncio.sleep(0.01)
        assert len(events) == 1


class TestDecayAndAbandon:
    @pytest.mark.asyncio
    async def test_decay_lowers_strength(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.5, decay_factor=0.5)
        await l8.propose("foo", "x")
        await l8.decay_tick()
        assert l8.get("foo").strength == pytest.approx(0.25)

    @pytest.mark.asyncio
    async def test_decay_below_floor_abandons(self, fresh_bus):
        l8 = IntentStack(
            bus=fresh_bus, initial_strength=0.1, decay_factor=0.4, abandon_floor=0.05,
        )
        await l8.propose("foo", "x")
        # 0.1 → 0.04 < 0.05
        n = await l8.decay_tick()
        assert n == 1
        assert l8.get("foo") is None
        assert len(l8.history()) == 1
        assert l8.history()[0].key == "foo"

    @pytest.mark.asyncio
    async def test_satisfy_accelerates_abandonment(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.1, abandon_floor=0.05)
        await l8.propose("foo", "x")
        # satisfy multiplies by 0.4 → 0.04 < 0.05 → abandoned
        await l8.satisfy("foo")
        assert l8.get("foo") is None

    @pytest.mark.asyncio
    async def test_abandoned_event_emitted(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.1, decay_factor=0.4)
        events = []
        fresh_bus.subscribe("L8.intent.abandoned", lambda e: events.append(e))
        await l8.propose("foo", "x")
        await l8.decay_tick()
        await asyncio.sleep(0.01)
        assert len(events) == 1
        assert events[0].payload["abandon_reason"] == "decay"


class TestCapacity:
    @pytest.mark.asyncio
    async def test_capacity_drops_weakest(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, capacity=3, initial_strength=0.5)
        await l8.propose("a", "a")
        await l8.propose("b", "b")
        # boost b
        await l8.propose("b", "b")
        await l8.propose("c", "c")
        await l8.propose("d", "d")
        # 4 intents, capacity=3 — weakest should be dropped
        assert len(l8.all_intents()) == 3


class TestQueries:
    @pytest.mark.asyncio
    async def test_top_sorts_by_strength(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.3, reinforce_alpha=0.5)
        await l8.propose("a", "a")
        await l8.propose("b", "b")
        await l8.propose("b", "b")  # b stronger
        top = l8.top(2)
        assert top[0].key == "b"
        assert top[1].key == "a"

    @pytest.mark.asyncio
    async def test_what_do_i_want_empty(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus)
        text = l8.what_do_i_want()
        assert "没什么特别想要" in text

    @pytest.mark.asyncio
    async def test_what_do_i_want_has_top_3(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.5)
        await l8.propose("a", "想a")
        await l8.propose("b", "想b")
        text = l8.what_do_i_want()
        assert "想a" in text
        assert "想b" in text


class TestL7Listener:
    @pytest.mark.asyncio
    async def test_l7_attenuate_creates_balance_intent(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus)
        await l8.attach()
        await fresh_bus.publish(Event(
            topic="L7.regulator.acted", source="L7",
            payload={
                "action": "attenuate_layer_salience",
                "trigger": "L9 占了 90%",
                "detail": {"layer": "L9", "factor": 0.5},
            },
        ))
        await asyncio.sleep(0.02)
        intent = l8.get("keep_attention_balanced")
        assert intent is not None
        assert intent.source == "L7"
        await l8.detach()

    @pytest.mark.asyncio
    async def test_l7_shorten_threshold_creates_grow_intent(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus)
        await l8.attach()
        await fresh_bus.publish(Event(
            topic="L7.regulator.acted", source="L7",
            payload={
                "action": "shorten_sleep_threshold",
                "trigger": "stagnation",
                "detail": {"from": 4.0, "to": 3.5},
            },
        ))
        await asyncio.sleep(0.02)
        assert l8.get("grow_identity") is not None
        await l8.detach()

    @pytest.mark.asyncio
    async def test_repeated_l7_acts_reinforce_intent(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.4, reinforce_alpha=0.5)
        await l8.attach()
        for _ in range(3):
            await fresh_bus.publish(Event(
                topic="L7.regulator.acted", source="L7",
                payload={
                    "action": "attenuate_layer_salience",
                    "trigger": "L9 占主导",
                    "detail": {"layer": "L9", "factor": 0.5},
                },
            ))
            await asyncio.sleep(0.01)
        intent = l8.get("keep_attention_balanced")
        # First propose at 0.4, then 2 reinforcements
        # 0.4 → 0.7 → 0.85
        assert intent.reinforce_count == 2
        assert intent.strength > 0.8
        await l8.detach()


class TestL6Listener:
    @pytest.mark.asyncio
    async def test_l6_attention_issue_becomes_intent(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus)
        await l8.attach()
        await fresh_bus.publish(Event(
            topic="L6.metacognition.report", source="L6",
            payload={
                "score": 0.5, "healthy": False,
                "issues": ["注意力倾斜：L9 层占了 60%"],
                "suggestions": [],
            },
        ))
        await asyncio.sleep(0.02)
        assert l8.get("keep_attention_balanced") is not None
        await l8.detach()

    @pytest.mark.asyncio
    async def test_l6_unmapped_issue_silently_skipped(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus)
        await l8.attach()
        await fresh_bus.publish(Event(
            topic="L6.metacognition.report", source="L6",
            payload={
                "score": 0.9, "healthy": True,
                "issues": ["something obscure unrelated"],
                "suggestions": [],
            },
        ))
        await asyncio.sleep(0.02)
        assert len(l8.all_intents()) == 0
        await l8.detach()


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_emits_event(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.5)
        await l8.propose("a", "a")
        await l8.propose("b", "b")
        events = []
        fresh_bus.subscribe("L8.intent.snapshot", lambda e: events.append(e))
        await l8.snapshot()
        await asyncio.sleep(0.01)
        assert len(events) == 1
        assert events[0].payload["stack_size"] == 2
        assert len(events[0].payload["top_intents"]) == 2
