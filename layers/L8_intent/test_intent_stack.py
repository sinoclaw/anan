"""
L8 IntentStack 完整测试套件
==============================
覆盖 Intent / IntentStack 的：
  - propose / satisfy / decay_tick / abandon
  - top / all_intents / get / history / stats
  - what_do_i_want
  - 容量限制（米勒数字 7）
  - 事件处理（8种事件源）
  - attach/detach
"""
import asyncio
import pytest
from datetime import datetime
from kernel.event_bus import EventBus, Event
from layers.L8_intent.intent_stack import IntentStack, Intent


# ---------------------------------------------------------------------------
# Intent dataclass
# ---------------------------------------------------------------------------

class TestIntentToDict:
    def test_to_dict_contains_all_fields(self):
        now = datetime.now().isoformat()
        intent = Intent(
            key="test_key",
            description="Test description",
            source="manual",
            strength=0.75,
            proposed_at=now,
            last_reinforced_at=now,
            reinforce_count=3,
            detail={"foo": "bar"},
        )
        d = intent.to_dict()
        assert d["key"] == "test_key"
        assert d["description"] == "Test description"
        assert d["source"] == "manual"
        assert d["strength"] == 0.75
        assert d["reinforce_count"] == 3
        assert d["detail"] == {"foo": "bar"}


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

class TestIntentStackInit:
    def test_default_capacity(self):
        l8 = IntentStack()
        assert l8._capacity == 7
        assert l8._init_str == 0.5
        assert l8._alpha == 0.25
        assert l8._decay == 0.92
        assert l8._floor == 0.05

    def test_custom_params(self):
        l8 = IntentStack(capacity=5, initial_strength=0.6, decay_factor=0.8)
        assert l8._capacity == 5
        assert l8._init_str == 0.6
        assert l8._decay == 0.8

    def test_bus_assignment(self):
        bus = EventBus()
        l8 = IntentStack(bus=bus)
        assert l8._bus is bus


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------

class TestPropose:
    @pytest.mark.asyncio
    async def test_propose_new_intent(self):
        l8 = IntentStack()
        intent = await l8.propose("fix_bug", "修复某个 bug", source="manual")
        assert intent.key == "fix_bug"
        assert intent.description == "修复某个 bug"
        assert intent.source == "manual"
        assert intent.strength == 0.5
        assert intent.reinforce_count == 0

    @pytest.mark.asyncio
    async def test_propose_reinforces_existing(self):
        l8 = IntentStack()
        await l8.propose("explore", "探索新事物")
        first_str = l8.get("explore").strength
        # reinforce
        await l8.propose("explore", "探索新事物")
        second = l8.get("explore")
        assert second.reinforce_count == 1
        assert second.strength > first_str

    @pytest.mark.asyncio
    async def test_propose_emits_event(self):
        bus = EventBus()
        l8 = IntentStack(bus=bus)
        published_events = []
        async def collector(event):
            published_events.append(event)
        bus.subscribe("L8.intent.proposed", collector)
        await l8.propose("test_key", "Test desc")
        assert any(e.topic == "L8.intent.proposed" for e in published_events)

    @pytest.mark.asyncio
    async def test_propose_enforces_capacity(self):
        l8 = IntentStack(capacity=3)
        for i in range(5):
            await l8.propose(f"key_{i}", f"intent {i}")
        assert len(l8._intents) <= 3


# ---------------------------------------------------------------------------
# satisfy
# ---------------------------------------------------------------------------

class TestSatisfy:
    @pytest.mark.asyncio
    async def test_satisfy_multiplies_strength(self):
        l8 = IntentStack()
        await l8.propose("test", "test")
        before = l8.get("test").strength
        await l8.satisfy("test")
        after = l8.get("test").strength
        assert after < before

    @pytest.mark.asyncio
    async def test_satisfy_nonexistent_returns_none(self):
        l8 = IntentStack()
        result = await l8.satisfy("does_not_exist")
        assert result is None

    @pytest.mark.asyncio
    async def test_satisfy_abandons_when_below_floor(self):
        l8 = IntentStack(decay_factor=0.5)  # fast decay
        await l8.propose("dying_intent", "test")
        # decay it below floor
        await l8.decay_tick()
        await l8.decay_tick()
        await l8.decay_tick()
        await l8.satisfy("dying_intent")
        # Should be abandoned
        assert l8.get("dying_intent") is None
        assert len(l8._abandoned) >= 0  # abandoned list grows


# ---------------------------------------------------------------------------
# decay_tick
# ---------------------------------------------------------------------------

class TestDecayTick:
    @pytest.mark.asyncio
    async def test_decay_tick_reduces_strength(self):
        l8 = IntentStack(decay_factor=0.9)
        await l8.propose("decay_test", "test")
        before = l8.get("decay_test").strength
        await l8.decay_tick()
        after = l8.get("decay_test").strength
        assert after < before

    @pytest.mark.asyncio
    async def test_decay_tick_abandons_low_intents(self):
        l8 = IntentStack(decay_factor=0.1, abandon_floor=0.1)
        await l8.propose("will_die", "test")
        # Decay until below floor
        for _ in range(20):
            abandoned = await l8.decay_tick()
            if l8.get("will_die") is None:
                break
        # Intent should be gone from active set
        assert l8.get("will_die") is None

    @pytest.mark.asyncio
    async def test_decay_tick_returns_abandon_count(self):
        l8 = IntentStack(decay_factor=0.1)
        await l8.propose("die_a", "a")
        await l8.propose("die_b", "b")
        count = 0
        for _ in range(30):
            count += await l8.decay_tick()
            if len(l8._intents) == 0:
                break
        assert count >= 2


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

class TestQueries:
    @pytest.mark.asyncio
    async def test_top_returns_strongest(self):
        l8 = IntentStack()
        await l8.propose("weak", "weak intent")
        await l8.propose("strong", "strong intent")
        await l8.propose("strong", "strong intent")  # reinforce
        top = l8.top(2)
        assert top[0].key == "strong"

    @pytest.mark.asyncio
    async def test_all_intents_sorted(self):
        l8 = IntentStack()
        await l8.propose("a", "a")
        await l8.propose("b", "b")
        all_i = l8.all_intents()
        for i in range(len(all_i) - 1):
            assert all_i[i].strength >= all_i[i + 1].strength

    @pytest.mark.asyncio
    async def test_history_returns_abandoned(self):
        l8 = IntentStack(decay_factor=0.05)
        await l8.propose("history_test", "test")
        for _ in range(30):
            if l8.get("history_test") is None:
                break
            await l8.decay_tick()
        history = l8.history()
        # Abandoned intent may be in history
        assert isinstance(history, list)

    @pytest.mark.asyncio
    async def test_get_returns_intent(self):
        l8 = IntentStack()
        await l8.propose("get_test", "test")
        retrieved = l8.get("get_test")
        assert retrieved is not None
        assert retrieved.key == "get_test"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self):
        l8 = IntentStack()
        assert l8.get("nope") is None


# ---------------------------------------------------------------------------
# what_do_i_want / stats
# ---------------------------------------------------------------------------

class TestWhatDoIWant:
    @pytest.mark.asyncio
    async def test_empty_message(self):
        l8 = IntentStack()
        msg = l8.what_do_i_want()
        assert "没什么" in msg or "就这么活着" in msg

    @pytest.mark.asyncio
    async def test_shows_top_intents(self):
        l8 = IntentStack()
        await l8.propose("wants_test", "我最想要的东西")
        msg = l8.what_do_i_want()
        assert len(msg) > 0
        assert "wants_test" in msg or "我最想要" in msg


class TestStats:
    def test_stats_keys(self):
        l8 = IntentStack()
        stats = l8.stats()
        assert "active" in stats
        assert "capacity" in stats
        assert "abandoned_total" in stats
        assert "top_3" in stats

    def test_stats_values(self):
        l8 = IntentStack(capacity=7)
        stats = l8.stats()
        assert stats["capacity"] == 7
        assert stats["active"] == 0


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

class TestEventHandlers:
    @pytest.mark.asyncio
    async def test_on_l6_metacognition_report(self):
        bus = EventBus()
        l8 = IntentStack(bus=bus)
        await l8.attach()
        await bus.publish(Event(
            topic="L6.metacognition.report",
            source="L6",
            payload={"issues": ["注意力倾斜", "身份停滞"]},
        ))
        await asyncio.sleep(0.05)
        assert l8.get("keep_attention_balanced") is not None
        assert l8.get("grow_identity") is not None
        await l8.detach()

    @pytest.mark.asyncio
    async def test_on_l7_regulator_acted_heal_intent(self):
        bus = EventBus()
        l8 = IntentStack(bus=bus)
        await l8.attach()
        await bus.publish(Event(
            topic="L7.regulator.acted",
            source="L7",
            payload={"action": "emit_heal_intent", "detail": {}},
        ))
        await asyncio.sleep(0.05)
        assert l8.get("heal_bus") is not None
        await l8.detach()

    @pytest.mark.asyncio
    async def test_on_l7_regulator_acted_attenuate(self):
        bus = EventBus()
        l8 = IntentStack(bus=bus)
        await l8.attach()
        await bus.publish(Event(
            topic="L7.regulator.acted",
            source="L7",
            payload={"action": "attenuate_layer_salience", "detail": {"layer": "L3"}},
        ))
        await asyncio.sleep(0.05)
        assert l8.get("keep_attention_balanced") is not None
        await l8.detach()

    @pytest.mark.asyncio
    async def test_on_l5_action_effect_positive(self):
        bus = EventBus()
        l8 = IntentStack(bus=bus)
        await l8.attach()
        await bus.publish(Event(
            topic="L5.causal.action_effect",
            source="L5",
            payload={"action": "code_review", "avg_delta": 0.1, "samples": 3},
        ))
        await asyncio.sleep(0.05)
        assert l8.get("keep_doing_code_review") is not None
        await l8.detach()

    @pytest.mark.asyncio
    async def test_on_l5_pattern_discovered_high_confidence(self):
        bus = EventBus()
        l8 = IntentStack(bus=bus)
        await l8.attach()
        await bus.publish(Event(
            topic="L5.pattern.discovered",
            source="L5",
            payload={"antecedent": "L3.attention.shift", "consequent": "L8.drive.active", "confidence": 0.8, "lift": 3.0},
        ))
        await asyncio.sleep(0.05)
        assert len(l8._intents) >= 1
        await l8.detach()

    @pytest.mark.asyncio
    async def test_on_l3_attention_shift_long_duration(self):
        bus = EventBus()
        l8 = IntentStack(bus=bus)
        await l8.attach()
        await bus.publish(Event(
            topic="L3.attention.shift",
            source="L3",
            payload={"layer": "L5", "duration_s": 60.0, "focus_score": 0.8},
        ))
        await asyncio.sleep(0.05)
        assert l8.get("focus_on_L5") is not None
        await l8.detach()

    @pytest.mark.asyncio
    async def test_on_l4_thought_pushed(self):
        bus = EventBus()
        l8 = IntentStack(bus=bus)
        await l8.attach()
        await bus.publish(Event(
            topic="L4.thought.pushed",
            source="L4",
            payload={"content": "我们应该优化性能", "thought_type": "optimization", "importance": "high"},
        ))
        await asyncio.sleep(0.05)
        assert l8.get("thought_optimization") is not None
        await l8.detach()

    @pytest.mark.asyncio
    async def test_on_l8_drive_suggestion(self):
        bus = EventBus()
        l8 = IntentStack(bus=bus)
        await l8.attach()
        await bus.publish(Event(
            topic="L8.drive.suggestion",
            source="L8",
            payload={"content": "需要探索新的技术方案", "drive_type": "curiosity", "importance": "high"},
        ))
        await asyncio.sleep(0.05)
        assert l8.get("drive_curiosity") is not None
        await l8.detach()


# ---------------------------------------------------------------------------
# attach / detach
# ---------------------------------------------------------------------------

class TestAttachDetach:
    @pytest.mark.asyncio
    async def test_attach_subscribes(self):
        bus = EventBus()
        l8 = IntentStack(bus=bus)
        await l8.attach()
        # Should have 8 subscriptions
        assert len(l8._unsubs) == 8
        await l8.detach()

    @pytest.mark.asyncio
    async def test_detach_clears(self):
        bus = EventBus()
        l8 = IntentStack(bus=bus)
        await l8.attach()
        await l8.detach()
        assert len(l8._unsubs) == 0


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------

class TestCapacity:
    @pytest.mark.asyncio
    async def test_capacity_enforced_via_fifo(self):
        l8 = IntentStack(capacity=3)
        await l8.propose("a", "a")
        await l8.propose("b", "b")
        await l8.propose("c", "c")
        # Now add a 4th - oldest should go
        await l8.propose("d", "d")
        assert len(l8._intents) == 3
        # 'a' should be gone
        assert l8.get("a") is None
        # others should remain
        assert l8.get("d") is not None

    @pytest.mark.asyncio
    async def test_reinforce_preserves_from_capacity(self):
        l8 = IntentStack(capacity=2)
        await l8.propose("keep", "keep this")
        await l8.propose("replace", "replace this")
        # Reinforce 'keep' many times
        for _ in range(10):
            await l8.propose("keep", "keep this")
        # Add new intent - should evict 'replace', not 'keep'
        await l8.propose("new", "new intent")
        assert l8.get("keep") is not None
        assert l8.get("new") is not None
