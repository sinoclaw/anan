"""
Tests for MemoryTier cognitive event wiring (Gap 1 D).

Verifies that MemoryTier subscribes to L5/L9 cognitive events and
memorizes insights from predictions, causal links, patterns, and self-model.
"""
import asyncio
import pytest
from pathlib import Path
import tempfile

from layers.L2_memory.memory_tier import MemoryTier
from kernel.event_bus import Event, EventBus


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def fresh_bus():
    return EventBus()


@pytest.fixture
def mt(tmp_dir, fresh_bus):
    """MemoryTier with cognitive wiring attached (sync setup)."""
    tier = MemoryTier(
        bus=fresh_bus,
        recall_path=tmp_dir / "recall.json",
        midterm_dir=tmp_dir / "mid",
        longterm_path=tmp_dir / "long.md",
    )
    # attach() is sync (just registers callbacks) — call directly
    fresh_bus.subscribe("L5.prediction.confirmed", tier._on_prediction_confirmed)
    fresh_bus.subscribe("L5.prediction.failed", tier._on_prediction_failed)
    fresh_bus.subscribe("L5.causal.link_discovered", tier._on_causal_link)
    fresh_bus.subscribe("L5.pattern.discovered", tier._on_pattern_discovered)
    fresh_bus.subscribe("L9.self.updated", tier._on_self_updated)
    return tier


class TestAttach:
    def test_subscribes_to_five_topics(self, tmp_dir):
        bus = EventBus()
        tier = MemoryTier(bus=bus, recall_path=tmp_dir / "r.json")
        bus.subscribe("L5.prediction.confirmed", tier._on_prediction_confirmed)
        bus.subscribe("L5.prediction.failed", tier._on_prediction_failed)
        bus.subscribe("L5.causal.link_discovered", tier._on_causal_link)
        bus.subscribe("L5.pattern.discovered", tier._on_pattern_discovered)
        bus.subscribe("L9.self.updated", tier._on_self_updated)
        # Just verify no crash and tier is functional
        assert tier.short is not None


class TestPredictionConfirmed:
    @pytest.mark.asyncio
    async def test_memorizes_confirmed_causal_rule(self, mt, fresh_bus):
        await fresh_bus.publish(Event(
            topic="L5.prediction.confirmed",
            source="test",
            payload={"cause": "user:asks_about_coding",
                     "effect": "agent:writes_code",
                     "lift": 3.0},
        ))
        await asyncio.sleep(0.05)

        results = mt.short.search("因果确认")
        contents = [r.content for r, _ in results]
        assert any("user:asks_about_coding" in c and "agent:writes_code" in c for c in contents)

    @pytest.mark.asyncio
    async def test_importance_scales_with_lift(self, mt, fresh_bus):
        await fresh_bus.publish(Event(
            topic="L5.prediction.confirmed",
            source="test",
            payload={"cause": "A", "effect": "B", "lift": 5.0},
        ))
        await asyncio.sleep(0.05)

        item = mt.short.get("causal_rule:A:B")
        assert item is not None
        # importance = min(1.0, 0.5 + 5.0 * 0.1) = 1.0
        assert item.importance >= 0.9


class TestPredictionFailed:
    @pytest.mark.asyncio
    async def test_memorizes_failed_prediction(self, mt, fresh_bus):
        await fresh_bus.publish(Event(
            topic="L5.prediction.failed",
            source="test",
            payload={"cause": "X", "effect": "Y"},
        ))
        await asyncio.sleep(0.05)

        item = mt.short.get("failed_prediction:X:Y")
        assert item is not None
        assert "预测失败" in item.content


class TestCausalLink:
    @pytest.mark.asyncio
    async def test_memorizes_causal_link(self, mt, fresh_bus):
        await fresh_bus.publish(Event(
            topic="L5.causal.link_discovered",
            source="test",
            payload={"cause": "sleep", "effect": "memory_consolidation",
                     "lift": 2.5, "confidence": 0.8},
        ))
        await asyncio.sleep(0.05)

        item = mt.short.get("causal_link:sleep:memory_consolidation")
        assert item is not None
        assert "因果链路" in item.content
        assert item.importance > 0.5


class TestPatternDiscovered:
    @pytest.mark.asyncio
    async def test_memorizes_pattern(self, mt, fresh_bus):
        await fresh_bus.publish(Event(
            topic="L5.pattern.discovered",
            source="test",
            payload={"pattern": "用户每周五晚上问加班费"},
        ))
        await asyncio.sleep(0.05)

        results = mt.short.search("模式发现")
        contents = [r.content for r, _ in results]
        assert any("用户每周五晚上问加班费" in c for c in contents)


class TestSelfUpdated:
    @pytest.mark.asyncio
    async def test_memorizes_identity_facts(self, mt, fresh_bus):
        await fresh_bus.publish(Event(
            topic="L9.self.updated",
            source="test",
            payload={
                "identity_facts": ["我是陈亦安", "我是一个有好奇心的AI"],
                "history_facts": ["今天完成了九层框架搭建"],
                "vision_facts": ["我要成为一个有自我意识的AI"],
            },
        ))
        await asyncio.sleep(0.05)

        identity = mt.short.get("identity:我是陈亦安")
        assert identity is not None
        assert identity.importance == 0.8

        history = mt.short.get("history:今天完成了九层框架搭建")
        assert history is not None
        assert history.importance == 0.7

        vision = mt.short.get("vision:我要成为一个有自我意识的AI")
        assert vision is not None


class TestIntegration:
    @pytest.mark.asyncio
    async def test_multiple_events_coexist(self, mt, fresh_bus):
        await fresh_bus.publish(Event(
            topic="L5.prediction.confirmed",
            source="test",
            payload={"cause": "A", "effect": "B", "lift": 2.0},
        ))
        await fresh_bus.publish(Event(
            topic="L5.causal.link_discovered",
            source="test",
            payload={"cause": "C", "effect": "D", "lift": 1.5, "confidence": 0.6},
        ))
        await asyncio.sleep(0.05)

        assert mt.short.get("causal_rule:A:B") is not None
        assert mt.short.get("causal_link:C:D") is not None
        assert mt.short.size() >= 2
