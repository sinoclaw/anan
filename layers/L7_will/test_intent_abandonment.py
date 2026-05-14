"""Tests for L5 causal insights guiding L8 intent abandonment."""

from __future__ import annotations

import asyncio

import pytest

from kernel.event_bus import Event, EventBus
from layers.L5_reasoning import PatternMiner
from layers.L7_will import SelfRegulator
from layers.L8_intent import IntentStack


@pytest.fixture
def fresh_bus():
    return EventBus()


@pytest.mark.asyncio
async def test_l5_pattern_triggers_intent_weaken(fresh_bus):
    """L5 discovers L8.intent.* → L4.observation.* pattern → L7 publishes weaken_intent → L8 weakens intent."""
    l8 = IntentStack(bus=fresh_bus, capacity=7)
    await l8.attach()
    l7 = SelfRegulator(bus=fresh_bus)
    await l7.attach()
    l5 = PatternMiner(bus=fresh_bus, window=3, min_support=2, min_confidence=0.8)
    await l5.attach()

    weakened = []
    abandoned = []
    fresh_bus.subscribe("L8.intent.weakened", lambda e: weakened.append(e))
    fresh_bus.subscribe("L8.intent.abandoned", lambda e: abandoned.append(e))

    # Step 1: Create events via propose (which counts reinforce) + falsify
    # Intent created via L7.acted → L8.propose → reinforce_count increments
    intent_key = "persistent_goal"
    for _ in range(3):
        await fresh_bus.publish(Event(topic="L7.regulator.acted", source="test",
                            payload={"action": "attenuate_layer_salience",
                                     "detail": {"layer": "L9", "rationale": "test"}}))
        await fresh_bus.publish(Event(topic="L4.observation.falsified", source="test",
                            payload={"intent_key": intent_key, "verdict": "failed", "reason": "失败"}))
        await asyncio.sleep(0.01)

    # Step 2: L5 mines → discovers pattern
    patterns = await l5.mine_now()
    # The pattern is: L8.intent.* → L4.observation.* (intent leads to observation/failure)
    l8_to_l4 = [p for p in patterns
                if "L8.intent" in p.antecedent and "L4.observation" in p.consequent]
    assert len(l8_to_l4) > 0, f"L5 should discover L8→L4 pattern. Got: {[(p.antecedent, p.consequent) for p in patterns]}"

    # Step 3: L7 should have published weaken_intent signal
    await asyncio.sleep(0.05)
    assert len(weakened) + len(abandoned) > 0, \
        "L7 should have sent weaken_intent, L8 should have acted on it"

    await l5.detach()
    await l7.detach()
    await l8.detach()


@pytest.mark.asyncio
async def test_only_strong_patterns_trigger_weaken(fresh_bus):
    """Low-support patterns don't trigger intent weakening."""
    l8 = IntentStack(bus=fresh_bus, capacity=7)
    await l8.attach()
    l7 = SelfRegulator(bus=fresh_bus)
    await l7.attach()
    l5 = PatternMiner(bus=fresh_bus, window=3, min_support=3, min_confidence=0.8)
    await l5.attach()

    acted = []
    fresh_bus.subscribe("L7.regulator.acted", lambda e: acted.append(e))

    # Only 2 failures (below min_support=3)
    for _ in range(2):
        await fresh_bus.publish(Event(topic="L8.intent.proposed", source="test",
                                    payload={"key": "rare_attempt", "strength": 0.5}))
        await fresh_bus.publish(Event(topic="L4.observation.falsified", source="test",
                                    payload={"intent_key": "rare_attempt",
                                             "verdict": "failed", "reason": "失败"}))
        await asyncio.sleep(0.01)

    await l5.mine_now()
    await asyncio.sleep(0.05)

    # No L7 action because pattern not strong enough
    weaken = [e for e in acted if e.payload.get("action") == "weaken_intent"]
    assert len(weaken) == 0, "No action for low-support patterns"

    await l5.detach()
    await l7.detach()
    await l8.detach()
