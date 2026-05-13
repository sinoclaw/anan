"""Tests for L7 proactive regulation using L5 causal insights."""

from __future__ import annotations

import asyncio

import pytest

from kernel.event_bus import Event, EventBus
from layers.L3_working_memory import WorkingMemory
from layers.L5_reasoning import PatternMiner
from layers.L7_will import SelfRegulator


@pytest.fixture
def fresh_bus():
    return EventBus()


@pytest.mark.asyncio
async def test_l5_pattern_triggers_preemptive_action(fresh_bus):
    """L5 discovers X always causes Y (bad) → L7 preemptively acts before Y happens."""
    wm = WorkingMemory(capacity=20)
    await wm.attach(fresh_bus)
    l7 = SelfRegulator(bus=fresh_bus, working_memory=wm)
    await l7.attach()
    l5 = PatternMiner(bus=fresh_bus, window=3, min_support=2, min_confidence=0.8)
    await l5.attach()

    captured_actions = []
    fresh_bus.subscribe("L7.regulator.acted", lambda e: captured_actions.append(e))

    # Step 1: Teach L5 the pattern: L9.self.* always leads to L6.metacognition.*
    # Don't send real L6.warn events (to avoid reactive action) — send dummy L6.metacognition.other
    for _ in range(3):
        await fresh_bus.publish(Event(topic="L9.self.updated", source="test", payload={}))
        await fresh_bus.publish(Event(topic="L6.metacognition.other", source="test",
                                    payload={"score": 0.5, "issues": [], "suggestions": []}))
        await asyncio.sleep(0.01)

    # Let L5 mine
    patterns = await l5.mine_now()
    assert len(patterns) > 0
    l9_to_l6 = [p for p in patterns if "L9.self" in p.antecedent and "L6.metacognition" in p.consequent]
    assert len(l9_to_l6) > 0, "L5 should discover L9 → L6 pattern"

    # L7 should have preemptively attenuated L9 when L5 published the pattern
    assert "L9" in l7._layer_atten, "L7 should preemptively attenuate L9 based on L5 insight"
    assert len(captured_actions) >= 1, "L7 should have emitted a proactive action event"
    proactive_actions = [a for a in captured_actions if "proactive" in a.payload["detail"]["rationale"]]
    assert len(proactive_actions) > 0, "Should have proactive action"

    await l5.detach()
    await l7.detach()
    await wm.detach()


@pytest.mark.asyncio
async def test_only_high_confidence_patterns_trigger_action(fresh_bus):
    """Only high-confidence patterns should trigger preemptive action."""
    wm = WorkingMemory(capacity=20)
    await wm.attach(fresh_bus)
    l7 = SelfRegulator(bus=fresh_bus, working_memory=wm)
    await l7.attach()
    l5 = PatternMiner(bus=fresh_bus, window=3, min_support=2, min_confidence=0.5)
    await l5.attach()

    # Send mixed pattern: L9 -> warn only 1 out of 2 times (low confidence)
    for i in range(2):
        await fresh_bus.publish(Event(topic="L9.self.updated", source="test", payload={}))
        if i == 0:  # Only first L9 causes warn
            await fresh_bus.publish(Event(topic="L6.metacognition.warn", source="test",
                                        payload={"score": 0.5, "issues": ["注意力倾斜：L9 层占了 90%"],
                                                 "suggestions": []}))
        await asyncio.sleep(0.01)

    await l5.mine_now()

    # Send another L9 - should NOT trigger action because confidence too low
    initial_atten = l7._layer_atten.copy()
    await fresh_bus.publish(Event(topic="L9.self.updated", source="test", payload={}))
    await asyncio.sleep(0.05)

    assert l7._layer_atten == initial_atten, "No action should be taken for low-confidence patterns"

    await l5.detach()
    await l7.detach()
    await wm.detach()
