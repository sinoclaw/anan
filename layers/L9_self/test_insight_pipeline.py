"""
Integration test: L5 PatternMiner → L9 SelfModel
===============================================

Verifies the full insight pipeline:
  1. Event bus collects events
  2. L5 mines patterns
  3. L9 receives L5.pattern.discovered
  4. Pattern becomes wisdom_fact in SelfModel
  5. L9 emits L9.self.wisdom_grown
"""
import asyncio
import pytest

from kernel.event_bus import EventBus, Event
from layers.L5_reasoning.pattern_miner import PatternMiner
from layers.L9_self.self_model import SelfModel, SelfModelLive


@pytest.fixture
def bus():
    return EventBus()


@pytest.mark.asyncio
async def test_full_insight_pipeline(bus):
    """L5 PatternMiner discovers pattern → L9 stores as wisdom_fact."""
    # --- Setup ---
    miner = PatternMiner(bus=bus, window=3, min_support=2)
    live = SelfModelLive(model=SelfModel())

    # Collect events
    wisdom_events = []

    async def on_wisdom_grown(event):
        wisdom_events.append(event)

    bus.subscribe("L9.self.wisdom_grown", on_wisdom_grown)

    async with live.bound(bus):
        await miner.attach()

        # --- Simulate activity: L1 sleep always followed by L9 updated ---
        for _ in range(3):
            await bus.publish(Event(topic="L1.sleep.started", source="test"))
            await bus.publish(Event(topic="L1.sleep.completed", source="test"))
            await bus.publish(Event(topic="L9.self.updated", source="test"))

        # --- Mine! ---
        patterns = await miner.mine_now()

        # Give event loop a tick to deliver
        await asyncio.sleep(0.01)

    # --- Verify ---
    # L5 found a pattern
    assert len(patterns) > 0, "L5 should have found at least one pattern"
    print(f"✓ L5 discovered {len(patterns)} patterns:")
    for p in patterns:
        print(f"  - {p.antecedent} → {p.consequent} (lift={p.lift:.1f}x)")

    # L9 received it and stored as wisdom
    assert len(live.model.wisdom_facts) > 0, "L9 should have wisdom_facts"
    print(f"✓ L9 now has {len(live.model.wisdom_facts)} wisdom facts")

    # L9 emitted wisdom_grown event
    assert len(wisdom_events) > 0, "L9 should have emitted wisdom_grown event"
    evt = wisdom_events[0]
    assert evt.payload["total_wisdom"] == len(live.model.wisdom_facts)
    print(f"✓ L9 emitted wisdom_grown with payload: {evt.payload}")

    # what_have_i_learned() shows the insight
    learned = live.model.what_have_i_learned()
    assert "我注意到的规律" in learned
    print(f"\n📚 what_have_i_learned() output:\n{learned}")


@pytest.mark.asyncio
async def test_wisdom_dedupe(bus):
    """Same pattern discovered multiple times → only stored once."""
    live = SelfModelLive(model=SelfModel())
    pattern = {
        "antecedent": "L1.sleep.*",
        "consequent": "L9.self.*",
        "confidence": 0.85,
        "lift": 3.2,
        "summary": "L1.sleep.* 之后常出现 L9.self.*（置信=85%，提升=3.2x）",
    }

    async with live.bound(bus):
        # Publish same pattern twice
        for _ in range(2):
            await bus.publish(Event(
                topic="L5.pattern.discovered",
                source="test",
                payload=pattern,
            ))
        await asyncio.sleep(0.01)

    # Only one wisdom fact (deduplicated)
    assert len(live.model.wisdom_facts) == 1


@pytest.mark.asyncio
async def test_add_wisdom_builds_summary_if_missing(bus):
    """Pattern without summary field → add_wisdom builds it."""
    model = SelfModel()
    pattern = {
        "antecedent": "A.*",
        "consequent": "B.*",
        "confidence": 0.75,
        "lift": 2.5,
        # No 'summary' key
    }

    is_new = model.add_wisdom(pattern)
    assert is_new
    assert len(model.wisdom_facts) == 1
    wisdom = model.wisdom_facts[0]
    assert "A.* 之后常出现 B.*" in wisdom
    assert "置信=75%" in wisdom
    assert "提升=2.5x" in wisdom


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
