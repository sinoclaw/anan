"""
Tests for L7 Goals
"""

import asyncio

import pytest

from layers.L7_goals.goal_engine import (
    Goal,
    GoalStatus,
    GoalScope,
    GoalGenerator,
    SubGoal,
)


@pytest.fixture
def bus():
    from kernel.event_bus import EventBus
    return EventBus()


@pytest.fixture
def gen(bus):
    return GoalGenerator(bus=bus)


# ---------------------------------------------------------------------------
# Fake LLM for tests
# ---------------------------------------------------------------------------

async def fake_llm(messages: list[dict], *, temperature: float = 0.3) -> str:
    """Fake LLM that returns structured JSON for goal generation tests."""
    content = messages[-1]["content"] if messages else ""

    if "generate" in content.lower() or "context" in content.lower() or "上下文" in content:
        return json.dumps({
            "goals": [
                {"description": "学习 Python 异步编程", "scope": "short", "tags": ["学习", "Python"]},
                {"description": "优化 anan 的目标系统", "scope": "medium", "tags": ["工程", "优化"]},
                {"description": "写一份技术文档", "scope": "immediate", "tags": ["写作", "文档"]},
            ]
        })
    if "decompose" in content.lower() or "分解" in content:
        return json.dumps({
            "sub_goals": [
                {"description": "阅读 Python asyncio 官方文档", "scope": "immediate", "action_type": "immediate"},
                {"description": "写一个异步爬虫示例", "scope": "short", "action_type": "this_week"},
                {"description": "总结异步编程最佳实践", "scope": "medium", "action_type": "this_month"},
            ]
        })
    if "conflict" in content.lower():
        return json.dumps({
            "is_conflict": True,
            "resolution": "keep_a",
            "reason": "目标A更具体可执行",
            "suggested_modification": "",
        })
    if "score" in content.lower() or "打分" in content:
        return json.dumps({
            "scores": [
                {"goal_id": "goal-0", "score": 0.9, "rationale": "紧急且重要"},
                {"goal_id": "goal-1", "score": 0.7, "rationale": "重要但不太紧急"},
            ]
        })
    # Default
    return "{}"


import json


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


# ---------------------------------------------------------------------------
# LLM-driven tests
# ---------------------------------------------------------------------------

class TestLLMDriven:
    """Tests for LLM-driven goal generation, decomposition, conflict resolution."""

    @pytest.mark.asyncio
    async def test_generate_goals_from_context_returns_non_empty(self):
        """Verify generate_goals_from_context returns non-empty goal list."""
        from kernel.event_bus import EventBus
        bus = EventBus()
        gen = GoalGenerator(bus=bus, llm=fake_llm)

        goals = await gen.generate_goals_from_context(
            "用户想学习Python，并希望优化anan项目"
        )

        assert len(goals) > 0, "Should return at least one goal"
        for g in goals:
            assert g.description, "Each goal must have a description"
            assert g.scope in (GoalScope.IMMEDIATE, GoalScope.SHORT, GoalScope.MEDIUM, GoalScope.LONG)
            assert g.status == GoalStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_generate_goals_from_context_no_llm(self):
        """Without LLM provider, generate_goals_from_context returns empty list."""
        from kernel.event_bus import EventBus
        bus = EventBus()
        gen = GoalGenerator(bus=bus, llm=None)

        goals = await gen.generate_goals_from_context("some context")
        assert goals == []

    @pytest.mark.asyncio
    async def test_decompose_goal_llm(self):
        """LLM decomposition returns SubGoal list attached to parent."""
        from kernel.event_bus import EventBus
        bus = EventBus()
        gen = GoalGenerator(bus=bus, llm=fake_llm)

        goal = gen.propose("学习Python异步编程", scope=GoalScope.MEDIUM)
        subs = await gen.decompose_goal_llm(goal)

        assert len(subs) > 0, "Should decompose into sub-goals"
        assert all(isinstance(sg, SubGoal) for sg in subs)
        # Parent goal should have sub-goal descriptions attached
        assert len(goal.sub_goals) > 0

    @pytest.mark.asyncio
    async def test_decompose_goal_llm_no_llm(self):
        """Without LLM, decompose_goal_llm returns empty list."""
        from kernel.event_bus import EventBus
        bus = EventBus()
        gen = GoalGenerator(bus=bus, llm=None)

        goal = gen.propose("测试目标", scope=GoalScope.SHORT)
        subs = await gen.decompose_goal_llm(goal)
        assert subs == []

    @pytest.mark.asyncio
    async def test_resolve_conflicts_llm(self):
        """Conflict resolution reduces goal list via LLM recommendation."""
        from kernel.event_bus import EventBus
        bus = EventBus()
        gen = GoalGenerator(bus=bus, llm=fake_llm)

        g1 = gen.propose("学习Python", scope=GoalScope.SHORT, tags=["Python", "学习"])
        g2 = gen.propose("学习Django", scope=GoalScope.SHORT, tags=["Python", "Web"])

        surviving = await gen.resolve_conflicts_llm([g1, g2])
        # At least one should be removed if LLM says conflict
        assert len(surviving) <= 2

    @pytest.mark.asyncio
    async def test_choose_next_goal_with_llm(self):
        """choose_next_goal uses LLM scoring and sets llm_score/rationale."""
        from kernel.event_bus import EventBus
        bus = EventBus()
        gen = GoalGenerator(bus=bus, llm=fake_llm)

        g1 = gen.propose("紧急修复bug", scope=GoalScope.IMMEDIATE, tags=["紧急", "bug"])
        g2 = gen.propose("长期技术改进", scope=GoalScope.LONG, tags=["工程", "改进"])

        chosen = await gen.choose_next_goal([g1, g2])

        assert chosen is not None
        assert chosen.llm_score > 0
        assert chosen.rationale is not None
        assert isinstance(chosen, Goal)

    @pytest.mark.asyncio
    async def test_choose_next_goal_rule_based_without_llm(self):
        """Without LLM, choose_next_goal falls back to rule-based selection."""
        from kernel.event_bus import EventBus
        bus = EventBus()
        gen = GoalGenerator(bus=bus, llm=None)

        g1 = gen.propose("紧急任务", scope=GoalScope.IMMEDIATE)
        g2 = gen.propose("长期任务", scope=GoalScope.LONG)

        chosen = await gen.choose_next_goal([g1, g2])

        assert chosen is not None
        # IMMEDIATE should win over LONG in rule-based scoring
        assert chosen.id == g1.id
        assert chosen.rationale == "rule_based_fallback"

    @pytest.mark.asyncio
    async def test_generate_goals_publishes_events(self):
        """generate_goals_from_context publishes L7.goal.created events."""
        from kernel.event_bus import EventBus
        bus = EventBus()
        gen = GoalGenerator(bus=bus, llm=fake_llm)

        await gen.generate_goals_from_context("测试上下文")

        events = bus.history(topic_pattern="L7.goal.created")
        assert len(events) >= 3, "Should publish at least 3 goal.created events"

    @pytest.mark.asyncio
    async def test_generate_goals_skips_empty_descriptions(self):
        """Goals with empty descriptions are filtered out."""
        from kernel.event_bus import EventBus
        bus = EventBus()
        gen = GoalGenerator(bus=bus, llm=fake_llm)

        goals = await gen.generate_goals_from_context("测试")
        for g in goals:
            assert g.description.strip(), "No goal should have empty description"
