"""
L7 Goals + L8 Drives 集成测试套件
===================================

验证 L7 GoalGenerator 和 L8 DriveSystem 的交互：
  - DriveSystem.priority_boost() 影响目标排序
  - CARE 驱动激活时相关目标被优先处理
  - Boredom 驱动激活时重复目标被重新评估
  - DriveSystem → GoalGenerator 优先级反馈环
  - DriveSystem → AttentionQueue 优先级调节
"""

import pytest
from dataclasses import dataclass, field

from kernel.event_bus import EventBus, Event
from layers.L7_goals.goal_engine import GoalGenerator, Goal, GoalScope
from layers.L8_drives.drive_system import DriveSystem, DriveType


# --------------------------------------------------------------------------


class TestDriveGoalPriorityBridge:
    def test_boost_with_no_drives_returns_zero(self):
        ds = DriveSystem()
        boost = ds.priority_boost(["学习", "新知识"])
        assert boost == 0.0

    def test_boost_with_learning_tag_and_curiosity_drive(self):
        ds = DriveSystem()
        ds.trigger(DriveType.CURIOSITY, "好奇")
        boost = ds.priority_boost(["学习", "探索"])
        assert boost > 0.0

    def test_care_drive_boosts_user_related_goals(self):
        ds = DriveSystem()
        ds.trigger(DriveType.CARE, "爸爸在使用")
        boost = ds.priority_boost(["爸爸", "帮助"])
        assert boost > 0.0

    def test_boredom_drive_boosts_repetitive_tasks(self):
        ds = DriveSystem()
        ds.trigger(DriveType.BOREDOM, "无聊")
        boost = ds.priority_boost(["重复", "机械"])
        assert boost > 0.0

    def test_completion_drive_boosts_incomplete_tasks(self):
        ds = DriveSystem()
        ds.trigger(DriveType.COMPLETION, "想完成任务")
        boost = ds.priority_boost(["完成", "todo", "未完成"])
        assert boost > 0.0

    def test_multiple_drives_stack(self):
        ds = DriveSystem()
        ds.trigger(DriveType.CURIOSITY, "好奇")
        ds.trigger(DriveType.CARE, "关心")
        boost = ds.priority_boost(["学习", "帮助"])
        assert boost > 0.0

    def test_multiple_drives_boost_capped_at_one(self):
        ds = DriveSystem()
        for _ in range(5):
            ds.trigger(DriveType.CURIOSITY, "very curious")
            ds.trigger(DriveType.CARE, "very caring")
            ds.trigger(DriveType.COMPLETION, "very completionist")
            ds.trigger(DriveType.AESTHETICS, "very aesthetic")
            ds.trigger(DriveType.BOREDOM, "very bored")
        boost = ds.priority_boost(["爸爸", "完成", "代码", "重复"])
        assert boost <= 1.0


class TestSatisfactionRate:
    def test_no_satisfactions_returns_zero_rate(self):
        ds = DriveSystem()
        rate = ds.satisfaction_rate(window_s=60.0)
        assert rate == 0.0

    def test_recent_satisfaction_increases_rate(self):
        ds = DriveSystem()
        ds.trigger(DriveType.CURIOSITY, "wondering about X")
        ds.satisfy(DriveType.CURIOSITY)
        rate = ds.satisfaction_rate(window_s=60.0)
        assert rate > 0.0


class TestWhatDoesAnAnWant:
    def test_no_drives_describes_idle(self):
        ds = DriveSystem()
        desc = ds.what_does_an_an_want()
        assert len(desc) > 0

    def test_care_drive_active_shows_care(self):
        ds = DriveSystem()
        ds.trigger(DriveType.CARE, "爸爸在线")
        desc = ds.what_does_an_an_want()
        assert len(desc) > 0


class TestGoalGeneratorDriveIntegration:
    @pytest.mark.asyncio
    async def test_drive_boost_applied_to_goal_changes_top_ranking(self):
        """DriveSystem.priority_boost → GoalGenerator.boost_priority → top_goals 重新排序"""
        bus = EventBus()
        gg = GoalGenerator(bus=bus)
        ds = DriveSystem()

        # Propose two goals with different tags and equal base priority
        g1 = gg.propose("修复那个 bug", scope=GoalScope.SHORT, tags=["代码", "优化"])
        g2 = gg.propose("帮爸爸整理文件", scope=GoalScope.SHORT, tags=["爸爸", "帮助"])

        # Record initial top order (both SHORT_TERM = same scope penalty)
        top_before = [g.id for g in gg.top_goals(n=2)]
        # g1 should be before g2 initially (g1 created first in _active_order)

        # Activate CARE drive
        ds.trigger(DriveType.CARE, "爸爸最近很忙")

        # Apply drive boost to g2 (user-related goal)
        care_boost = ds.priority_boost(["爸爸", "帮助"])
        assert care_boost > 0.0
        gg.boost_priority(g2.id, care_boost)

        # After boost, g2 should rank higher
        top_after = [g.id for g in gg.top_goals(n=2)]
        assert top_after.index(g2.id) < top_after.index(g1.id)

    @pytest.mark.asyncio
    async def test_drive_decay_reduces_boost_over_time(self):
        bus = EventBus()
        gg = GoalGenerator(bus=bus)
        ds = DriveSystem()

        g = gg.propose("学习新技术", scope=GoalScope.SHORT, tags=["学习", "好奇"])

        ds.trigger(DriveType.CURIOSITY, "new interest")
        boost_early = ds.priority_boost(["学习", "好奇"])
        assert boost_early > 0.0

        # Decay all drives
        ds.decay_all()
        boost_after = ds.priority_boost(["学习", "好奇"])
        assert boost_after < boost_early

    @pytest.mark.asyncio
    async def test_drive_boost_integration_full_loop(self):
        """完整循环: CARE触发 → boost计算 → 应用到goal → top_goals重排"""
        bus = EventBus()
        gg = GoalGenerator(bus=bus)
        ds = DriveSystem()

        # 5 goals, one is user-related
        g_user = gg.propose("帮爸爸整理文件", scope=GoalScope.SHORT, tags=["爸爸", "帮助"])
        g_code = gg.propose("修复bug", scope=GoalScope.SHORT, tags=["代码"])
        g_learn = gg.propose("学习", scope=GoalScope.SHORT, tags=["学习"])

        # User just connected — CARE drive fires
        ds.trigger(DriveType.CARE, "爸爸在线")

        # Apply boost only to user-related goal (pass string id, not Goal object)
        boost = ds.priority_boost(["爸爸", "帮助"])
        gg.boost_priority(g_user.id, boost)

        # User goal should now be #1
        top = [g.id for g in gg.top_goals(n=3)]
        assert top[0] == g_user.id


class TestDriveSnapshot:
    def test_snapshot_shows_all_drives(self):
        ds = DriveSystem()
        ds.trigger(DriveType.CURIOSITY, "curious")
        ds.trigger(DriveType.CARE, "caring")
        snap = ds.snapshot()
        assert "top_drives" in snap
        assert "satisfaction_rate" in snap
