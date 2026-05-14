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
        pm = PatternMiner(
            bus=bus,
            window=5,
            min_support=2,
            min_confidence=0.5,
            min_lift=1.0,
            cooldown_s=0.1,
        )
        await pm.attach()

        # Fire events that create a pattern: A always followed by B within window
        for _ in range(5):
            await bus.publish(Event(topic="L3.attention.shift", source="test", payload={}))
            await asyncio.sleep(0.01)
            await bus.publish(Event(topic="L8.drive.suggestion", source="test", payload={}))
            await asyncio.sleep(0.01)

        patterns = await pm.mine_now()

        # Should discover L3.attention.* → L8.drive.* (abstracted)
        antecedents = [p.antecedent for p in patterns]
        consequents = [p.consequent for p in patterns]
        assert "L3.attention.*" in antecedents
        assert "L8.drive.*" in consequents

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
        pm = PatternMiner(
            bus=bus,
            window=5,
            min_support=2,
            min_confidence=0.5,
            min_lift=1.0,
        )
        await pm.attach()

        for _ in range(5):
            await bus.publish(Event(topic="L3.attention.shift", source="test", payload={}))
            await asyncio.sleep(0.01)
            await bus.publish(Event(topic="L8.drive.suggestion", source="test", payload={}))
            await asyncio.sleep(0.01)

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
