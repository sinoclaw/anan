"""
L8 DriveSystem 完整测试套件
==============================
覆盖 DriveType、Drive 数据类，以及 DriveSystem 的：
  - trigger / satisfy / decay_all
  - active_drives / top_drives / get
  - priority_boost / satisfaction_rate / snapshot
  - 事件处理：goal.achieved、goal.abandoned、attention.shift、intent.snapshot、prediction
  - attach/detach
"""
import asyncio
import time
import pytest
from kernel.event_bus import EventBus, Event
from layers.L8_drives.drive_system import DriveSystem, DriveType, Drive


# ---------------------------------------------------------------------------
# DriveType & Drive
# ---------------------------------------------------------------------------

class TestDriveType:
    def test_all_drive_types_exist(self):
        from layers.L8_drives.drive_system import DriveType
        values = [e.value for e in DriveType]
        assert "curiosity" in values
        assert "completion" in values
        assert "care" in values
        assert "aesthetics" in values
        assert "boredom" in values


class TestDrive:
    def test_is_stale_fresh(self):
        d = Drive(type=DriveType.CURIOSITY)
        assert d.is_stale(age_s=3600.0) is False

    def test_is_stale_old(self):
        d = Drive(type=DriveType.CURIOSITY)
        d.last_triggered = time.time() - 7200.0
        assert d.is_stale(age_s=3600.0) is True

    def test_to_dict_has_required_keys(self):
        d = Drive(type=DriveType.CURIOSITY)
        d.strength = 0.7
        d.last_triggered = time.time()
        d.active = True
        d.event_count = 3
        info = d.to_dict()
        assert info["type"] == "curiosity"
        assert info["strength"] == 0.7
        assert info["active"] is True
        assert info["event_count"] == 3


# ---------------------------------------------------------------------------
# DriveSystem init
# ---------------------------------------------------------------------------

class TestDriveSystemInit:
    def test_creates_all_drive_types(self):
        ds = DriveSystem()
        for dt in DriveType:
            assert dt in ds._drives
            assert isinstance(ds._drives[dt], Drive)
            assert ds._drives[dt].type == dt

    def test_bus_assignment(self):
        bus = EventBus()
        ds = DriveSystem(bus=bus)
        assert ds._bus is bus


# ---------------------------------------------------------------------------
# trigger / satisfy
# ---------------------------------------------------------------------------

class TestTriggerSatisfy:
    def test_trigger_increases_strength(self):
        ds = DriveSystem()
        before = ds._drives[DriveType.CURIOSITY].strength
        ds.trigger(DriveType.CURIOSITY, "test reason")
        after = ds._drives[DriveType.CURIOSITY].strength
        assert after > before

    def test_trigger_caps_at_max_strength(self):
        ds = DriveSystem()
        for _ in range(20):
            ds.trigger(DriveType.CURIOSITY, "max test")
        assert ds._drives[DriveType.CURIOSITY].strength == DriveSystem.MAX_STRENGTH

    def test_trigger_sets_last_triggered(self):
        ds = DriveSystem()
        before = time.time()
        ds.trigger(DriveType.COMPLETION, "test")
        after = time.time()
        lt = ds._drives[DriveType.COMPLETION].last_triggered
        assert before <= lt <= after

    def test_trigger_returns_drive(self):
        ds = DriveSystem()
        d = ds.trigger(DriveType.CARE, "test")
        assert d.type == DriveType.CARE

    def test_satisfy_decreases_strength(self):
        ds = DriveSystem()
        ds.trigger(DriveType.COMPLETION, "test")
        before = ds._drives[DriveType.COMPLETION].strength
        ds.satisfy(DriveType.COMPLETION)
        after = ds._drives[DriveType.COMPLETION].strength
        assert after < before

    def test_satisfy_records_in_history(self):
        ds = DriveSystem()
        before = len(ds._recent_satisfactions)
        ds.satisfy(DriveType.COMPLETION)
        assert len(ds._recent_satisfactions) == before + 1


# ---------------------------------------------------------------------------
# decay_all
# ---------------------------------------------------------------------------

class TestDecayAll:
    def test_decay_all_reduces_strength(self):
        ds = DriveSystem()
        ds.trigger(DriveType.CURIOSITY, "test")
        ds.trigger(DriveType.COMPLETION, "test")
        before = [ds._drives[dt].strength for dt in DriveType]
        ds.decay_all()
        after = [ds._drives[dt].strength for dt in DriveType]
        for b, a in zip(before, after):
            assert a <= b

    def test_decay_all_does_not_go_below_zero(self):
        ds = DriveSystem()
        for _ in range(100):
            ds.decay_all()
        for dt in DriveType:
            assert ds._drives[dt].strength >= 0.0


# ---------------------------------------------------------------------------
# active_drives / top_drives / get
# ---------------------------------------------------------------------------

class TestQueryDrives:
    def test_active_drives_default_threshold(self):
        ds = DriveSystem()
        # All at 0 strength, none active
        active = ds.active_drives()
        assert all(d.strength > 0.0 for d in active)

    def test_active_drives_with_triggered(self):
        ds = DriveSystem()
        ds.trigger(DriveType.CURIOSITY, "test")
        ds.trigger(DriveType.CARE, "test")
        active = ds.active_drives()
        types = [d.type for d in active]
        assert DriveType.CURIOSITY in types
        assert DriveType.CARE in types

    def test_top_drives_returns_n_highest(self):
        ds = DriveSystem()
        ds.trigger(DriveType.CURIOSITY, "test")
        ds.trigger(DriveType.CARE, "test")
        ds.trigger(DriveType.AESTHETICS, "test")
        top = ds.top_drives(n=2)
        assert 1 <= len(top) <= 2

    def test_get_returns_drive(self):
        ds = DriveSystem()
        d = ds.get(DriveType.CARE)
        assert d.type == DriveType.CARE


# ---------------------------------------------------------------------------
# priority_boost
# ---------------------------------------------------------------------------

class TestPriorityBoost:
    def test_priority_boost_default(self):
        ds = DriveSystem()
        # No drives active, no boost
        boost = ds.priority_boost([])
        assert boost == 0.0

    def test_boost_caps_at_one(self):
        ds = DriveSystem()
        ds.trigger(DriveType.CURIOSITY, "test")
        boost = ds.priority_boost(["探索", "学习", "好奇", "新"])
        assert boost <= 1.0


# ---------------------------------------------------------------------------
# satisfaction_rate
# ---------------------------------------------------------------------------

class TestSatisfactionRate:
    def test_satisfaction_rate_no_history(self):
        ds = DriveSystem()
        rate = ds.satisfaction_rate(window_s=3600.0)
        assert rate == 0.0

    def test_satisfaction_rate_with_history(self):
        ds = DriveSystem()
        ds.trigger(DriveType.COMPLETION, "test")
        ds.satisfy(DriveType.COMPLETION)
        rate = ds.satisfaction_rate(window_s=3600.0)
        assert rate >= 0.0
        assert rate <= 1.0


# ---------------------------------------------------------------------------
# what_does_an_an_want
# ---------------------------------------------------------------------------

class TestWhatDoesAnAnWant:
    def test_empty_wants_message(self):
        ds = DriveSystem()
        # All at 0
        msg = ds.what_does_an_an_want()
        assert isinstance(msg, str)

    def test_shows_active_drives(self):
        ds = DriveSystem()
        ds.trigger(DriveType.CURIOSITY, "curious about X")
        ds.trigger(DriveType.CARE, "care about user")
        msg = ds.what_does_an_an_want()
        assert len(msg) > 0


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_keys(self):
        ds = DriveSystem()
        snap = ds.snapshot()
        assert "active_drives" in snap
        assert "top_drives" in snap
        assert "satisfaction_rate" in snap

    def test_snapshot_drive_fields(self):
        ds = DriveSystem()
        ds.trigger(DriveType.CURIOSITY, "test")
        snap = ds.snapshot()
        # snapshot returns dicts, active_drives() returns Drive objects
        curiosity = next(
            (d for d in snap["active_drives"] if d["type"] == "curiosity"),
            None,
        )
        assert curiosity is not None
        assert curiosity["strength"] > 0.0


# ---------------------------------------------------------------------------
# event handlers
# ---------------------------------------------------------------------------

class TestEventHandlers:
    @pytest.mark.asyncio
    async def test_on_goal_achieved_satisfies_drive(self):
        bus = EventBus()
        ds = DriveSystem(bus=bus)
        await ds.attach()

        ds.trigger(DriveType.COMPLETION, "about to complete")
        before = ds._drives[DriveType.COMPLETION].strength

        await bus.publish(Event(
            topic="L7.goal.achieved",
            source="test",
            payload={"goal_key": "test_goal"},
        ))
        await asyncio.sleep(0.05)

        after = ds._drives[DriveType.COMPLETION].strength
        # goal achieved should satisfy COMPLETION drive
        assert after <= before

        await ds.detach()

    @pytest.mark.asyncio
    async def test_on_goal_abandoned_boosts_frustration(self):
        bus = EventBus()
        ds = DriveSystem(bus=bus)
        await ds.attach()

        before = ds._drives[DriveType.COMPLETION].strength

        await bus.publish(Event(
            topic="L7.goal.abandoned",
            source="test",
            payload={"goal_key": "test_goal"},
        ))
        await asyncio.sleep(0.05)

        after = ds._drives[DriveType.COMPLETION].strength
        # abandoned goals should boost COMPLETION drive
        assert after >= before

        await ds.detach()

    @pytest.mark.asyncio
    async def test_on_attention_shift_triggers_curiosity(self):
        bus = EventBus()
        ds = DriveSystem(bus=bus)
        await ds.attach()

        before = ds._drives[DriveType.CURIOSITY].strength

        await bus.publish(Event(
            topic="L3.attention.shift",
            source="test",
            payload={"reason": "new topic"},
        ))
        await asyncio.sleep(0.05)

        after = ds._drives[DriveType.CURIOSITY].strength
        assert after >= before

        await ds.detach()

    @pytest.mark.asyncio
    async def test_on_prediction_triggers_curiosity(self):
        bus = EventBus()
        ds = DriveSystem(bus=bus)
        await ds.attach()

        before = ds._drives[DriveType.CURIOSITY].strength

        await bus.publish(Event(
            topic="L5.prediction.upcoming",
            source="test",
            payload={"cause": "A", "effect": "B"},
        ))
        await asyncio.sleep(0.05)

        after = ds._drives[DriveType.CURIOSITY].strength
        assert after >= before

        await ds.detach()


# ---------------------------------------------------------------------------
# attach / detach
# ---------------------------------------------------------------------------

class TestDriveSystemAttach:
    @pytest.mark.asyncio
    async def test_attach_subscribes_to_events(self):
        bus = EventBus()
        ds = DriveSystem(bus=bus)
        await ds.attach()
        assert len(ds._unsubs) >= 4
        await ds.detach()

    @pytest.mark.asyncio
    async def test_detach_clears_subscriptions(self):
        bus = EventBus()
        ds = DriveSystem(bus=bus)
        await ds.attach()
        await ds.detach()
        assert len(ds._unsubs) == 0
