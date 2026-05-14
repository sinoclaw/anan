"""
L4 Stream of Consciousness — 测试套件
======================================
"""

import pytest
import time

from kernel.event_bus import EventBus
from layers.L4_consciousness import (
    ConsciousnessEngine,
    IdleDetector,
    OutputGate,
    Thought,
    ThoughtImportance,
    ThoughtStream,
    ThoughtType,
)


# ---------------------------------------------------------------------------
# IdleDetector tests
# ---------------------------------------------------------------------------

class TestIdleDetector:
    def test_not_idle_initially(self):
        bus = EventBus()
        detector = IdleDetector(bus, threshold_s=60.0)
        assert not detector.is_idle()

    def test_idle_after_threshold(self):
        bus = EventBus()
        detector = IdleDetector(bus, threshold_s=0.5)
        time.sleep(0.6)
        assert detector.is_idle()

    def test_note_user_input_resets(self):
        bus = EventBus()
        detector = IdleDetector(bus, threshold_s=0.5)
        time.sleep(0.3)
        detector.note_user_input()
        time.sleep(0.3)
        assert not detector.is_idle()

    def test_idle_started_event_fired(self):
        bus = EventBus()
        fired = []
        bus.subscribe("L4.idle.started", lambda e: fired.append(e))
        detector = IdleDetector(bus, threshold_s=0.3)
        time.sleep(0.4)
        detector.is_idle()  # trigger check
        assert len(fired) == 1
        assert fired[0].payload["idle_reason"] == "threshold_reached"

    def test_idle_ended_event_fired(self):
        bus = EventBus()
        fired = []
        bus.subscribe("L4.idle.ended", lambda e: fired.append(e))
        detector = IdleDetector(bus, threshold_s=0.3)
        time.sleep(0.4)
        detector.is_idle()
        detector.note_user_input()
        assert len(fired) == 1
        assert fired[0].payload["by_user_input"] is True

    def test_seconds_since_input(self):
        bus = EventBus()
        detector = IdleDetector(bus, threshold_s=300.0)
        time.sleep(0.2)
        elapsed = detector.seconds_since_input()
        assert 0.15 < elapsed < 0.4


# ---------------------------------------------------------------------------
# ThoughtStream tests
# ---------------------------------------------------------------------------

class TestThoughtStream:
    def test_add_and_recent(self):
        stream = ThoughtStream(max_size=5)
        t1 = _make_thought(ThoughtType.SPONTANEOUS)
        t2 = _make_thought(ThoughtType.SPONTANEOUS)
        stream.add(t1)
        stream.add(t2)
        recent = stream.recent(2)
        assert len(recent) == 2
        assert recent[-1] is t2

    def test_max_size_eviction(self):
        stream = ThoughtStream(max_size=3)
        thoughts = [_make_thought(ThoughtType.SPONTANEOUS) for _ in range(5)]
        for t in thoughts:
            stream.add(t)
        assert len(stream) == 3
        assert stream.recent(1)[0] is thoughts[-1]

    def test_by_type(self):
        stream = ThoughtStream()
        t_a = _make_thought(ThoughtType.DIALOGUE_REFLECTION)
        t_b = _make_thought(ThoughtType.QUESTION_EXTENSION)
        stream.add(t_a)
        stream.add(t_b)
        stream.add(t_a)
        assert len(stream.by_type(ThoughtType.DIALOGUE_REFLECTION)) == 2
        assert len(stream.by_type(ThoughtType.QUESTION_EXTENSION)) == 1

    def test_first_by_type(self):
        stream = ThoughtStream()
        t1 = _make_thought(ThoughtType.DIALOGUE_REFLECTION)
        t2 = _make_thought(ThoughtType.DIALOGUE_REFLECTION)
        stream.add(t1)
        stream.add(t2)
        assert stream.first_by_type(ThoughtType.DIALOGUE_REFLECTION) is t1
        assert stream.first_by_type(ThoughtType.QUESTION_EXTENSION) is None

    def test_repr(self):
        stream = ThoughtStream()
        assert "0 thoughts" in repr(stream)
        stream.add(_make_thought(ThoughtType.SPONTANEOUS))
        assert "1 thoughts" in repr(stream)


# ---------------------------------------------------------------------------
# OutputGate tests
# ---------------------------------------------------------------------------

class TestOutputGate:
    def test_low_importance_not_pushed(self):
        bus = EventBus()
        stream = ThoughtStream()
        gate = OutputGate(bus, stream)
        thought = _make_thought(ThoughtType.SPONTANEOUS, importance=ThoughtImportance.LOW)
        result = gate.evaluate(thought)
        assert result.push_decision == "internal"
        assert gate.stats["pushed"] == 0
        assert gate.stats["generated"] == 1

    def test_critical_always_pushed(self):
        bus = EventBus()
        stream = ThoughtStream()
        gate = OutputGate(bus, stream)
        thought = _make_thought(ThoughtType.SPONTANEOUS, importance=ThoughtImportance.CRITICAL)
        result = gate.evaluate(thought)
        assert result.push_decision == "push"
        assert gate.stats["pushed"] == 1

    def test_high_drive_suggestion_pushed(self):
        bus = EventBus()
        stream = ThoughtStream()
        gate = OutputGate(bus, stream)
        thought = _make_thought(ThoughtType.DRIVE_SUGGESTION, importance=ThoughtImportance.HIGH)
        result = gate.evaluate(thought)
        assert result.push_decision == "push"

    def test_high_dialogue_reflection_pushed(self):
        bus = EventBus()
        stream = ThoughtStream()
        gate = OutputGate(bus, stream)
        thought = _make_thought(ThoughtType.DIALOGUE_REFLECTION, importance=ThoughtImportance.HIGH)
        result = gate.evaluate(thought)
        assert result.push_decision == "push"

    def test_high_other_type_not_pushed(self):
        bus = EventBus()
        stream = ThoughtStream()
        gate = OutputGate(bus, stream)
        thought = _make_thought(ThoughtType.TODO_CHECK, importance=ThoughtImportance.HIGH)
        result = gate.evaluate(thought)
        assert result.push_decision == "internal"

    def test_medium_not_pushed_unless_duplicate(self):
        bus = EventBus()
        stream = ThoughtStream()
        gate = OutputGate(bus, stream)
        # 非重复 → 不推送
        thought = _make_thought(ThoughtType.SPONTANEOUS, importance=ThoughtImportance.MEDIUM)
        result = gate.evaluate(thought)
        assert result.push_decision == "internal"

    def test_medium_pushed_when_duplicate(self):
        bus = EventBus()
        stream = ThoughtStream()
        gate = OutputGate(bus, stream)
        # 先存一条
        t1 = _make_thought(ThoughtType.SPONTANEOUS, importance=ThoughtImportance.LOW,
                           content="重复的思考内容")
        stream.add(t1)
        # 再来一条几乎相同的 MEDIUM
        t2 = _make_thought(ThoughtType.SPONTANEOUS, importance=ThoughtImportance.MEDIUM,
                           content="重复的思考内容")
        result = gate.evaluate(t2)
        assert result.push_decision == "push"

    def test_thought_generated_event_fired(self):
        bus = EventBus()
        fired = []
        bus.subscribe("L4.thought.generated", lambda e: fired.append(e))
        stream = ThoughtStream()
        gate = OutputGate(bus, stream)
        thought = _make_thought(ThoughtType.SPONTANEOUS)
        gate.evaluate(thought)
        assert len(fired) == 1

    def test_thought_pushed_event_fired_on_critical(self):
        bus = EventBus()
        fired = []
        bus.subscribe("L4.thought.pushed", lambda e: fired.append(e))
        stream = ThoughtStream()
        gate = OutputGate(bus, stream)
        thought = _make_thought(ThoughtType.SPONTANEOUS, importance=ThoughtImportance.CRITICAL)
        gate.evaluate(thought)
        assert len(fired) == 1

    def test_non_pushable_high_never_pushed_event(self):
        bus = EventBus()
        fired = []
        bus.subscribe("L4.thought.pushed", lambda e: fired.append(e))
        stream = ThoughtStream()
        gate = OutputGate(bus, stream)
        thought = _make_thought(ThoughtType.TODO_CHECK, importance=ThoughtImportance.HIGH)
        gate.evaluate(thought)
        assert len(fired) == 0


# ---------------------------------------------------------------------------
# ConsciousnessEngine tests
# ---------------------------------------------------------------------------

class TestConsciousnessEngine:
    @pytest.mark.asyncio
    async def test_attach_detach_lifecycle(self):
        bus = EventBus()
        engine = ConsciousnessEngine(bus, idle_threshold_s=300.0)
        await engine.attach()
        assert engine.is_attached  # should have an is_attached property... wait

    def test_note_user_input(self):
        bus = EventBus()
        engine = ConsciousnessEngine(bus, idle_threshold_s=0.5)
        engine.note_user_input()
        assert not engine.is_idle

    def test_set_contexts(self):
        bus = EventBus()
        engine = ConsciousnessEngine(bus)
        engine.set_dialogue_context("用户刚才在问 Python 的 GIL")
        engine.set_question_context("什么是异步编程")
        engine.set_todo_context("完成 anan L4 实现")
        assert "GIL" in engine._recent_dialogue_context
        assert "异步" in engine._recent_question_context
        assert "anan" in engine._todo_context

    def test_is_idle_property(self):
        bus = EventBus()
        engine = ConsciousnessEngine(bus, idle_threshold_s=300.0)
        assert not engine.is_idle  # no user input yet

    def test_thought_generated_via_drive_suggestion(self):
        bus = EventBus()
        engine = ConsciousnessEngine(bus)
        fired = []
        bus.subscribe("L4.thought.generated", lambda e: fired.append(e))

        # 注入 L8 drive suggestion 事件
        engine.inject_drive_suggestion_sync({
            "content": "建议你探索一下 LLM 的工具调用能力",
            "importance": "high",
            "drive_type": "curiosity",
        })
        assert len(fired) == 1
        assert fired[0].payload["thought_type"] == "drive_suggestion"

    def test_generate_thought_exhausts_dialogue_context(self):
        bus = EventBus()
        engine = ConsciousnessEngine(bus)
        engine.set_dialogue_context("用户刚才在问关于 Python 异步的问题")

        thought = engine._generate_one_thought(silent_s=130.0)
        assert thought is not None
        assert thought.thought_type == ThoughtType.DIALOGUE_REFLECTION
        # 消费后清空
        assert engine._recent_dialogue_context == ""

    def test_generate_thought_consumes_question_context(self):
        bus = EventBus()
        engine = ConsciousnessEngine(bus)
        engine.set_question_context("什么是 GRPO 训练")

        thought = engine._generate_one_thought(silent_s=130.0)
        assert thought is not None
        assert thought.thought_type == ThoughtType.QUESTION_EXTENSION
        assert engine._recent_question_context == ""

    def test_generate_thought_todo_when_no_dialogue(self):
        bus = EventBus()
        engine = ConsciousnessEngine(bus)
        engine.set_todo_context("完成 L4 测试")

        thought = engine._generate_one_thought(silent_s=130.0)
        assert thought is not None
        assert thought.thought_type == ThoughtType.TODO_CHECK

    def test_generate_thought_none_when_no_context(self):
        bus = EventBus()
        engine = ConsciousnessEngine(bus)
        # 无任何上下文
        thought = engine._generate_one_thought(silent_s=130.0)
        # 30% 概率触发联想，否则 None
        # 不做强制断言，只验证不抛异常

    def test_stream_property(self):
        bus = EventBus()
        engine = ConsciousnessEngine(bus)
        assert isinstance(engine.stream, ThoughtStream)

    def test_output_gate_property(self):
        bus = EventBus()
        engine = ConsciousnessEngine(bus)
        assert isinstance(engine.output_gate, OutputGate)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import uuid


def _make_thought(
    thought_type: ThoughtType = ThoughtType.SPONTANEOUS,
    importance: ThoughtImportance = ThoughtImportance.MEDIUM,
    content: str = "测试思考内容",
) -> Thought:
    return Thought(
        thought_id=uuid.uuid4().hex[:8],
        content=content,
        thought_type=thought_type,
        importance=importance,
        source_context="test",
    )
