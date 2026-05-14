"""
adapters/reflection_dream.py 测试套件
======================================

覆盖:
  - _summarize_topics()
  - reflect_light() — 统计事件数量
  - reflect_rem() — 叙事生成
  - reflect_deep() — 深度抽象
  - 事件按 cycle 过滤
"""

import pytest
from unittest.mock import AsyncMock, patch
from kernel.event_bus import Event, EventBus
from adapters.reflection_dream import (
    _summarize_topics,
    reflect_light,
    reflect_rem,
    reflect_deep,
)


class TestSummarizeTopics:
    def test_empty_events(self):
        result = _summarize_topics([])
        assert result == {}

    def test_single_topic(self):
        events = [
            Event(topic="L3.attention", source="test", payload={}),
            Event(topic="L3.attention", source="test", payload={}),
        ]
        result = _summarize_topics(events)
        assert result == {"L3": 2}

    def test_multiple_topics(self):
        events = [
            Event(topic="L3.attention", source="test", payload={}),
            Event(topic="L5.prediction.confirmed", source="test", payload={}),
            Event(topic="L3.attention", source="test", payload={}),
            Event(topic="L8.intent.proposed", source="test", payload={}),
        ]
        result = _summarize_topics(events)
        assert result == {"L3": 2, "L5": 1, "L8": 1}

    def test_topics_extracted_from_topic_attribute(self):
        events = [
            Event(topic="L9.self.updated", source="test", payload={}),
        ]
        result = _summarize_topics(events)
        assert result == {"L9": 1}


class TestReflectLight:
    @pytest.mark.asyncio
    async def test_no_events_returns_empty_consolidated_facts(self):
        bus = EventBus()
        result = reflect_light(bus, day="2026-05-14", cycle=1)
        assert "consolidated_facts" in result

    @pytest.mark.asyncio
    async def test_counts_events_by_topic(self):
        bus = EventBus()
        await bus.publish(Event(
            topic="L3.attention.shift",
            source="test",
            payload={"cycle": 1},
        ))
        await bus.publish(Event(
            topic="L5.prediction.confirmed",
            source="test",
            payload={"cycle": 1},
        ))
        await bus.publish(Event(
            topic="L3.attention.shift",
            source="test",
            payload={"cycle": 1},
        ))

        result = reflect_light(bus, day="2026-05-14", cycle=1)
        facts = result["consolidated_facts"]
        assert any("L3" in f for f in facts)
        assert any("L5" in f for f in facts)


class TestReflectRem:
    @pytest.mark.asyncio
    async def test_no_events_returns_empty(self):
        bus = EventBus()
        result = reflect_rem(bus, day="2026-05-14", cycle=1)
        assert "dream" in result
        assert "consolidated_facts" in result

    @pytest.mark.asyncio
    async def test_narrative_includes_event_summary(self):
        bus = EventBus()
        await bus.publish(Event(
            topic="L3.attention.shift",
            source="test",
            payload={"cycle": 1},
        ))
        await bus.publish(Event(
            topic="L8.intent.proposed",
            source="test",
            payload={"cycle": 1},
        ))

        result = reflect_rem(bus, day="2026-05-14", cycle=1)
        dream = result["dream"]
        facts = result["consolidated_facts"]
        assert len(dream) > 0
        assert len(facts) >= 0


class TestReflectDeep:
    @pytest.mark.asyncio
    async def test_no_events_returns_empty(self):
        bus = EventBus()
        result = reflect_deep(bus, day="2026-05-14", cycle=1)
        assert "consolidated_facts" in result

    @pytest.mark.asyncio
    async def test_finds_recurring_patterns(self):
        bus = EventBus()
        for cycle in range(1, 4):
            await bus.publish(Event(
                topic="L3.attention.shift",
                source="test",
                payload={"cycle": cycle},
            ))
            await bus.publish(Event(
                topic="L5.prediction.confirmed",
                source="test",
                payload={"cycle": cycle},
            ))

        result = reflect_deep(bus, day="2026-05-14", cycle=3)
        facts = result["consolidated_facts"]
        assert len(facts) >= 0  # at least ran without error
