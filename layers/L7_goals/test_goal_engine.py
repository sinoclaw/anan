"""
Tests for L7 Goals
"""

import asyncio

import pytest

from layers.L7_goals.goal_engine import Goal, GoalStatus, GoalScope, GoalGenerator


@pytest.fixture
def bus():
    from kernel.event_bus import EventBus
    return EventBus()


@pytest.fixture
def gen(bus):
    return GoalGenerator(bus=bus)


class TestGoal:
    def test_to_dict(self):
        g = Goal(
            id="test-1",
            description="完成某事",
            scope=GoalScope.MEDIUM,
        )
        d = g.to_dict()
        assert d["id"] == "test-1"
        assert d["scope"] == "medium"
        assert d["status"] == "proposed"


class TestGoalGenerator:
    @pytest.mark.asyncio
    async def test_propose(self, gen):
        g = gen.propose("完成 L7 goals", scope=GoalScope.SHORT)
        assert g.id.startswith("goal-")
        assert g.description == "完成 L7 goals"
        assert g.scope == GoalScope.SHORT
        assert g.status == GoalStatus.ACTIVE
        assert len(gen.active_goals()) == 1

    @pytest.mark.asyncio
    async def test_decompose_immediate(self, gen):
        g = gen.propose("立即任务", scope=GoalScope.IMMEDIATE)
        subs = gen.decompose(g.id)
        assert "immediate_actions" in g.to_dict()
        assert len(subs) >= 1

    @pytest.mark.asyncio
    async def test_decompose_medium_generates_week_and_month(self, gen):
        g = gen.propose("中期目标", scope=GoalScope.MEDIUM)
        subs = gen.decompose(g.id)
        assert len(g.this_week_actions) >= 1
        assert len(g.this_month_actions) >= 1
        assert len(subs) >= 2

    @pytest.mark.asyncio
    async def test_decompose_long_generates_subgoal(self, gen):
        g = gen.propose("长期目标", scope=GoalScope.LONG)
        subs = gen.decompose(g.id)
        assert len(g.sub_goals) >= 1
        assert len(subs) >= 1

    @pytest.mark.asyncio
    async def test_achieve(self, gen):
        g = gen.propose("可完成的任务", scope=GoalScope.IMMEDIATE)
        assert gen.achieve(g.id) is True
        assert g.status == GoalStatus.ACHIEVED
        assert g.achieved_at is not None
        assert len(gen.active_goals()) == 0

    @pytest.mark.asyncio
    async def test_abandon(self, gen):
        g = gen.propose("要放弃的任务", scope=GoalScope.SHORT)
        assert gen.abandon(g.id, "不再需要") is True
        assert g.status == GoalStatus.ABANDONED
        assert len(gen.active_goals()) == 0

    @pytest.mark.asyncio
    async def test_top_goals_sorted(self, gen):
        g1 = gen.propose("目标A", scope=GoalScope.LONG)
        g2 = gen.propose("目标B", scope=GoalScope.IMMEDIATE)
        g3 = gen.propose("目标C", scope=GoalScope.MEDIUM)
        # Boost priority of g1
        gen.boost_priority(g1.id, 0.5)
        top = gen.top_goals(n=3)
        assert len(top) == 3
        # g1 with boost should be first
        assert top[0].id == g1.id

    @pytest.mark.asyncio
    async def test_conflict_detection(self, gen):
        g1 = gen.propose("学习 Python", scope=GoalScope.SHORT, tags=["Python", "编程"])
        g2 = gen.propose("学习 JavaScript", scope=GoalScope.SHORT, tags=["JS", "编程"])
        conflicts = gen.detect_conflicts(g1.id)
        # Both have "编程" tag in same scope → conflict
        assert len(conflicts) >= 1

        g3 = gen.propose("写报告", scope=GoalScope.SHORT, tags=["写作", "报告"])
        conflicts = gen.detect_conflicts(g3.id)
        assert len(conflicts) == 0  # no overlap with g1/g2

    def test_what_are_my_goals(self, gen):
        gen.propose("目标1", scope=GoalScope.IMMEDIATE)
        gen.propose("目标2", scope=GoalScope.SHORT)
        desc = gen.what_are_my_goals()
        assert "目标1" in desc or "目标2" in desc

    def test_boost_priority(self, gen):
        g = gen.propose("可提升的目标", scope=GoalScope.LONG)
        assert g.priority_boost == 0.0
        gen.boost_priority(g.id, 0.3)
        assert g.priority_boost == 0.3
        # cap at 1.0
        gen.boost_priority(g.id, 1.0)
        assert g.priority_boost == 1.0
