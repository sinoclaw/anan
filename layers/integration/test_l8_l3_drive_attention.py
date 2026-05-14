"""
L8 DriveSystem → L3 Attention 桥接测试
=======================================

覆盖：
  AttentionQueue.boost() — 加分、优先级升级、boosted 事件
  AttentionBridge — L8.drive.updated → L3.attention.boosted
  端到端：DriveSystem 激活 → AttentionQueue item 被 boost
"""

import pytest
from kernel.event_bus import EventBus, Event
from layers.L3_attention.attention import AttentionQueue, AttentionScore, Priority
from layers.L8_drives.drive_system import DriveSystem
from layers.L8_drives.attention_bridge import AttentionBridge


class TestAttentionQueueBoost:
    def test_boost_found_item_increases_boost_field(self):
        """boost() 找到 item → boost 字段增加。"""
        bus = EventBus()
        q = AttentionQueue(bus=bus)
        q.enqueue("goal-1", "帮爸爸整理文件", source="L7",
                  score=AttentionScore(0.5, 0.5, 0.5), priority=Priority.MEDIUM)

        found = q.boost("goal-1", extra_score=0.2)
        assert found is True

        item = next(i for i in q._items if i.id == "goal-1")
        assert item.boost == 0.2

    def test_boost_not_found_returns_false(self):
        """boost() 未找到 item → 返回 False。"""
        bus = EventBus()
        q = AttentionQueue(bus=bus)
        found = q.boost("nonexistent", extra_score=0.2)
        assert found is False

    def test_boost_accumulates(self):
        """连续 boost() 累加。"""
        bus = EventBus()
        q = AttentionQueue(bus=bus)
        q.enqueue("goal-1", "任务", source="L7",
                  score=AttentionScore(0.5, 0.5, 0.5))

        q.boost("goal-1", extra_score=0.1)
        q.boost("goal-1", extra_score=0.2)

        item = next(i for i in q._items if i.id == "goal-1")
        assert item.boost == pytest.approx(0.3)

    def test_boost_upgrades_priority_background_to_medium(self):
        """boost() 将 BACKGROUND → MEDIUM。"""
        bus = EventBus()
        q = AttentionQueue(bus=bus)
        q.enqueue("goal-1", "任务", source="L7",
                  score=AttentionScore(0.5, 0.5, 0.5), priority=Priority.BACKGROUND)

        q.boost("goal-1")

        item = next(i for i in q._items if i.id == "goal-1")
        assert item.priority == Priority.MEDIUM

    def test_boost_upgrades_priority_medium_to_high(self):
        """boost() 将 MEDIUM → HIGH。"""
        bus = EventBus()
        q = AttentionQueue(bus=bus)
        q.enqueue("goal-1", "任务", source="L7",
                  score=AttentionScore(0.5, 0.5, 0.5), priority=Priority.MEDIUM)

        q.boost("goal-1")

        item = next(i for i in q._items if i.id == "goal-1")
        assert item.priority == Priority.HIGH

    def test_boost_emits_boosted_event(self):
        """boost() 发送 L3.attention.boosted 事件。"""
        bus = EventBus()
        q = AttentionQueue(bus=bus)
        q.enqueue("goal-1", "帮爸爸", source="L7",
                  score=AttentionScore(0.5, 0.5, 0.5))

        events = []
        bus.subscribe("L3.attention.boosted", lambda e: events.append(e))
        q.boost("goal-1", extra_score=0.15)

        assert len(events) == 1
        assert events[0].payload["id"] == "goal-1"
        assert events[0].payload["extra_score"] == 0.15
        assert events[0].payload["total_boost"] == 0.15


class TestAttentionBridge:
    @pytest.mark.asyncio
    async def test_subscribes_to_l8_drive_updated(self):
        """AttentionBridge.attach() 订阅 L8.drive.updated。"""
        bridge = AttentionBridge(attention_q=AttentionQueue())
        assert len(bridge._unsubs) == 0

        await bridge.attach()
        assert len(bridge._unsubs) == 1  # subscribed

        await bridge.detach()

    @pytest.mark.asyncio
    async def test_drive_active_boosts_matching_attention_items(self):
        """L8.drive.updated(active=True) → 匹配标签的注意力项被 boost。"""
        bus = EventBus()
        q = AttentionQueue(bus=bus)
        ds = DriveSystem(bus=bus)

        bridge = AttentionBridge(bus=bus, attention_q=q, drive_system=ds)
        await bridge.attach()

        # 入队两项，一项标签匹配爸爸
        q.enqueue("goal-care", "帮爸爸整理文件", source="L7",
                  score=AttentionScore(0.5, 0.5, 0.5), priority=Priority.MEDIUM)
        q.enqueue("goal-code", "修复bug", source="L7",
                  score=AttentionScore(0.5, 0.5, 0.5), priority=Priority.MEDIUM)

        # 激活 CARE drive（需要先激活才能有 boost）
        from layers.L8_drives.drive_system import DriveType
        ds._drives[DriveType.CARE].active = True
        ds._drives[DriveType.CARE].strength = 0.5

        # 直接调用 bridge 的 handler
        from kernel.event_bus import Event
        bridge._on_drive_updated(Event(
            topic="L8.drive.updated",
            source="test",
            payload={"drive": "CARE", "active": True, "goal_tags": ["爸爸"]},
        ))

        item_care = next(i for i in q._items if i.id == "goal-care")
        item_code = next(i for i in q._items if i.id == "goal-code")

        assert item_care.boost > 0, f"goal-care should be boosted, got boost={item_care.boost}"
        assert item_code.boost == 0.0, "goal-code should not be boosted"

        await bridge.detach()

    @pytest.mark.asyncio
    async def test_drive_inactive_does_nothing(self):
        """L8.drive.updated(active=False) → 不 boost。"""
        bus = EventBus()
        q = AttentionQueue(bus=bus)
        bridge = AttentionBridge(bus=bus, attention_q=q)

        await bridge.attach()
        q.enqueue("goal-1", "任务", source="L7",
                  score=AttentionScore(0.5, 0.5, 0.5))

        # 直接调 handler，避免事件订阅的时序不确定性
        from kernel.event_bus import Event
        bridge._on_drive_updated(Event(
            topic="L8.drive.updated",
            source="test",
            payload={"drive": "CARE", "active": False, "goal_tags": ["爸爸"]},
        ))

        item = next(i for i in q._items if i.id == "goal-1")
        assert item.boost == 0.0

        await bridge.detach()

    @pytest.mark.asyncio
    async def test_emits_l3_attention_drive_boost_event(self):
        """boost 发生时发送 L3.attention.drive_boost 事件。"""
        bus = EventBus()
        q = AttentionQueue(bus=bus)
        bridge = AttentionBridge(bus=bus, attention_q=q)

        await bridge.attach()
        q.enqueue("goal-1", "帮爸爸", source="L7",
                  score=AttentionScore(0.5, 0.5, 0.5))

        events = []
        bus.subscribe("L3.attention.drive_boost", lambda e: events.append(e))

        # 直接调 handler
        from kernel.event_bus import Event
        bridge._on_drive_updated(Event(
            topic="L8.drive.updated",
            source="test",
            payload={"drive": "CARE", "active": True, "goal_tags": ["爸爸"]},
        ))

        assert len(events) == 1
        assert events[0].payload["drive"] == "CARE"
        assert events[0].payload["boosted_count"] == 1

        await bridge.detach()


class TestBoostTotalScore:
    def test_total_score_includes_boost(self):
        """total_score() = 原始分 - 抢占惩罚 + boost。"""
        bus = EventBus()
        q = AttentionQueue(bus=bus)
        # AttentionScore(0.4, 0.3, 0.2).total() ≈ 0.33
        q.enqueue("goal-1", "任务", source="L7",
                  score=AttentionScore(0.4, 0.3, 0.2), priority=Priority.MEDIUM)
        q.boost("goal-1", extra_score=0.3)

        item = next(i for i in q._items if i.id == "goal-1")
        # total_score = 0.33 - 0 + 0.3 ≈ 0.63
        assert item.total_score() == pytest.approx(0.63, rel=1e-3)

    def test_boost_and_suppress_interact(self):
        """boost 和 suppress 共同影响 total_score。"""
        bus = EventBus()
        q = AttentionQueue(bus=bus)
        # AttentionScore(0.6, 0.3, 0.2).total() ≈ 0.43
        q.enqueue("goal-1", "任务", source="L7",
                  score=AttentionScore(0.6, 0.3, 0.2), priority=Priority.MEDIUM)
        q.boost("goal-1", extra_score=0.2)
        q.suppress("goal-1")  # suppress_count=1

        item = next(i for i in q._items if i.id == "goal-1")
        # total_score = 0.43 - 0.05 + 0.2 = 0.58
        assert item.total_score() == pytest.approx(0.58, rel=1e-3)
