"""
L5 PatternMiner 测试套件
==============================
测试 Pattern 数据类，以及 PatternMiner 的：
  - 初始化
  - 抽象 (topic abstraction)
  - 模式挖掘
  - 去重/冷却
  - discovered / stats
"""
import asyncio
import pytest
from datetime import datetime, timedelta
from kernel.event_bus import EventBus, Event
from layers.L5_reasoning.pattern_miner import Pattern, _Discovered, PatternMiner


# ---------------------------------------------------------------------------
# DataClass tests
# ---------------------------------------------------------------------------

class TestPattern:
    def test_fields(self):
        p = Pattern(
            antecedent="L3.attention.shift",
            consequent="L8.drive.suggestion",
            support=10,
            confidence=0.75,
            lift=2.0,
        )
        assert p.antecedent == "L3.attention.shift"
        assert p.consequent == "L8.drive.suggestion"
        assert p.support == 10
        assert p.confidence == 0.75
        assert p.lift == 2.0

    def test_frozen(self):
        p = Pattern("A", "B", 1, 0.5, 1.0)
        with pytest.raises(Exception):  # frozen dataclass is immutable
            p.support = 99


class TestDiscovered:
    def test_init(self):
        p = Pattern("A", "B", 1, 0.5, 1.0)
        d = _Discovered(p, datetime.now())
        assert d.pattern == p
        assert d.last_emitted_at is not None


# ---------------------------------------------------------------------------
# PatternMiner tests
# ---------------------------------------------------------------------------

class TestPatternMinerInit:
    def test_defaults(self):
        pm = PatternMiner()
        assert pm._window == 5
        assert pm._min_support == 2
        assert pm._min_confidence == 0.6
        assert pm._min_lift == 1.5
        assert pm._cooldown == timedelta(seconds=30.0)

    def test_custom_params(self):
        pm = PatternMiner(window=10, min_support=5, min_confidence=0.8, min_lift=2.0, cooldown_s=60.0)
        assert pm._window == 10
        assert pm._min_support == 5
        assert pm._min_confidence == 0.8
        assert pm._min_lift == 2.0
        assert pm._cooldown == timedelta(seconds=60.0)

    def test_bus_assignment(self):
        bus = EventBus()
        pm = PatternMiner(bus=bus)
        assert pm._bus is bus

    def test_default_abstract(self):
        pm = PatternMiner()
        abstract = pm._default_abstract
        # Drops the last segment from any multi-segment topic
        assert abstract("L9.self.wisdom_grown") == "L9.self.*"
        assert abstract("L9.self.reflected") == "L9.self.*"
        assert abstract("L7.regulator.acted") == "L7.regulator.*"
        assert abstract("L3.attention.shift") == "L3.attention.*"
        # Single-segment topics pass through
        assert abstract("tick") == "tick"
        assert abstract("single") == "single"


class TestTopicAbstraction:
    def test_abstract_drops_last_segment(self):
        pm = PatternMiner()
        abstract = pm._default_abstract
        # All multi-segment topics have last segment dropped → wildcard
        assert abstract("L3.attention.shift") == "L3.attention.*"
        assert abstract("L7.regulator.acted") == "L7.regulator.*"
        assert abstract("L9.self.wisdom_grown") == "L9.self.*"
        # Single-segment stays as-is
        assert abstract("tick") == "tick"


class TestDiscoveredAndStats:
    def test_discovered_empty_initially(self):
        pm = PatternMiner()
        assert pm.discovered() == []

    def test_stats_empty(self):
        pm = PatternMiner()
        stats = pm.stats()
        assert stats["mine_count"] == 0
        assert stats["patterns_discovered"] == 0

    @pytest.mark.asyncio
    async def test_mine_now_empty_bus(self):
        bus = EventBus()
        pm = PatternMiner(bus=bus)
        patterns = await pm.mine_now()
        assert patterns == []


class TestPatternDiscovery:
    @pytest.mark.asyncio
    async def test_single_pattern_discovered(self):
        """Mine after some events fire — at least one pattern should surface."""
        bus = EventBus()

        # Use a custom abstractor that maps these test topics to a non-periodic
        # abstract, bypassing the KNOWN_PERIODIC and dynamic periodic filters.
        def non_periodic_abstract(topic: str) -> str:
            return f"test.{topic.split('.')[1]}.*"

        pm = PatternMiner(
            bus=bus,
            window=5,
            min_support=2,
            min_confidence=0.5,
            min_lift=1.0,
            cooldown_s=0.1,
            topic_abstractor=non_periodic_abstract,
            min_interval_std_s=0.0,  # disable periodic detection filter in this test
        )
        await pm.attach()

        # Fire events that create a pattern: A always followed by B within window.
        import random
        for _ in range(5):
            await bus.publish(Event(topic="L4.thought.generated", source="test", payload={}))
            await asyncio.sleep(0.01)
            await bus.publish(Event(topic="L7.goal.achieved", source="test", payload={}))
            await asyncio.sleep(random.uniform(0.01, 0.09))

        patterns = await pm.mine_now()

        # Should discover test.thought.* → test.goal.* via custom abstractor
        antecedents = [p.antecedent for p in patterns]
        consequents = [p.consequent for p in patterns]
        assert "test.thought.*" in antecedents
        assert "test.goal.*" in consequents

        await pm.detach()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_duplicate(self):
        """Same pattern within cooldown window should not be emitted twice."""
        bus = EventBus()
        pm = PatternMiner(
            bus=bus,
            window=5,
            min_support=2,
            min_confidence=0.5,
            min_lift=1.0,
            cooldown_s=10.0,  # long cooldown
        )
        await pm.attach()

        for _ in range(3):
            await bus.publish(Event(topic="L3.attention.shift", source="test", payload={}))
            await asyncio.sleep(0.01)
            await bus.publish(Event(topic="L8.drive.suggestion", source="test", payload={}))
            await asyncio.sleep(0.01)

        patterns1 = await pm.mine_now()
        patterns2 = await pm.mine_now()

        # First call discovers patterns
        assert len(patterns1) >= 1
        # Second call during cooldown returns empty (duplicate suppressed)
        assert len(patterns2) == 0

        await pm.detach()

    @pytest.mark.asyncio
    async def test_stats_after_mining(self):
        bus = EventBus()

        def non_periodic_abstract(topic: str) -> str:
            return f"test.{topic.split('.')[1]}.*"

        pm = PatternMiner(
            bus=bus,
            window=5,
            min_support=2,
            min_confidence=0.5,
            min_lift=1.0,
            topic_abstractor=non_periodic_abstract,
            min_interval_std_s=0.0,  # disable periodic detection filter in this test
        )
        await pm.attach()

        import random
        for _ in range(5):
            await bus.publish(Event(topic="L4.thought.generated", source="test", payload={}))
            await asyncio.sleep(0.01)
            await bus.publish(Event(topic="L7.goal.achieved", source="test", payload={}))
            await asyncio.sleep(random.uniform(0.01, 0.09))

        patterns = await pm.mine_now()
        stats = pm.stats()

        assert stats["patterns_discovered"] == len(patterns)
        assert stats["patterns_discovered"] >= 1

        await pm.detach()

    @pytest.mark.asyncio
    async def test_low_support_filtered(self):
        """Patterns with support < min_support should not appear."""
        bus = EventBus()
        pm = PatternMiner(
            bus=bus,
            window=5,
            min_support=10,  # high threshold
            min_confidence=0.5,
            min_lift=1.0,
        )
        await pm.attach()

        # Only fire 3 times — below min_support=10
        for _ in range(3):
            await bus.publish(Event(topic="L3.attention.shift", source="test", payload={}))
            await asyncio.sleep(0.01)
            await bus.publish(Event(topic="L8.drive.suggestion", source="test", payload={}))

        patterns = await pm.mine_now()

        # Should be filtered out by support
        assert len(patterns) == 0

        await pm.detach()


class TestPatternMinerAttach:
    @pytest.mark.asyncio
    async def test_attach_idempotent(self):
        bus = EventBus()
        pm = PatternMiner(bus=bus)
        await pm.attach()
        first = len(pm._unsubs)
        await pm.attach()
        assert len(pm._unsubs) == first
        await pm.detach()

    @pytest.mark.asyncio
    async def test_detach_clears_unsubs(self):
        bus = EventBus()
        pm = PatternMiner(bus=bus)
        await pm.attach()
        await pm.detach()
        assert len(pm._unsubs) == 0


# --------------------------------------------------------------------------


class MockSelfModel:
    def __init__(self):
        self.history_facts = []


class TestPatternMinerSelfModel:
    @pytest.mark.asyncio
    async def test_self_model_param_accepted(self):
        bus = EventBus()
        sm = MockSelfModel()
        pm = PatternMiner(bus=bus, self_model=sm)
        assert pm._sm is sm

    @pytest.mark.asyncio
    async def test_no_self_model_is_fine(self):
        bus = EventBus()
        pm = PatternMiner(bus=bus)
        assert pm._sm is None

    @pytest.mark.asyncio
    async def test_pattern_writes_to_self_model(self):
        """When a pattern is discovered, it should be written to self_model.history_facts."""
        bus = EventBus()
        sm = MockSelfModel()
        pm = PatternMiner(
            bus=bus,
            self_model=sm,
            window=5,
            min_support=1,
            min_confidence=0.5,
            min_lift=0.5,
            cooldown_s=0.0,
        )
        await pm.attach()

        # Push events to create a pattern
        await bus.publish(Event(topic="L3.attention.shift", source="test"))
        await bus.publish(Event(topic="L3.attention.shift", source="test"))
        await bus.publish(Event(topic="L8.intent.proposed", source="test"))

        patterns = await pm.mine_now()

        if patterns:
            assert len(sm.history_facts) >= 1
            fact = sm.history_facts[-1]
            assert "模式" in fact or "pattern" in fact.lower()
            assert "L3" in fact or "L8" in fact

        await pm.detach()
