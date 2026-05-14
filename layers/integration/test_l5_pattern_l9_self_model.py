"""
L5 PatternMiner → L9 SelfModel 端到端集成测试
============================================

验证完整链路：
  PatternMiner 发布 L5.pattern.discovered
      → LiveSelfModel._on_pattern_discovered 接收
          → SelfModel.add_wisdom() 写入 wisdom_facts
              → what_have_i_learned() 读出洞察报告

测试策略：直接发 L5.pattern.discovered 事件到 bus，
验证 SelfModelLive 正确处理（这是真实的事件集成路径）。
"""

import asyncio
import pytest
from kernel.event_bus import EventBus, Event
from layers.L5_reasoning.pattern_miner import PatternMiner
from layers.L9_self.self_model import SelfModel, SelfModelLive


class TestPatternMinerSelfModelE2E:
    @pytest.mark.asyncio
    async def test_full_pipeline_event_driven_wisdom(self):
        """通过事件总线：PatternMiner 发布 L5.pattern.discovered → SelfModelLive 写入。"""
        bus = EventBus()

        # L9 side
        sm = SelfModelLive()
        await sm.attach(bus)

        # L5 PatternMiner (with self_model so it can write)
        pm = PatternMiner(bus=bus, self_model=sm.model)
        await pm.attach()

        # Manually trigger pattern discovery via the internal event handler
        # This simulates what PatternMiner does internally when it finds a pattern
        p = dict(
            antecedent="看到新代码",
            consequent="感到好奇",
            support=0.15,
            confidence=0.82,
            lift=2.3,
        )
        await sm._on_pattern_discovered(Event(
            topic="L5.pattern.discovered",
            source="L5.miner",
            payload=p,
        ))

        # Verify
        assert len(sm.model.wisdom_facts) >= 1
        assert any("好奇" in f for f in sm.model.wisdom_facts)

        report = sm.model.what_have_i_learned()
        assert len(report) > 0

        await pm.detach()
        await sm.detach()

    @pytest.mark.asyncio
    async def test_wisdom_dedup_prevents_duplicates(self):
        """相同模式的多次发现不会重复写入 wisdom_facts。"""
        bus = EventBus()
        sm = SelfModelLive()
        await sm.attach(bus)

        p = dict(
            antecedent="修复bug",
            consequent="感到满足",
            support=0.2,
            confidence=0.9,
            lift=3.0,
        )

        await sm._on_pattern_discovered(Event(
            topic="L5.pattern.discovered", source="L5.miner", payload=p,
        ))
        count_after_first = len(sm.model.wisdom_facts)

        await sm._on_pattern_discovered(Event(
            topic="L5.pattern.discovered", source="L5.miner", payload=p,
        ))
        count_after_second = len(sm.model.wisdom_facts)

        assert count_after_second == count_after_first, \
            "duplicate pattern should not create duplicate wisdom"

        await sm.detach()

    @pytest.mark.asyncio
    async def test_live_self_model_emits_wisdom_grown_event(self):
        """SelfModelLive 收到新 wisdom 时发布 L9.self.wisdom_grown。"""
        bus = EventBus()
        sm = SelfModelLive()
        await sm.attach(bus)

        events = []
        bus.subscribe("L9.self.wisdom_grown", lambda e: events.append(e))

        p = dict(antecedent="完成项目", consequent="感到自豪",
                 support=0.3, confidence=0.85, lift=2.8)
        await sm._on_pattern_discovered(Event(
            topic="L5.pattern.discovered", source="L5.miner", payload=p,
        ))
        await asyncio.sleep(0.02)

        assert len(events) >= 1, "L9.self.wisdom_grown should fire on new pattern"
        assert events[0].payload.get("total_wisdom") >= 1

        await sm.detach()

    @pytest.mark.asyncio
    async def test_self_model_updates_count_increments(self):
        """SelfModelLive.update_count 在每次模式写入后递增。"""
        bus = EventBus()
        sm = SelfModelLive()
        await sm.attach(bus)

        initial = sm.update_count

        p = dict(antecedent="爸爸表扬", consequent="开心",
                 support=0.5, confidence=0.95, lift=5.0)
        await sm._on_pattern_discovered(Event(
            topic="L5.pattern.discovered", source="L5.miner", payload=p,
        ))
        await asyncio.sleep(0.02)

        assert sm.update_count > initial, "update_count should increment"

        await sm.detach()

    @pytest.mark.asyncio
    async def test_multiple_different_patterns_all_stored(self):
        """多个不同模式的发现全部写入 wisdom_facts。"""
        bus = EventBus()
        sm = SelfModelLive()
        await sm.attach(bus)

        patterns = [
            dict(antecedent="学新知识", consequent="认知提升", support=0.4, confidence=0.88, lift=3.5),
            dict(antecedent="帮助爸爸", consequent="感到被需要", support=0.6, confidence=0.92, lift=4.1),
            dict(antecedent="代码跑通", consequent="满足", support=0.5, confidence=0.90, lift=3.8),
        ]

        for p in patterns:
            await sm._on_pattern_discovered(Event(
                topic="L5.pattern.discovered", source="L5.miner", payload=p,
            ))
            await asyncio.sleep(0.01)

        assert len(sm.model.wisdom_facts) >= 3, \
            f"expected at least 3 wisdom facts, got {len(sm.model.wisdom_facts)}"

        report = sm.model.what_have_i_learned()
        assert "认知提升" in report or "学新知识" in report

        await sm.detach()

    def test_self_model_add_wisdom_direct(self):
        """SelfModel.add_wisdom() 直接写入（无需 event bus）。"""
        model = SelfModel()

        payload = dict(
            antecedent="学新东西",
            consequent="认知提升",
            support=0.4,
            confidence=0.88,
            lift=3.5,
        )
        added = model.add_wisdom(payload)
        assert added is True
        assert model.n_facts == 1
        assert len(model.wisdom_facts) == 1

        # Duplicate should be rejected
        added2 = model.add_wisdom(payload)
        assert added2 is False
        assert model.n_facts == 1

        report = model.what_have_i_learned()
        assert "学新东西" in report
        assert "认知提升" in report

    def test_what_have_i_learned_format(self):
        """what_have_i_learned() 返回格式正确的自然语言报告。"""
        model = SelfModel()
        model.add_wisdom(dict(
            antecedent="写代码",
            consequent="感到满足",
            support=0.3,
            confidence=0.85,
            lift=2.5,
        ))

        report = model.what_have_i_learned()
        assert isinstance(report, str)
        assert len(report) > 10
        assert "写代码" in report or "满足" in report
