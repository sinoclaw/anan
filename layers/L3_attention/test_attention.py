"""
Tests for L3 Attention System
"""

import asyncio

import pytest

from layers.L3_attention.attention import (
    AttentionQueue,
    AttentionItem,
    AttentionScore,
    PreemptiveMode,
    Priority,
    VigilanceMonitor,
)


class TestAttentionScore:
    def test_total_weighted(self):
        s = AttentionScore(urgency=1.0, importance=1.0, interest=1.0)
        assert s.total() == 1.0

    def test_total_zero(self):
        s = AttentionScore(urgency=0.0, importance=0.0, interest=0.0)
        assert s.total() == 0.0

    def test_total_mixed(self):
        s = AttentionScore(urgency=0.8, importance=0.6, interest=0.4)
        # 0.5*0.8 + 0.3*0.6 + 0.2*0.4 = 0.4+0.18+0.08 = 0.66
        assert abs(s.total() - 0.66) < 0.01

    def test_to_dict(self):
        s = AttentionScore(urgency=0.5, importance=0.5, interest=0.5)
        d = s.to_dict()
        assert d["urgency"] == 0.5
        assert d["total"] == 0.5


class TestAttentionItem:
    def test_suppress_penalty(self):
        item = AttentionItem(
            id="test", label="test", source="L4",
            score=AttentionScore(0.5, 0.5, 0.5),
            priority=Priority.MEDIUM,
        )
        assert item.total_score() == 0.5  # no penalty yet
        item.suppress_count = 2
        # 0.5 - 2*0.05 = 0.4
        assert item.total_score() == 0.4

    def test_expired(self):
        import time
        item = AttentionItem(
            id="test", label="test", source="L4",
            score=AttentionScore(0.5, 0.5, 0.5),
            priority=Priority.MEDIUM,
        )
        assert not item.is_expired()
        item.created_at = time.time() - 1000  # far in the past
        item.ttl_s = 0.1
        assert item.is_expired()


class TestAttentionQueue:
    @pytest.fixture
    def bus(self):
        from kernel.event_bus import EventBus
        return EventBus()

    @pytest.fixture
    def q(self, bus):
        return AttentionQueue(bus=bus, focus_threshold=0.5)

    @pytest.mark.asyncio
    async def test_enqueue_returns_item(self, q):
        item = q.enqueue(
            "a1", "test task", "L4",
            score=AttentionScore(0.7, 0.6, 0.5),
        )
        assert item.id == "a1"
        assert item.label == "test task"

    @pytest.mark.asyncio
    async def test_focus_returns_highest_score(self, q):
        q.enqueue("low", "low task", "L4",
                  score=AttentionScore(0.3, 0.3, 0.3))
        q.enqueue("high", "high task", "L4",
                  score=AttentionScore(0.9, 0.8, 0.7))
        top = q.focus()
        assert top.id == "high"

    @pytest.mark.asyncio
    async def test_complete_removes_item(self, q):
        q.enqueue("t1", "task1", "L4",
                  score=AttentionScore(0.5, 0.5, 0.5))
        assert q.complete("t1") is True
        assert q.focus() is None

    @pytest.mark.asyncio
    async def test_duplicate_id_replaces(self, q):
        q.enqueue("same", "first", "L4",
                  score=AttentionScore(0.5, 0.5, 0.5))
        q.enqueue("same", "second", "L4",
                  score=AttentionScore(0.8, 0.8, 0.8))
        snap = q.queue_snapshot()
        assert len(snap) == 1
        assert snap[0]["label"] == "second"

    @pytest.mark.asyncio
    async def test_focus_below_threshold_returns_none(self, q):
        q.enqueue("low", "low", "L4",
                  score=AttentionScore(0.1, 0.1, 0.1))
        assert q.focus() is None

    @pytest.mark.asyncio
    async def test_snapshot(self, q):
        q.enqueue("t1", "task1", "L4",
                  score=AttentionScore(0.6, 0.5, 0.4))
        snap = q.queue_snapshot()
        assert len(snap) == 1
        assert snap[0]["id"] == "t1"
        assert "total_score" in snap[0]

    @pytest.mark.asyncio
    async def test_suppress_degrades(self, q):
        q.enqueue("t1", "task1", "L4",
                  score=AttentionScore(0.56, 0.56, 0.56))
        assert q.focus().id == "t1"
        q.suppress("t1")
        # Score drops: 0.56 - 0.05 = 0.51, still above threshold 0.5
        assert q.focus().id == "t1"
        q.suppress("t1")
        # Score drops: 0.56 - 0.10 = 0.46, below threshold 0.5
        assert q.focus() is None

    @pytest.mark.asyncio
    async def test_preemptive_mode_defusing(self, q, bus):
        q.set_mode(PreemptiveMode.DEFUSING)
        q.enqueue("focused", "currently focused", "L4",
                  score=AttentionScore(0.5, 0.5, 0.5),
                  priority=Priority.MEDIUM)
        q.enqueue("urgent", "urgent task", "L7",
                  score=AttentionScore(0.9, 0.9, 0.9),
                  priority=Priority.HIGH)
        snap = q.queue_snapshot()
        ids = [i["id"] for i in snap]
        assert "focused" in ids
        assert "urgent" in ids

    def test_set_mode(self, q):
        assert q._mode == PreemptiveMode.NORMAL
        q.set_mode(PreemptiveMode.FOCUSED)
        assert q._mode == PreemptiveMode.FOCUSED


class TestVigilanceMonitor:
    @pytest.fixture
    def bus(self):
        from kernel.event_bus import EventBus
        return EventBus()

    @pytest.mark.asyncio
    async def test_record_and_check(self, bus):
        vm = VigilanceMonitor(bus=bus, window_s=10, threshold=0.35, consecutive_trigger=1)
        vm.record_focus_start()
        import time; time.sleep(0.05)
        vm.record_focus_end()
        result = vm.check()
        assert result is None or isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_consecutive_trigger(self, bus):
        """After 3 consecutive low-focus cycles (consecutive_low reaches 3),
        check() should emit the L3.vigilance.low event and return non-None.

        Tracing:
          cycle 1-2: recent < 3 → return None, consecutive_low not incremented
          cycle 3: recent=3, ratio=1.0 → consecutive_low=1, not triggered
          cycle 4: recent=4, ratio=1.0 → consecutive_low=2, not triggered
          cycle 5: recent=5, ratio=1.0 → consecutive_low=3, TRIGGERED → return dict"""
        import time
        vm = VigilanceMonitor(bus=bus, window_s=10, threshold=0.99, consecutive_trigger=3)

        results = []
        for _ in range(6):
            vm.record_focus_start()
            time.sleep(0.01)
            vm.record_focus_end()
            results.append(vm.check())

        # The 5th check (index 4) should trigger
        assert results[4] is not None, "5th check should trigger vigilance"
        assert "suggestion" in results[4]
