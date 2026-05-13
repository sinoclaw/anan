"""Tests for L7 self-regulator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from kernel.event_bus import Event, EventBus
from layers.L3_working_memory import WorkingMemory
from layers.L7_will import SelfRegulator


@pytest.fixture
def fresh_bus():
    return EventBus()


@dataclass
class FakeConfig:
    sleep_threshold: float = 4.0


@dataclass
class FakeCircadian:
    config: FakeConfig = None
    def __post_init__(self):
        if self.config is None:
            self.config = FakeConfig()


async def _send_warn(bus: EventBus, issues: list[str]):
    await bus.publish(Event(
        topic="L6.metacognition.warn", source="test",
        payload={"score": 0.4, "issues": issues, "suggestions": []},
    ))


class TestBusErrorAdaptation:
    @pytest.mark.asyncio
    async def test_high_error_emits_heal_intent(self, fresh_bus):
        l7 = SelfRegulator(bus=fresh_bus)
        await l7.attach()
        captured = []
        fresh_bus.subscribe("L7.regulator.acted", lambda e: captured.append(e))

        await _send_warn(fresh_bus, ["事件总线错误率 8.0% 严重"])
        await asyncio.sleep(0.02)

        assert len(captured) == 1
        assert captured[0].payload["action"] == "emit_heal_intent"
        await l7.detach()


class TestAttentionRebalancing:
    @pytest.mark.asyncio
    async def test_skewed_layer_attenuated(self, fresh_bus):
        wm = WorkingMemory(capacity=10)
        await wm.attach(fresh_bus)
        l7 = SelfRegulator(bus=fresh_bus, working_memory=wm)
        await l7.attach()

        await _send_warn(fresh_bus, ["注意力倾斜：L9 层占了 90% (9/10)"])
        await asyncio.sleep(0.02)

        # L9 should now be attenuated to 0.3 of original
        assert "L9" in l7._layer_atten
        assert l7._layer_atten["L9"] == pytest.approx(0.3)

        # And the wrapped salience_fn should reflect that
        l9_event = Event(topic="L9.self.updated", source="t", payload={})
        original = 0.95   # default_salience for L9
        scored = wm.salience_fn(l9_event)
        assert scored == pytest.approx(original * 0.3, rel=0.01)

        await l7.detach()
        await wm.detach()

    @pytest.mark.asyncio
    async def test_repeated_skew_compounds_attenuation(self, fresh_bus):
        wm = WorkingMemory(capacity=10)
        await wm.attach(fresh_bus)
        l7 = SelfRegulator(bus=fresh_bus, working_memory=wm,
                           salience_attenuation=0.5)
        await l7.attach()

        await _send_warn(fresh_bus, ["注意力倾斜：L9 层占了 90%"])
        await asyncio.sleep(0.02)
        assert l7._layer_atten["L9"] == pytest.approx(0.5)

        await _send_warn(fresh_bus, ["注意力倾斜：L9 层占了 80%"])
        await asyncio.sleep(0.02)
        assert l7._layer_atten["L9"] == pytest.approx(0.25)

        await l7.detach()
        await wm.detach()

    @pytest.mark.asyncio
    async def test_attenuation_floored_at_5pct(self, fresh_bus):
        wm = WorkingMemory(capacity=10)
        await wm.attach(fresh_bus)
        l7 = SelfRegulator(bus=fresh_bus, working_memory=wm,
                           salience_attenuation=0.01)
        await l7.attach()

        # Many skews — would dive to ~0 without floor
        for _ in range(5):
            await _send_warn(fresh_bus, ["注意力倾斜：L1 层占了 90%"])
            await asyncio.sleep(0.01)

        assert l7._layer_atten["L1"] >= 0.05

        await l7.detach()
        await wm.detach()


class TestIdentityStirring:
    @pytest.mark.asyncio
    async def test_stagnation_shortens_threshold(self, fresh_bus):
        circ = FakeCircadian()
        l7 = SelfRegulator(bus=fresh_bus, circadian=circ, threshold_step=1.0)
        await l7.attach()

        original = circ.config.sleep_threshold
        await _send_warn(fresh_bus, ["身份事实已经 5 个周期没增长"])
        await asyncio.sleep(0.02)

        assert circ.config.sleep_threshold == original - 1.0
        await l7.detach()

    @pytest.mark.asyncio
    async def test_threshold_floored(self, fresh_bus):
        circ = FakeCircadian(config=FakeConfig(sleep_threshold=1.5))
        l7 = SelfRegulator(bus=fresh_bus, circadian=circ,
                           threshold_step=1.0, min_sleep_threshold=1.0)
        await l7.attach()

        # First step: 1.5 → 1.0
        await _send_warn(fresh_bus, ["身份事实已经 5 个周期没增长"])
        await asyncio.sleep(0.02)
        assert circ.config.sleep_threshold == 1.0

        # Second step: would go below floor, becomes noop
        await _send_warn(fresh_bus, ["身份事实已经 5 个周期没增长"])
        await asyncio.sleep(0.02)
        assert circ.config.sleep_threshold == 1.0  # unchanged
        # Last action recorded as noop
        assert l7.latest().action == "noop"
        await l7.detach()

    @pytest.mark.asyncio
    async def test_no_circadian_records_noop(self, fresh_bus):
        l7 = SelfRegulator(bus=fresh_bus)  # no circadian
        await l7.attach()
        await _send_warn(fresh_bus, ["身份事实已经 5 个周期没增长"])
        await asyncio.sleep(0.02)
        assert l7.latest().action == "noop"
        await l7.detach()


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_max_actions_per_warn(self, fresh_bus):
        wm = WorkingMemory(capacity=10)
        await wm.attach(fresh_bus)
        circ = FakeCircadian()
        l7 = SelfRegulator(bus=fresh_bus, working_memory=wm,
                           circadian=circ, max_actions_per_warn=2)
        await l7.attach()

        # 3 issues in one warn — only first 2 should fire
        await _send_warn(fresh_bus, [
            "事件总线错误率 9.0% 严重",
            "注意力倾斜：L9 层占了 95%",
            "身份事实已经 5 个周期没增长",
        ])
        await asyncio.sleep(0.02)
        # 3 issues, but capped at 2 actions
        assert len(l7.history()) == 2
        await l7.detach()
        await wm.detach()


class TestStatsAndHistory:
    @pytest.mark.asyncio
    async def test_history_and_stats(self, fresh_bus):
        wm = WorkingMemory(capacity=10)
        await wm.attach(fresh_bus)
        l7 = SelfRegulator(bus=fresh_bus, working_memory=wm)
        await l7.attach()

        await _send_warn(fresh_bus, ["注意力倾斜：L9 层占了 90%"])
        await _send_warn(fresh_bus, ["注意力倾斜：L1 层占了 80%"])
        await asyncio.sleep(0.02)

        s = l7.stats()
        assert s["total_adaptations"] == 2
        assert s["by_action"]["attenuate_layer_salience"] == 2
        assert "L9" in s["layer_attenuations"]
        assert "L1" in s["layer_attenuations"]
        await l7.detach()
        await wm.detach()


class TestDetachRestores:
    @pytest.mark.asyncio
    async def test_detach_restores_original_salience_fn(self, fresh_bus):
        wm = WorkingMemory(capacity=10)
        await wm.attach(fresh_bus)
        original = wm.salience_fn
        l7 = SelfRegulator(bus=fresh_bus, working_memory=wm)
        await l7.attach()
        # salience_fn was wrapped
        assert wm.salience_fn is not original
        await l7.detach()
        # restored
        assert wm.salience_fn is original
        await wm.detach()
