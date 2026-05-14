"""
L5 CausalReasoner 测试套件
==============================
测试 CausalLink、ActionEffect 数据类，以及 CausalReasoner 的：
  - 初始化
  - 事件消费与滚动窗口
  - lift 计算（通过 _build_link）
  - 因果链路发现
  - L7.acted → L6.report 效果评估
  - attach/detach
"""
import asyncio
import pytest
from kernel.event_bus import EventBus, Event
from layers.L5_reasoning.causal import CausalLink, ActionEffect, CausalReasoner


# ---------------------------------------------------------------------------
# DataClass tests
# ---------------------------------------------------------------------------

class TestCausalLink:
    def test_lift_calculation(self):
        link = CausalLink(
            cause="L3.attention.shift",
            effect="L8.drive.suggestion",
            co_count=10,
            cause_count=20,
            effect_count=50,
            total_events=200,
            lift=2.0,
        )
        assert abs(link.lift - 2.0) < 0.001

    def test_lift_zero_baseline(self):
        link = CausalLink("A", "B", co_count=0, cause_count=10, effect_count=0, total_events=100, lift=0.0)
        assert link.lift == 0.0

    def test_confidence_property(self):
        link = CausalLink("A", "B", co_count=5, cause_count=10, effect_count=20, total_events=100, lift=1.0)
        assert link.confidence == 0.5

    def test_str_representation(self):
        link = CausalLink("A", "B", co_count=5, cause_count=10, effect_count=20, total_events=100, lift=1.5)
        s = str(link)
        assert "A → B" in s
        assert "lift=" in s
        assert "conf=" in s


class TestActionEffect:
    def test_init(self):
        ae = ActionEffect(action="attenuate_layer", samples=3, avg_delta=0.15)
        assert ae.action == "attenuate_layer"
        assert ae.samples == 3
        assert ae.avg_delta == 0.15
        assert ae.last_before == 0.0
        assert ae.last_after == 0.0

    def test_fields(self):
        ae = ActionEffect(action="boost", samples=1, avg_delta=-0.05, last_before=0.5, last_after=0.45)
        assert ae.last_before == 0.5
        assert ae.last_after == 0.45


# ---------------------------------------------------------------------------
# CausalReasoner unit tests
# ---------------------------------------------------------------------------

class TestCausalReasonerInit:
    def test_default_values(self):
        cr = CausalReasoner()
        assert cr._window_s == 2.0
        assert cr._min_obs == 3
        assert cr._lift_threshold == 1.5
        assert cr._total_events == 0
        assert "L9.self.wisdom_grown" in cr._ignore

    def test_custom_params(self):
        cr = CausalReasoner(window_s=5.0, min_observations=5, lift_threshold=2.0)
        assert cr._window_s == 5.0
        assert cr._min_obs == 5
        assert cr._lift_threshold == 2.0

    def test_bus_assignment(self):
        bus = EventBus()
        cr = CausalReasoner(bus=bus)
        assert cr._bus is bus

    def test_ignore_includes_wisdom_grown(self):
        cr = CausalReasoner()
        assert "L9.self.wisdom_grown" in cr._ignore


class TestLiftComputation:
    def test_build_link_lift_above_threshold(self):
        """_build_link returns CausalLink with correct lift."""
        cr = CausalReasoner(window_s=2.0, min_observations=3, lift_threshold=1.5)
        cr._total_events = 100
        cr._cause_count["L3.attention.shift"] = 20
        cr._effect_count["L8.drive.suggestion"] = 50
        cr._co["L3.attention.shift"]["L8.drive.suggestion"] = 20

        link = cr._build_link("L3.attention.shift", "L8.drive.suggestion")
        # P(effect|after cause) = 20/20 = 1.0
        # P(effect) = 50/100 = 0.5
        # lift = 1.0 / 0.5 = 2.0
        assert abs(link.lift - 2.0) < 0.001
        assert link.co_count == 20
        assert link.cause_count == 20
        assert link.effect_count == 50
        assert link.total_events == 100

    def test_build_link_lift_below_threshold(self):
        """No lift → co/cause ≈ effect/total."""
        cr = CausalReasoner()
        cr._total_events = 100
        cr._cause_count["X"] = 10
        cr._effect_count["Y"] = 10
        cr._co["X"]["Y"] = 1
        link = cr._build_link("X", "Y")
        # P(Y|after X) = 1/10 = 0.1, P(Y) = 10/100 = 0.1, lift = 1.0
        assert abs(link.lift - 1.0) < 0.001

    def test_build_link_unknown_cause_returns_zero_lift(self):
        cr = CausalReasoner()
        cr._total_events = 100
        cr._effect_count["Y"] = 10
        # No cause_count for UNKNOWN
        link = cr._build_link("UNKNOWN", "Y")
        # co=0, cause_n=0 → P(effect|after cause) = 0/1 = 0, lift = 0
        assert link.lift == 0.0

    def test_build_link_unknown_effect_returns_zero_lift(self):
        cr = CausalReasoner()
        cr._cause_count["X"] = 10
        # No effect_count for UNKNOWN
        link = cr._build_link("X", "UNKNOWN")
        assert link.lift == 0.0


class TestCausalReasonerAttach:
    @pytest.mark.asyncio
    async def test_attach_subscribes_to_bus(self):
        bus = EventBus()
        cr = CausalReasoner(bus=bus)
        await cr.attach()
        assert len(bus._subscribers) > 0
        await cr.detach()

    @pytest.mark.asyncio
    async def test_detach_clears_subscriptions(self):
        bus = EventBus()
        cr = CausalReasoner(bus=bus)
        await cr.attach()
        await cr.detach()
        # After detach, _unsubs list should be cleared
        assert len(cr._unsubs) == 0

    @pytest.mark.asyncio
    async def test_attach_idempotent(self):
        bus = EventBus()
        cr = CausalReasoner(bus=bus)
        await cr.attach()
        first_count = len(cr._unsubs)
        await cr.attach()  # second attach without detach
        assert len(cr._unsubs) == first_count  # no accumulation
        await cr.detach()

class TestEventProcessing:
    @pytest.mark.asyncio
    async def test_event_updates_recent_queue(self):
        bus = EventBus()
        cr = CausalReasoner(bus=bus, window_s=2.0, min_observations=3)
        await cr.attach()

        await bus.publish(Event(topic="L3.attention.shift", source="test", payload={}))
        await asyncio.sleep(0.01)

        assert len(cr._recent) == 1
        assert cr._recent[0][1] == "L3.attention.shift"

        await cr.detach()

    @pytest.mark.asyncio
    async def test_event_increments_total(self):
        bus = EventBus()
        cr = CausalReasoner(bus=bus, window_s=2.0, min_observations=3)
        await cr.attach()

        await bus.publish(Event(topic="L3.attention.shift", source="test", payload={}))
        await asyncio.sleep(0.01)
        assert cr._total_events == 1

        await bus.publish(Event(topic="L8.drive.suggestion", source="test", payload={}))
        await asyncio.sleep(0.01)
        assert cr._total_events == 2

        await cr.detach()

    @pytest.mark.asyncio
    async def test_event_within_window_triggers_cooccurrence(self):
        bus = EventBus()
        cr = CausalReasoner(bus=bus, window_s=2.0, min_observations=2)
        await cr.attach()

        await bus.publish(Event(topic="L3.attention.shift", source="test", payload={}))
        await asyncio.sleep(0.02)
        await bus.publish(Event(topic="L8.drive.suggestion", source="test", payload={}))

        await asyncio.sleep(0.05)

        assert cr._cause_count["L3.attention.shift"] == 1
        assert cr._effect_count["L8.drive.suggestion"] == 1

        await cr.detach()

    @pytest.mark.asyncio
    async def test_l5_events_ignored(self):
        """L5.* events should not be fed back into reasoning."""
        bus = EventBus()
        cr = CausalReasoner(bus=bus)
        await cr.attach()

        await bus.publish(Event(topic="L5.causal.link_discovered", source="test", payload={}))
        await asyncio.sleep(0.01)
        recent_topics = [t for _, t in cr._recent]
        assert "L5.causal.link_discovered" not in recent_topics

        await cr.detach()

    @pytest.mark.asyncio
    async def test_ignored_topics_skipped(self):
        """Topics in _ignore set should not be tracked."""
        bus = EventBus()
        cr = CausalReasoner(bus=bus, ignore_topics={"L0.circadian.tick"})
        await cr.attach()

        await bus.publish(Event(topic="L0.circadian.tick", source="test", payload={}))
        await asyncio.sleep(0.01)
        assert len(cr._recent) == 0

        await cr.detach()

    @pytest.mark.asyncio
    async def test_wisdom_grown_auto_ignored(self):
        """L9.self.wisdom_grown is always auto-ignored to prevent feedback loops."""
        bus = EventBus()
        cr = CausalReasoner(bus=bus)
        await cr.attach()

        await bus.publish(Event(topic="L9.self.wisdom_grown", source="test", payload={}))
        await asyncio.sleep(0.01)
        recent_topics = [t for _, t in cr._recent]
        assert "L9.self.wisdom_grown" not in recent_topics

        await cr.detach()


class TestCausalDiscovery:
    @pytest.mark.asyncio
    async def test_high_lift_triggers_link_discovered_event(self):
        """When lift > threshold, L5.causal.link_discovered should fire."""
        bus = EventBus()
        cr = CausalReasoner(
            bus=bus,
            window_s=2.0,
            min_observations=2,
            lift_threshold=1.5,
        )
        await cr.attach()

        fired = []
        bus.subscribe("L5.causal.link_discovered", lambda e: fired.append(e))

        # Establish baseline: B appears 50 times out of 100 events
        for _ in range(50):
            await bus.publish(Event(topic="L8.drive.suggestion", source="test", payload={}))
        # A appears 20 times, always followed by B within window
        for _ in range(20):
            await bus.publish(Event(topic="L3.attention.shift", source="test", payload={}))
            await asyncio.sleep(0.01)
            await bus.publish(Event(topic="L8.drive.suggestion", source="test", payload={}))

        await asyncio.sleep(0.1)

        assert len(fired) >= 1
        link_event = fired[0]
        assert "cause" in link_event.payload
        assert "effect" in link_event.payload
        assert "lift" in link_event.payload
        assert "confidence" in link_event.payload

        await cr.detach()


class TestActionEffectEvaluation:
    @pytest.mark.asyncio
    async def test_l7_acted_records_pending_action(self):
        bus = EventBus()
        cr = CausalReasoner(bus=bus)
        await cr.attach()

        await bus.publish(Event(
            topic="L7.regulator.acted",
            source="test",
            payload={"action": "boost_creativity"},
        ))
        await asyncio.sleep(0.01)

        assert len(cr._pending_actions) == 1
        assert cr._pending_actions[0][0] == "boost_creativity"

        await cr.detach()

    @pytest.mark.asyncio
    async def test_l6_report_evaluates_pending_action(self):
        bus = EventBus()
        cr = CausalReasoner(bus=bus, action_eval_lookahead=2)
        await cr.attach()

        # Establish baseline health reading first
        await bus.publish(Event(
            topic="L6.metacognition.report",
            source="test",
            payload={"score": 0.5},
        ))
        await asyncio.sleep(0.01)

        # Record an action
        await bus.publish(Event(
            topic="L7.regulator.acted",
            source="test",
            payload={"action": "test_action"},
        ))
        await asyncio.sleep(0.01)

        # Evaluate with a different health score
        await bus.publish(Event(
            topic="L6.metacognition.report",
            source="test",
            payload={"score": 0.8},
        ))
        await asyncio.sleep(0.05)

        # Action evaluated (removed from pending, recorded to _action_effects)
        assert len(cr._pending_actions) == 0
        assert "test_action" in cr._action_effects

        await cr.detach()

    @pytest.mark.asyncio
    async def test_action_effects_accumulates(self):
        bus = EventBus()
        cr = CausalReasoner(bus=bus)
        await cr.attach()

        # Establish baseline
        await bus.publish(Event(topic="L6.metacognition.report", source="test", payload={"score": 0.5}))
        await asyncio.sleep(0.01)

        # Two actions recorded and evaluated
        await bus.publish(Event(topic="L7.regulator.acted", source="test", payload={"action": "same"}))
        await bus.publish(Event(topic="L6.metacognition.report", source="test", payload={"score": 0.6}))
        await asyncio.sleep(0.01)
        await bus.publish(Event(topic="L7.regulator.acted", source="test", payload={"action": "same"}))
        await bus.publish(Event(topic="L6.metacognition.report", source="test", payload={"score": 0.2}))
        await asyncio.sleep(0.05)

        assert "same" in cr._action_effects
        ae = cr.action_effects()["same"]
        assert ae.samples >= 1

        await cr.detach()


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

class TestCausalChainIntegration:
    @pytest.mark.asyncio
    async def test_attention_shift_triggers_drive_suggestion_link(self):
        """Full chain: L3.attention.shift → L8.drive.suggestion with high lift."""
        bus = EventBus()
        cr = CausalReasoner(
            bus=bus,
            window_s=2.0,
            min_observations=3,
            lift_threshold=1.5,
        )
        await cr.attach()

        discovered_links = []
        bus.subscribe("L5.causal.link_discovered", lambda e: discovered_links.append(e.payload))

        # Establish baseline: B fires 60 times out of 120
        for _ in range(60):
            await bus.publish(Event(topic="L8.drive.suggestion", source="test", payload={}))
            await asyncio.sleep(0.001)
        # A fires 30 times, always followed by B within window
        for _ in range(30):
            await bus.publish(Event(topic="L3.attention.shift", source="test", payload={}))
            await asyncio.sleep(0.01)
            await bus.publish(Event(topic="L8.drive.suggestion", source="test", payload={}))
            await asyncio.sleep(0.001)

        await asyncio.sleep(0.2)

        causes = [d["cause"] for d in discovered_links]
        effects = [d["effect"] for d in discovered_links]
        assert "L3.attention.shift" in causes
        assert "L8.drive.suggestion" in effects

        await cr.detach()
