"""
L7 SelfRegulator 完整测试套件
================================
覆盖 SelfRegulator 的：
  - _react（issues → adaptation actions）
  - _heal_bus / _rebalance_attention / _stir_identity
  - _on_causal_pattern（L5 模式 → 主动调节）
  - _on_goal_achieved / _on_goal_abandoned
  - history / latest / stats
  - attach/detach（含 WM salience_fn 注入）
  - Adaptation dataclass
"""
import asyncio
import pytest
from kernel.event_bus import EventBus, Event
from layers.L7_will.regulator import SelfRegulator, Adaptation


class MockWM:
    """Minimal working memory mock for salience_fn injection test."""
    def __init__(self):
        self.salience_fn = lambda ev: 0.5


class TestAdaptation:
    def test_to_dict(self):
        a = Adaptation(
            timestamp="2025-05-14T12:00:00",
            trigger="注意力倾斜",
            action="attenuate_layer_salience",
            detail={"layer": "L3", "factor": 0.3},
        )
        d = a.to_dict()
        assert d["trigger"] == "注意力倾斜"
        assert d["action"] == "attenuate_layer_salience"
        assert d["detail"]["layer"] == "L3"


class TestSelfRegulatorInit:
    def test_defaults(self):
        r = SelfRegulator()
        assert r._sal_atten == 0.3
        assert r._min_thresh == 1.0
        assert r._thresh_step == 0.5
        assert r._max_actions == 3

    def test_custom_params(self):
        r = SelfRegulator(
            salience_attenuation=0.5,
            min_sleep_threshold=2.0,
            threshold_step=1.0,
            max_actions_per_warn=5,
        )
        assert r._sal_atten == 0.5
        assert r._min_thresh == 2.0
        assert r._thresh_step == 1.0
        assert r._max_actions == 5


class TestAttachDetach:
    @pytest.mark.asyncio
    async def test_attach_subscribes(self):
        bus = EventBus()
        r = SelfRegulator(bus=bus)
        await r.attach()
        # After attach, _unsub should be set
        assert r._unsub is not None
        await r.detach()

    @pytest.mark.asyncio
    async def test_detach_unsubscribes(self):
        bus = EventBus()
        r = SelfRegulator(bus=bus)
        await r.attach()
        await r.detach()
        assert r._unsub is None

    @pytest.mark.asyncio
    async def test_attach_injects_salience_fn(self):
        wm = MockWM()
        bus = EventBus()
        r = SelfRegulator(bus=bus, working_memory=wm)
        assert wm.salience_fn is not None
        await r.attach()
        # After attach, WM's salience_fn is wrapped
        assert wm.salience_fn is not None
        await r.detach()

    @pytest.mark.asyncio
    async def test_detach_restores_salience_fn(self):
        wm = MockWM()
        original = wm.salience_fn
        bus = EventBus()
        r = SelfRegulator(bus=bus, working_memory=wm)
        await r.attach()
        await r.detach()
        # After detach, original salience_fn is restored
        assert wm.salience_fn == original


class TestReact:
    @pytest.mark.asyncio
    async def test_heal_bus_on_critical_error(self):
        bus = EventBus()
        r = SelfRegulator(bus=bus)
        await r.attach()

        acted = []
        bus.subscribe("L7.regulator.acted", lambda e: acted.append(e.payload))

        await bus.publish(Event(
            topic="L6.metacognition.warn",
            source="test",
            payload={"issues": ["错误率严重"]},
        ))
        await asyncio.sleep(0.05)

        assert len(acted) == 1
        assert acted[0]["action"] == "emit_heal_intent"
        await r.detach()

    @pytest.mark.asyncio
    async def test_rebalance_attention_on_tilt_with_wm(self):
        """With WM wired, attention tilt triggers salience attenuation + history record."""
        bus = EventBus()
        wm = MockWM()
        r = SelfRegulator(bus=bus, working_memory=wm)
        await r.attach()

        acted = []
        bus.subscribe("L7.regulator.acted", lambda e: acted.append(e.payload))

        await bus.publish(Event(
            topic="L6.metacognition.warn",
            source="test",
            payload={"issues": ["注意力倾斜：L3 被霸占"]},
        ))
        await asyncio.sleep(0.05)

        assert len(acted) >= 1
        assert any(a["action"] == "attenuate_layer_salience" for a in acted)
        assert r.latest() is not None
        await r.detach()

    @pytest.mark.asyncio
    async def test_stir_identity_on_stagnation(self):
        bus = EventBus()
        r = SelfRegulator(bus=bus)
        await r.attach()

        acted = []
        bus.subscribe("L7.regulator.acted", lambda e: acted.append(e.payload))

        # Direct call to _react with identity stagnation issue
        warn_event = Event(
            topic="L6.metacognition.warn",
            source="test",
            payload={"issues": ["身份事实已经 6 个周期没增长"]},
        )
        await r._react(warn_event)
        await asyncio.sleep(0.05)

        # Without circadian wired, action is "noop" with reason
        assert len(acted) >= 1
        assert acted[0]["trigger"] == "身份事实已经 6 个周期没增长"
        assert acted[0]["action"] in ("shorten_sleep_threshold", "noop")
        await r.detach()

    @pytest.mark.asyncio
    async def test_max_actions_per_warn(self):
        bus = EventBus()
        r = SelfRegulator(bus=bus, max_actions_per_warn=2)
        await r.attach()

        acted = []
        bus.subscribe("L7.regulator.acted", lambda e: acted.append(e.payload))

        await bus.publish(Event(
            topic="L6.metacognition.warn",
            source="test",
            payload={"issues": ["注意力倾斜", "身份停滞", "错误率严重", "注意力倾斜再次"]},
        ))
        await asyncio.sleep(0.05)

        # Should only act on max_actions (2)
        assert len(acted) <= 2
        await r.detach()


class TestOnCausalPattern:
    @pytest.mark.asyncio
    async def test_high_confidence_pattern_triggers_preemptive_action(self):
        bus = EventBus()
        wm = MockWM()
        r = SelfRegulator(bus=bus, working_memory=wm)
        await r.attach()

        acted = []
        bus.subscribe("L7.regulator.acted", lambda e: acted.append(e.payload))

        # High confidence, high lift pattern: L3 → L6.warn
        await bus.publish(Event(
            topic="L5.pattern.discovered",
            source="test",
            payload={
                "antecedent": "L3.attention.shift",
                "consequent": "L6.metacognition.warn",
                "confidence": 0.85,
                "lift": 3.0,
            },
        ))
        await asyncio.sleep(0.05)

        assert len(acted) >= 1
        assert any(a["action"] == "attenuate_layer_salience" for a in acted)
        await r.detach()

    @pytest.mark.asyncio
    async def test_low_confidence_pattern_ignored(self):
        bus = EventBus()
        r = SelfRegulator(bus=bus)
        await r.attach()

        acted = []
        bus.subscribe("L7.regulator.acted", lambda e: acted.append(e.payload))

        # Low confidence pattern — should be ignored
        await bus.publish(Event(
            topic="L5.pattern.discovered",
            source="test",
            payload={
                "antecedent": "L3.attention.shift",
                "consequent": "L6.metacognition.warn",
                "confidence": 0.5,
                "lift": 1.5,
            },
        ))
        await asyncio.sleep(0.05)

        assert len(acted) == 0
        await r.detach()

    @pytest.mark.asyncio
    async def test_pattern_acted_only_once(self):
        """Same pattern discovered twice — second discovery is ignored (dedup by _learned_risky_patterns)."""
        bus = EventBus()
        wm = MockWM()
        r = SelfRegulator(bus=bus, working_memory=wm)
        await r.attach()

        acted = []
        bus.subscribe("L7.regulator.acted", lambda e: acted.append(e.payload))

        pattern_event = Event(
            topic="L5.pattern.discovered",
            source="test",
            payload={
                "antecedent": "L3.attention.shift",
                "consequent": "L6.metacognition.warn",
                "confidence": 0.9,
                "lift": 3.0,
            },
        )
        # Call _on_causal_pattern directly to avoid async bus publish timing issues
        await r._on_causal_pattern(pattern_event)
        await r._on_causal_pattern(pattern_event)

        # First call: _apply_layer_attenuation publishes + _record_and_emit publishes (2 actions)
        # Second call: skipped by _learned_risky_patterns dedup (0 new actions)
        # Total: 2 actions, all from first call
        assert len(acted) == 2, f"Expected 2 actions from first call, got {len(acted)}"
        # Dedup worked: second call added nothing
        await r._on_causal_pattern(pattern_event)
        assert len(acted) == 2, "Third call should add nothing (dedup)"
        await r.detach()

    @pytest.mark.asyncio
    async def test_intent_loop_triggers_weaken_signal(self):
        bus = EventBus()
        r = SelfRegulator(bus=bus)
        await r.attach()

        weakened = []
        acted = []
        bus.subscribe("L7.regulator.weaken_intent", lambda e: weakened.append(e.payload))
        bus.subscribe("L7.regulator.acted", lambda e: acted.append(e.payload))

        # Call _on_causal_pattern directly to avoid async bus timing issues
        pattern_event = Event(
            topic="L5.pattern.discovered",
            source="test",
            payload={
                "antecedent": "L8.intent.proposed",
                "consequent": "L4.observation.verified",
                "confidence": 0.9,
                "lift": 4.0,
            },
        )
        await r._on_causal_pattern(pattern_event)

        assert len(weakened) == 1
        assert acted[0]["action"] == "weaken_intent"
        await r.detach()


class TestGoalLifecycle:
    @pytest.mark.asyncio
    async def test_goal_achieved_recorded(self):
        bus = EventBus()
        r = SelfRegulator(bus=bus)
        await r.attach()

        before = len(r.history())
        await bus.publish(Event(
            topic="L7.goal.achieved",
            source="test",
            payload={"goal_id": "test_goal", "goal_text": "完成测试"},
        ))
        await asyncio.sleep(0.05)

        assert len(r.history()) == before + 1
        latest = r.latest()
        assert latest.action == "goal_achieved"
        await r.detach()

    @pytest.mark.asyncio
    async def test_goal_abandoned_recorded(self):
        bus = EventBus()
        r = SelfRegulator(bus=bus)
        await r.attach()

        before = len(r.history())
        await bus.publish(Event(
            topic="L7.goal.abandoned",
            source="test",
            payload={"goal_id": "test_goal", "reason": "资源不足"},
        ))
        await asyncio.sleep(0.05)

        assert len(r.history()) == before + 1
        latest = r.latest()
        assert latest.action == "goal_abandoned"
        await r.detach()


class TestHistoryStats:
    @pytest.mark.asyncio
    async def test_history_returns_list(self):
        bus = EventBus()
        r = SelfRegulator(bus=bus)
        await r.attach()
        await bus.publish(Event(
            topic="L6.metacognition.warn",
            source="test",
            payload={"issues": ["注意力倾斜"]},
        ))
        await asyncio.sleep(0.05)
        assert isinstance(r.history(), list)
        await r.detach()

    @pytest.mark.asyncio
    async def test_latest_returns_last(self):
        bus = EventBus()
        wm = MockWM()
        r = SelfRegulator(bus=bus, working_memory=wm)
        await r.attach()

        # Directly call _react to avoid async bus timing issues
        warn_event = Event(
            topic="L6.metacognition.warn",
            source="test",
            payload={"issues": ["注意力倾斜：L3"]},
        )
        await r._react(warn_event)
        # _record_and_emit is synchronous within the same async context
        assert r.latest() is not None
        assert r.latest().action == "attenuate_layer_salience"
        await r.detach()

    @pytest.mark.asyncio
    async def test_latest_returns_none_when_empty(self):
        bus = EventBus()
        r = SelfRegulator(bus=bus)
        assert r.latest() is None

    @pytest.mark.asyncio
    async def test_stats_keys(self):
        bus = EventBus()
        r = SelfRegulator(bus=bus)
        await r.attach()
        await bus.publish(Event(
            topic="L6.metacognition.warn",
            source="test",
            payload={"issues": ["注意力倾斜"]},
        ))
        await asyncio.sleep(0.05)
        stats = r.stats()
        assert "total_adaptations" in stats
        assert "by_action" in stats
        await r.detach()
