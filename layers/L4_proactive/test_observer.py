"""Tests for L4 ProactiveObserver."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from kernel.event_bus import Event, EventBus
from layers.L3_working_memory import WorkingMemory
from layers.L4_proactive import (
    ProactiveObserver,
    ProbeContext,
    ProbeResult,
    probe_grow_identity,
    probe_heal_bus,
    probe_keep_attention_balanced,
)
from layers.L8_intent import IntentStack


@pytest.fixture
def fresh_bus():
    return EventBus()


@dataclass
class FakeSelfModel:
    identity_facts: list = None
    def __post_init__(self):
        if self.identity_facts is None:
            self.identity_facts = []


# ---------------------------------------------------------------------------
# Built-in probes
# ---------------------------------------------------------------------------

class TestProbeKeepAttentionBalanced:
    @pytest.mark.asyncio
    async def test_no_wm_inconclusive(self, fresh_bus):
        ctx = ProbeContext(bus=fresh_bus)
        intent = type("I", (), {"key": "k", "detail": {}})()
        result = probe_keep_attention_balanced(intent, ctx)
        assert result.verdict == "inconclusive"

    @pytest.mark.asyncio
    async def test_balanced_wm_verified(self, fresh_bus):
        wm = WorkingMemory(capacity=20)
        await wm.attach(fresh_bus)
        # Mix events from many layers — none dominates
        for layer in ["L0", "L1", "L2", "L3", "L6", "L7", "L8", "L9"]:
            await fresh_bus.publish(Event(
                topic=f"{layer}.something", source="t", payload={},
            ))
        await asyncio.sleep(0.02)
        ctx = ProbeContext(bus=fresh_bus, working_memory=wm)
        intent = type("I", (), {"key": "k", "detail": {}})()
        result = probe_keep_attention_balanced(intent, ctx)
        assert result.verdict == "verified"
        await wm.detach()

    @pytest.mark.asyncio
    async def test_skewed_wm_falsified(self, fresh_bus):
        wm = WorkingMemory(capacity=20)
        await wm.attach(fresh_bus)
        # Flood L9 events
        for _ in range(15):
            await fresh_bus.publish(Event(
                topic="L9.self.updated", source="t", payload={},
            ))
        await fresh_bus.publish(Event(
            topic="L0.tick", source="t", payload={},
        ))
        await asyncio.sleep(0.02)
        ctx = ProbeContext(bus=fresh_bus, working_memory=wm)
        intent = type("I", (), {"key": "k", "detail": {}})()
        result = probe_keep_attention_balanced(intent, ctx)
        assert result.verdict == "falsified"
        assert result.detail["top_layer"] == "L9"
        await wm.detach()


class TestProbeGrowIdentity:
    @pytest.mark.asyncio
    async def test_first_call_baselines(self, fresh_bus):
        sm = FakeSelfModel(identity_facts=["a"])
        ctx = ProbeContext(bus=fresh_bus, self_model=sm)
        intent = type("I", (), {"key": "grow_identity", "detail": {}})()
        result = probe_grow_identity(intent, ctx)
        assert result.verdict == "inconclusive"
        assert intent.detail["_l4_last_identity_count"] == 1

    @pytest.mark.asyncio
    async def test_growth_verified(self, fresh_bus):
        sm = FakeSelfModel(identity_facts=["a"])
        ctx = ProbeContext(bus=fresh_bus, self_model=sm)
        intent = type("I", (), {"key": "grow_identity",
                                "detail": {"_l4_last_identity_count": 1}})()
        sm.identity_facts.append("b")
        result = probe_grow_identity(intent, ctx)
        assert result.verdict == "verified"
        assert result.detail["delta"] == 1

    @pytest.mark.asyncio
    async def test_stagnation_falsified(self, fresh_bus):
        sm = FakeSelfModel(identity_facts=["a"])
        ctx = ProbeContext(bus=fresh_bus, self_model=sm)
        intent = type("I", (), {"key": "grow_identity",
                                "detail": {"_l4_last_identity_count": 1}})()
        result = probe_grow_identity(intent, ctx)
        assert result.verdict == "falsified"


class TestProbeHealBus:
    @pytest.mark.asyncio
    async def test_no_errors_verified(self, fresh_bus):
        await fresh_bus.publish(Event(topic="L0.tick", source="t", payload={}))
        ctx = ProbeContext(bus=fresh_bus)
        intent = type("I", (), {"key": "heal_bus", "detail": {}})()
        result = probe_heal_bus(intent, ctx)
        assert result.verdict == "verified"

    @pytest.mark.asyncio
    async def test_errors_falsified(self, fresh_bus):
        await fresh_bus.publish(Event(topic="L0.error", source="t", payload={}))
        ctx = ProbeContext(bus=fresh_bus)
        intent = type("I", (), {"key": "heal_bus", "detail": {}})()
        result = probe_heal_bus(intent, ctx)
        assert result.verdict == "falsified"


# ---------------------------------------------------------------------------
# Observer end-to-end
# ---------------------------------------------------------------------------

class TestObserverWiring:
    @pytest.mark.asyncio
    async def test_snapshot_triggers_probes(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.5)
        wm = WorkingMemory(capacity=20)
        await wm.attach(fresh_bus)
        # Make WM balanced so probe verifies
        for layer in ["L0", "L1", "L2", "L3", "L6", "L7", "L8", "L9"]:
            await fresh_bus.publish(Event(
                topic=f"{layer}.x", source="t", payload={},
            ))
        await asyncio.sleep(0.02)

        await l8.propose("keep_attention_balanced", "保持注意力均衡")

        l4 = ProactiveObserver(
            bus=fresh_bus, intent_stack=l8, working_memory=wm,
        )
        await l4.attach()
        await l8.snapshot()
        await asyncio.sleep(0.02)

        obs = l4.observations()
        assert len(obs) == 1
        assert obs[0]["intent_key"] == "keep_attention_balanced"
        assert obs[0]["verdict"] == "verified"
        await l4.detach()
        await wm.detach()

    @pytest.mark.asyncio
    async def test_verified_satisfies_intent(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.1, abandon_floor=0.05)
        wm = WorkingMemory(capacity=20)
        await wm.attach(fresh_bus)
        for layer in ["L0", "L1", "L2", "L3", "L6", "L7", "L8", "L9"]:
            await fresh_bus.publish(Event(
                topic=f"{layer}.x", source="t", payload={},
            ))
        await asyncio.sleep(0.02)

        await l8.propose("keep_attention_balanced", "保持注意力均衡")
        # strength = 0.1, satisfy mults by 0.4 → 0.04 < 0.05 → abandoned
        l4 = ProactiveObserver(
            bus=fresh_bus, intent_stack=l8, working_memory=wm,
            auto_satisfy=True,
        )
        await l4.observe_now()
        # Intent should be satisfied (strength dropped)
        assert l8.get("keep_attention_balanced") is None
        assert any(i.key == "keep_attention_balanced" for i in l8.history())
        await wm.detach()

    @pytest.mark.asyncio
    async def test_falsified_reinforces_intent(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.4, reinforce_alpha=0.5)
        wm = WorkingMemory(capacity=20)
        await wm.attach(fresh_bus)
        # Flood L9 — skew
        for _ in range(15):
            await fresh_bus.publish(Event(
                topic="L9.self.updated", source="t", payload={},
            ))
        await fresh_bus.publish(Event(topic="L0.tick", source="t", payload={}))
        await asyncio.sleep(0.02)

        await l8.propose("keep_attention_balanced", "保持注意力均衡")
        before = l8.get("keep_attention_balanced").strength

        l4 = ProactiveObserver(
            bus=fresh_bus, intent_stack=l8, working_memory=wm,
            reinforce_on_falsify=True,
        )
        await l4.observe_now()
        after = l8.get("keep_attention_balanced").strength
        assert after > before  # reinforced
        await wm.detach()

    @pytest.mark.asyncio
    async def test_no_probe_for_intent_skips(self, fresh_bus):
        """Without LLM probe, catchall handles unmatched intents heuristically."""
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.5)
        await l8.propose("custom_thing", "do something custom")
        l4 = ProactiveObserver(bus=fresh_bus, intent_stack=l8)
        results = await l4.observe_now()
        # Catch-all probe runs for unmatched intents — returns inconclusive
        assert len(results) == 1
        assert results[0]["intent_key"] == "custom_thing"
        assert results[0]["verdict"] == "inconclusive"

    @pytest.mark.asyncio
    async def test_custom_probe_registration(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.5)
        await l8.propose("my_thing", "x")

        def my_probe(intent, ctx):
            return ProbeResult("verified", "always true")

        l4 = ProactiveObserver(
            bus=fresh_bus, intent_stack=l8, auto_satisfy=False,
        )
        l4.register_probe("my_thing", my_probe)
        results = await l4.observe_now()
        assert len(results) == 1
        assert results[0]["verdict"] == "verified"

    @pytest.mark.asyncio
    async def test_probe_exception_isolated(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.5)
        await l8.propose("boom", "x")

        def bad_probe(intent, ctx):
            raise RuntimeError("kaboom")

        l4 = ProactiveObserver(bus=fresh_bus, intent_stack=l8)
        l4.register_probe("boom", bad_probe)
        results = await l4.observe_now()
        # Doesn't crash; result is inconclusive
        assert len(results) == 1
        assert results[0]["verdict"] == "inconclusive"
        assert "kaboom" in results[0]["evidence"]

    @pytest.mark.asyncio
    async def test_emits_observation_event(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.5)
        wm = WorkingMemory(capacity=20)
        await wm.attach(fresh_bus)
        for layer in ["L0", "L1", "L2", "L3", "L6", "L7", "L8", "L9"]:
            await fresh_bus.publish(Event(
                topic=f"{layer}.x", source="t", payload={},
            ))
        await asyncio.sleep(0.02)
        await l8.propose("keep_attention_balanced", "保持注意力均衡")

        events = []
        fresh_bus.subscribe("L4.observation.verified", lambda e: events.append(e))
        l4 = ProactiveObserver(
            bus=fresh_bus, intent_stack=l8, working_memory=wm,
        )
        await l4.observe_now()
        await asyncio.sleep(0.02)
        assert len(events) == 1
        assert events[0].payload["intent_key"] == "keep_attention_balanced"
        await wm.detach()


class TestStats:
    @pytest.mark.asyncio
    async def test_stats_reports_verdicts(self, fresh_bus):
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.5)
        await l8.propose("a", "x")

        def yes(intent, ctx):
            return ProbeResult("verified", "ok")

        l4 = ProactiveObserver(
            bus=fresh_bus, intent_stack=l8, auto_satisfy=False,
        )
        l4.register_probe("a", yes)
        await l4.observe_now()
        await l4.observe_now()
        s = l4.stats()
        assert s["total_observations"] == 2


# ---------------------------------------------------------------------------
# ObservabilityAdvisor (OBS-1)
# ---------------------------------------------------------------------------

from layers.L4_proactive.observability_advisor import (
    ObservabilityAdvisor,
    fallback_observe,
    OBSResult,
)


class TestFallbackObserve:
    """Rule-based fallback mirrors existing probe_catchall logic."""

    @pytest.mark.asyncio
    async def test_keep_triggering_seen(self, fresh_bus):
        await fresh_bus.publish(Event(
            topic="L7.goal.achieved", source="t", payload={},
        ))
        intent_key = "keep_triggering_L7_goal"
        ctx = ProbeContext(bus=fresh_bus)
        result = fallback_observe(intent_key, "持续触发 L7 目标", ctx)
        assert result.verdict == "verified"

    def test_keep_triggering_not_seen(self, fresh_bus):
        intent_key = "keep_triggering_L7_goal"
        ctx = ProbeContext(bus=fresh_bus)
        result = fallback_observe(intent_key, "持续触发 L7 目标", ctx)
        assert result.verdict == "inconclusive"

    def test_avoid_action_not_seen(self, fresh_bus):
        intent_key = "avoid_L9_self"
        ctx = ProbeContext(bus=fresh_bus)
        result = fallback_observe(intent_key, "避免 L9 更新", ctx)
        assert result.verdict == "verified"

    @pytest.mark.asyncio
    async def test_avoid_action_seen(self, fresh_bus):
        await fresh_bus.publish(Event(
            topic="L9.self.updated", source="t", payload={},
        ))
        intent_key = "avoid_L9_self"
        ctx = ProbeContext(bus=fresh_bus)
        result = fallback_observe(intent_key, "避免 L9 更新", ctx)
        assert result.verdict == "falsified"

    @pytest.mark.asyncio
    async def test_keep_doing_seen(self, fresh_bus):
        await fresh_bus.publish(Event(
            topic="L1.sleep.entered", source="t", payload={},
        ))
        intent_key = "keep_doing_L1_sleep"
        ctx = ProbeContext(bus=fresh_bus)
        result = fallback_observe(intent_key, "持续睡眠", ctx)
        assert result.verdict == "verified"

    def test_unknown_intent_inconclusive(self, fresh_bus):
        intent_key = "balance_attention"
        ctx = ProbeContext(bus=fresh_bus)
        result = fallback_observe(intent_key, "平衡注意力", ctx)
        assert result.verdict == "inconclusive"


class TestObservabilityAdvisorWiring:
    """Advisor is created automatically and wired to llm_probe_fn."""

    @pytest.mark.asyncio
    async def test_observer_creates_advisor_by_default(self, fresh_bus):
        """When no custom llm_probe_fn provided, advisor.evaluate is used."""
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.5)
        l4 = ProactiveObserver(bus=fresh_bus, intent_stack=l8)
        # advisor should be created and wired as llm_probe_fn
        assert l4._obs_advisor is not None
        assert l4._llm_probe_fn == l4._obs_advisor.evaluate

    @pytest.mark.asyncio
    async def test_custom_llm_probe_fn_not_overridden(self, fresh_bus):
        """When llm_probe_fn is provided explicitly, advisor is NOT used."""
        custom_fn = lambda k, d, c: ProbeResult("inconclusive", "custom")
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.5)
        l4 = ProactiveObserver(
            bus=fresh_bus, intent_stack=l8,
            llm_probe_fn=custom_fn,
        )
        assert l4._llm_probe_fn == custom_fn

    @pytest.mark.asyncio
    async def test_set_delegate_wires_advisor(self, fresh_bus):
        """set_delegate propagates to the internal advisor."""
        l8 = IntentStack(bus=fresh_bus, initial_strength=0.5)
        l4 = ProactiveObserver(bus=fresh_bus, intent_stack=l8)

        async def fake_delegate(goal, context, parent_agent):
            return '{"verdict": "verified", "evidence": "test ok", "detail": {}}'

        l4.set_delegate(fake_delegate)
        assert l4._obs_advisor._delegate_fn == fake_delegate


class TestObservabilityAdvisorNoDelegate:
    """When delegate is not set, advisor falls back to rule-based logic."""

    @pytest.mark.asyncio
    async def test_fallback_verified_keep_triggering(self, fresh_bus):
        await fresh_bus.publish(Event(
            topic="L6.metacognition.warn", source="t", payload={},
        ))
        ctx = ProbeContext(bus=fresh_bus)
        advisor = ObservabilityAdvisor()
        # No delegate set — uses fallback
        result = await advisor.evaluate(
            "keep_triggering_L6_metacognition",
            "持续触发 L6 元认知警告",
            ctx,
        )
        assert result.verdict == "verified"

    @pytest.mark.asyncio
    async def test_fallback_verified_avoid(self, fresh_bus):
        ctx = ProbeContext(bus=fresh_bus)
        advisor = ObservabilityAdvisor()
        result = await advisor.evaluate(
            "avoid_L9_self",
            "避免 L9 自我更新",
            ctx,
        )
        # No L9.self events in history → avoid verified
        assert result.verdict == "verified"


class TestObservabilityAdvisorWithDelegate:
    """When delegate is set, advisor calls subagent and parses response."""

    @pytest.mark.asyncio
    async def test_delegate_returns_json_verified(self, fresh_bus):
        ctx = ProbeContext(bus=fresh_bus)
        advisor = ObservabilityAdvisor()

        async def fake_delegate(goal, context, parent_agent):
            return '{"verdict": "verified", "evidence": "LLM 判断满足", "detail": {"reason": "事件充足"}}'

        advisor.set_delegate(fake_delegate)
        result = await advisor.evaluate(
            "keep_attention_balanced",
            "保持注意力均衡",
            ctx,
        )
        assert result.verdict == "verified"
        assert "LLM" in result.evidence

    @pytest.mark.asyncio
    async def test_delegate_returns_json_falsified(self, fresh_bus):
        ctx = ProbeContext(bus=fresh_bus)
        advisor = ObservabilityAdvisor()

        async def fake_delegate(goal, context, parent_agent):
            return '{"verdict": "falsified", "evidence": "未满足", "detail": {}}'

        advisor.set_delegate(fake_delegate)
        result = await advisor.evaluate(
            "balance_attention",
            "平衡注意力",
            ctx,
        )
        assert result.verdict == "falsified"

    @pytest.mark.asyncio
    async def test_delegate_returns_inconclusive(self, fresh_bus):
        ctx = ProbeContext(bus=fresh_bus)
        advisor = ObservabilityAdvisor()

        async def fake_delegate(goal, context, parent_agent):
            return '{"verdict": "inconclusive", "evidence": "数据不足", "detail": {}}'

        advisor.set_delegate(fake_delegate)
        result = await advisor.evaluate(
            "balance_attention",
            "平衡注意力",
            ctx,
        )
        assert result.verdict == "inconclusive"

    @pytest.mark.asyncio
    async def test_delegate_fails_falls_back_to_rules(self, fresh_bus):
        await fresh_bus.publish(Event(
            topic="L1.sleep.entered", source="t", payload={},
        ))
        ctx = ProbeContext(bus=fresh_bus)
        advisor = ObservabilityAdvisor()

        async def bad_delegate(goal, context, parent_agent):
            raise RuntimeError("LLM unavailable")

        advisor.set_delegate(bad_delegate)
        result = await advisor.evaluate(
            "keep_doing_L1_sleep",
            "持续睡眠",
            ctx,
        )
        # Falls back to rule-based: L1.sleep seen → verified
        assert result.verdict == "verified"


class TestOBSResultParsing:
    """Response parsing from different LLM output formats."""

    def test_parse_json_code_block(self):
        text = '```json\n{"verdict": "verified", "evidence": "证据充分", "detail": {"reason": "good"}}\n```'
        result = ObservabilityAdvisor._parse_response(text)
        assert result.verdict == "verified"
        assert result.evidence == "证据充分"
        assert result.detail["reason"] == "good"

    def test_parse_raw_json(self):
        text = '{"verdict": "falsified", "evidence": "未实现", "detail": {}}'
        result = ObservabilityAdvisor._parse_response(text)
        assert result.verdict == "falsified"
        assert result.evidence == "未实现"

    def test_parse_field_by_field(self):
        text = '{"verdict": "inconclusive", "evidence": "数据不足", "detail": {}}'
        result = ObservabilityAdvisor._parse_response(text)
        assert result.verdict == "inconclusive"

    def test_parse_invalid_json_defaults_inconclusive(self):
        text = "LLM 返回了一些无法解析的文本"
        result = ObservabilityAdvisor._parse_response(text)
        assert result.verdict == "inconclusive"

    def test_from_data_invalid_verdict_normalized(self):
        data = {"verdict": "maybe", "evidence": "test", "detail": {}}
        result = ObservabilityAdvisor._from_data(data)
        assert result.verdict == "inconclusive"
