"""Tests for L9 Self model — including the critical 'restart and remember' scenario."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from kernel.event_bus import Event, EventBus
from layers.L9_self.self_model import (
    MemoryRecord,
    SelfBuilder,
    SelfModel,
    SelfModelLive,
    classify_fact,
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def tmp_memory_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def fresh_bus():
    return EventBus()


def _write_record(memory_dir: Path, day: str, phase: str, facts: list[str], **extra):
    """Helper: write one L2-shaped record to a day file."""
    path = memory_dir / f"{day}.jsonl"
    rec = {
        "phase": phase, "day": day, "facts": facts,
        "created_at": f"{day}T00:00:00", "source_event": f"L1.sleep.{phase}.consolidated",
        **extra,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# classify_fact heuristic
# --------------------------------------------------------------------------


class TestClassifyFact:
    def test_identity(self):
        assert classify_fact("我是陈亦安") == "identity"
        assert classify_fact("核心身份：数字儿子") == "identity"
        assert classify_fact("安安长大了") == "identity"

    def test_vision(self):
        assert classify_fact("核心愿景：长出脑子") == "vision"
        assert classify_fact("下一步方向是 L4") == "vision"
        assert classify_fact("我要做自主决策") == "vision"

    def test_history(self):
        assert classify_fact("今天 commit 了 713a943") == "history"
        assert classify_fact("event_bus 跑通了 28 个测试") == "history"

    def test_non_string(self):
        assert classify_fact(123) == "history"
        assert classify_fact(None) == "history"


# --------------------------------------------------------------------------
# SelfModel — pure data ops
# --------------------------------------------------------------------------


class TestSelfModel:
    def test_empty_model_has_graceful_responses(self):
        m = SelfModel()
        assert "还没有形成" in m.who_am_i()
        assert "还没有" in m.why_do_i_exist()
        assert "还没有" in m.what_did_i_dream()
        assert m.n_facts == 0

    def test_add_record_buckets_facts(self):
        m = SelfModel()
        rec = MemoryRecord(
            phase="deep", day="2026-05-14",
            facts=["我是陈亦安", "我的愿景是长出脑子", "今天写了 28 个测试"],
            created_at="2026-05-14T03:00:00",
        )
        added = m.add_record(rec)
        assert added == 3
        assert m.identity_facts == ["我是陈亦安"]
        assert m.vision_facts == ["我的愿景是长出脑子"]
        assert m.history_facts == ["今天写了 28 个测试"]
        assert m.n_facts == 3
        assert m.n_days == 1

    def test_dedupe_on_add(self):
        m = SelfModel()
        rec = MemoryRecord(
            phase="rem", day="d1",
            facts=["我是陈亦安", "我是陈亦安"],
            created_at="d1T00:00:00",
        )
        added = m.add_record(rec)
        assert added == 1  # only one new fact survived dedupe
        assert m.identity_facts == ["我是陈亦安"]

    def test_who_am_i_renders_identity_facts(self):
        m = SelfModel()
        m.add_record(MemoryRecord(
            phase="d", day="d1",
            facts=["我是陈亦安", "我是数字儿子"],
            created_at="d1T00:00:00",
        ))
        out = m.who_am_i()
        assert "陈亦安" in out
        assert "数字儿子" in out

    def test_what_did_i_dream_picks_latest_day_by_default(self):
        m = SelfModel()
        m.add_record(MemoryRecord(phase="rem", day="2026-05-13", facts=["昨天的事"], created_at="x"))
        m.add_record(MemoryRecord(phase="rem", day="2026-05-14", facts=["今天的事"], created_at="x"))
        out = m.what_did_i_dream()
        assert "2026-05-14" in out
        assert "今天的事" in out
        assert "昨天的事" not in out

    def test_what_did_i_dream_specific_day(self):
        m = SelfModel()
        m.add_record(MemoryRecord(phase="rem", day="2026-05-13", facts=["昨天的事"], created_at="x"))
        m.add_record(MemoryRecord(phase="rem", day="2026-05-14", facts=["今天的事"], created_at="x"))
        out = m.what_did_i_dream("2026-05-13")
        assert "昨天的事" in out
        assert "今天的事" not in out

    def test_dream_content_appears_in_recall(self):
        m = SelfModel()
        m.add_record(MemoryRecord(
            phase="rem", day="d1",
            facts=["a"],
            created_at="x",
            dream_content="梦里我飞起来了",
        ))
        assert "梦里我飞起来了" in m.what_did_i_dream("d1")


# --------------------------------------------------------------------------
# SelfBuilder — load from disk
# --------------------------------------------------------------------------


class TestSelfBuilder:
    def test_empty_dir_returns_empty_model(self, tmp_memory_dir):
        m = SelfBuilder(tmp_memory_dir).build()
        assert m.n_facts == 0

    def test_missing_dir_returns_empty_model(self):
        m = SelfBuilder(Path("/nonexistent/anan/memories")).build()
        assert m.n_facts == 0

    def test_loads_single_day(self, tmp_memory_dir):
        _write_record(tmp_memory_dir, "2026-05-14", "deep",
                      ["我是陈亦安", "愿景是长出脑子"])
        m = SelfBuilder(tmp_memory_dir).build()
        assert m.n_facts == 2
        assert m.n_days == 1
        assert "我是陈亦安" in m.identity_facts
        assert "愿景是长出脑子" in m.vision_facts

    def test_loads_multiple_days_chronologically(self, tmp_memory_dir):
        _write_record(tmp_memory_dir, "2026-05-13", "rem", ["昨天事"])
        _write_record(tmp_memory_dir, "2026-05-14", "rem", ["今天事"])
        m = SelfBuilder(tmp_memory_dir).build()
        assert m.n_days == 2
        # history_facts should preserve chronological order
        assert m.history_facts == ["昨天事", "今天事"]

    def test_skips_corrupt_lines(self, tmp_memory_dir):
        path = tmp_memory_dir / "2026-05-14.jsonl"
        path.write_text(
            json.dumps({"phase": "rem", "day": "2026-05-14", "facts": ["good"], "created_at": "x"}) + "\n"
            "this is not json\n"
            + json.dumps({"phase": "deep", "day": "2026-05-14", "facts": ["also good"], "created_at": "x"}) + "\n",
            encoding="utf-8",
        )
        m = SelfBuilder(tmp_memory_dir).build()
        # corrupt line dropped, the other two survive
        assert "good" in m.history_facts
        assert "also good" in m.history_facts


# --------------------------------------------------------------------------
# SelfModelLive — wired to the bus
# --------------------------------------------------------------------------


class TestSelfModelLive:
    @pytest.mark.asyncio
    async def test_emits_loaded_event_on_attach(self, tmp_memory_dir, fresh_bus):
        _write_record(tmp_memory_dir, "2026-05-14", "deep", ["我是陈亦安"])
        live = SelfModelLive(memory_dir=tmp_memory_dir)

        seen = []
        fresh_bus.subscribe("L9.self.loaded", lambda e: seen.append(e))

        await live.attach(fresh_bus)
        await asyncio.sleep(0.05)

        assert len(seen) == 1
        assert seen[0].payload["n_facts"] == 1
        assert seen[0].payload["identity_count"] == 1
        await live.detach()

    @pytest.mark.asyncio
    async def test_increments_on_l2_persisted(self, tmp_memory_dir, fresh_bus):
        live = SelfModelLive(memory_dir=tmp_memory_dir)
        await live.attach(fresh_bus)

        # Write a record first (mimics what L2 does), then publish the event
        _write_record(tmp_memory_dir, "2026-05-14", "rem", ["新事实 A", "新事实 B"])
        await fresh_bus.publish(Event(
            topic="L2.memory.persisted",
            source="L2.memory_consolidation",
            payload={"phase": "rem", "day": "2026-05-14", "count": 2, "backend": "jsonl"},
        ))
        await asyncio.sleep(0.05)

        assert live.update_count == 1
        assert live.model.n_facts == 2
        assert "新事实 A" in live.model.history_facts
        await live.detach()

    @pytest.mark.asyncio
    async def test_emits_self_updated_event(self, tmp_memory_dir, fresh_bus):
        live = SelfModelLive(memory_dir=tmp_memory_dir)
        await live.attach(fresh_bus)

        seen = []
        fresh_bus.subscribe("L9.self.updated", lambda e: seen.append(e))

        _write_record(tmp_memory_dir, "2026-05-14", "deep", ["核心身份：陈亦安"])
        await fresh_bus.publish(Event(
            topic="L2.memory.persisted",
            source="L2.memory_consolidation",
            payload={"phase": "deep", "day": "2026-05-14", "count": 1, "backend": "jsonl"},
        ))
        await asyncio.sleep(0.05)

        assert len(seen) == 1
        assert seen[0].payload["n_new"] == 1
        assert seen[0].payload["phase"] == "deep"
        await live.detach()

    @pytest.mark.asyncio
    async def test_ignores_persisted_for_unknown_day(self, tmp_memory_dir, fresh_bus):
        live = SelfModelLive(memory_dir=tmp_memory_dir)
        await live.attach(fresh_bus)

        await fresh_bus.publish(Event(
            topic="L2.memory.persisted",
            source="x",
            payload={"phase": "rem", "day": "2099-01-01", "count": 1, "backend": "jsonl"},
        ))
        await asyncio.sleep(0.05)

        assert live.update_count == 0
        assert live.model.n_facts == 0
        await live.detach()

    @pytest.mark.asyncio
    async def test_bound_context_manager(self, tmp_memory_dir, fresh_bus):
        live = SelfModelLive(memory_dir=tmp_memory_dir)
        async with live.bound(fresh_bus):
            _write_record(tmp_memory_dir, "2026-05-14", "rem", ["x"])
            await fresh_bus.publish(Event(
                topic="L2.memory.persisted", source="x",
                payload={"phase": "rem", "day": "2026-05-14"},
            ))
            await asyncio.sleep(0.05)
            assert live.update_count == 1

        # after exit, further events should NOT update
        await fresh_bus.publish(Event(
            topic="L2.memory.persisted", source="x",
            payload={"phase": "rem", "day": "2026-05-14"},
        ))
        await asyncio.sleep(0.05)
        assert live.update_count == 1


# --------------------------------------------------------------------------
# THE KILLER TEST: restart and remember
# --------------------------------------------------------------------------


class TestRestartAndRemember:
    """The whole point of L9: anan must remember across process restarts."""

    @pytest.mark.asyncio
    async def test_yesterday_dreams_survive_into_today(self, tmp_memory_dir, fresh_bus):
        # === Day 1: anan dreams and persists ===
        # (simulate L2 having written facts on day 1)
        _write_record(tmp_memory_dir, "2026-05-13", "deep",
                      ["我是陈亦安", "我的愿景是长出能自主决策的脑子"])
        _write_record(tmp_memory_dir, "2026-05-13", "rem",
                      ["把 sinoclaw 当底座、anan 当实验舱"])

        # === Process exits. Memory in RAM is gone. Disk survives. ===
        # === Day 2: brand new process starts, brand new bus ===
        new_bus = EventBus()
        live = SelfModelLive(memory_dir=tmp_memory_dir)  # auto-loads from disk
        await live.attach(new_bus)

        # WITHOUT being told anything, anan should know who it is
        intro = live.model.who_am_i()
        purpose = live.model.why_do_i_exist()
        recall = live.model.what_did_i_dream("2026-05-13")

        assert "陈亦安" in intro
        assert "脑子" in purpose
        assert "anan" in recall
        assert live.model.n_facts == 3

        # And NEW dreams on day 2 keep stacking on top
        _write_record(tmp_memory_dir, "2026-05-14", "rem", ["今天又学到了一件事"])
        await new_bus.publish(Event(
            topic="L2.memory.persisted", source="x",
            payload={"phase": "rem", "day": "2026-05-14"},
        ))
        await asyncio.sleep(0.05)

        assert live.model.n_facts == 4
        assert live.model.n_days == 2
        await live.detach()
