"""Tests for adapters.memory_consolidation."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from kernel.event_bus import Event, EventBus
from adapters.memory_consolidation import (
    JSONLBackend,
    MemoryConsolidationAdapter,
    AnanProviderBackend,
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def fresh_bus():
    return EventBus()


# --------------------------------------------------------------------------
# JSONLBackend
# --------------------------------------------------------------------------


class TestJSONLBackend:
    def test_creates_base_dir(self, tmpdir_path):
        target = tmpdir_path / "nested" / "memories"
        b = JSONLBackend(base_dir=target)
        assert target.exists()
        assert b.name == "jsonl"

    def test_writes_record_to_day_file(self, tmpdir_path):
        b = JSONLBackend(base_dir=tmpdir_path)
        record = {"day": "2026-05-14", "phase": "rem", "facts": ["a", "b"]}
        ref = b.write(record)
        assert ref is not None
        path = Path(ref)
        assert path.exists()
        line = path.read_text(encoding="utf-8").strip()
        assert json.loads(line) == record

    def test_appends_multiple_records_same_day(self, tmpdir_path):
        b = JSONLBackend(base_dir=tmpdir_path)
        b.write({"day": "2026-05-14", "facts": ["a"]})
        b.write({"day": "2026-05-14", "facts": ["b"]})
        path = tmpdir_path / "2026-05-14.jsonl"
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2

    def test_falls_back_to_today_when_day_missing(self, tmpdir_path):
        b = JSONLBackend(base_dir=tmpdir_path)
        ref = b.write({"facts": ["x"]})
        assert ref is not None
        # filename should be a YYYY-MM-DD.jsonl pattern
        name = Path(ref).name
        assert name.endswith(".jsonl") and len(name) == len("YYYY-MM-DD.jsonl")


# --------------------------------------------------------------------------
# AnanProviderBackend
# --------------------------------------------------------------------------


class _FakeProvider:
    """Minimal stand-in for a anan MemoryProvider — captures writes."""

    def __init__(self, name="fake", *, raise_on_write=False):
        self.name = name
        self.writes: list[dict] = []
        self._raise = raise_on_write

    def on_memory_write(self, action, target, content, metadata=None):
        if self._raise:
            raise RuntimeError("boom")
        self.writes.append(
            {"action": action, "target": target, "content": content, "metadata": metadata}
        )


class TestAnanProviderBackend:
    def test_forwards_string_facts_one_by_one(self):
        provider = _FakeProvider("honcho-fake")
        b = AnanProviderBackend(provider=provider)
        ref = b.write({"phase": "rem", "day": "2026-05-14", "facts": ["fact1", "fact2"]})
        assert ref == "honcho-fake:2"
        assert len(provider.writes) == 2
        assert provider.writes[0]["content"] == "fact1"
        assert provider.writes[0]["metadata"] == {
            "source": "anan.L1.sleep", "phase": "rem", "day": "2026-05-14",
        }

    def test_serializes_dict_facts_as_json(self):
        provider = _FakeProvider()
        b = AnanProviderBackend(provider=provider)
        b.write({"phase": "deep", "facts": [{"k": "v", "n": 1}]})
        assert provider.writes[0]["content"] == '{"k": "v", "n": 1}'

    def test_returns_none_when_no_facts(self):
        provider = _FakeProvider()
        b = AnanProviderBackend(provider=provider)
        assert b.write({"facts": []}) is None
        assert b.write({}) is None
        assert provider.writes == []

    def test_swallows_provider_errors(self):
        provider = _FakeProvider(raise_on_write=True)
        b = AnanProviderBackend(provider=provider)
        # MUST NOT raise — we'd rather lose a memory than crash the agent
        ref = b.write({"facts": ["x"]})
        assert ref is None


# --------------------------------------------------------------------------
# MemoryConsolidationAdapter — end-to-end via real EventBus
# --------------------------------------------------------------------------


class TestMemoryConsolidationAdapter:
    @pytest.mark.asyncio
    async def test_persists_consolidated_event(self, tmpdir_path, fresh_bus):
        backend = JSONLBackend(base_dir=tmpdir_path)
        adapter = MemoryConsolidationAdapter(backend=backend)
        await adapter.attach(fresh_bus)

        await fresh_bus.publish(Event(
            topic="L1.sleep.rem.consolidated",
            source="test",
            payload={
                "phase": "rem",
                "day": "2026-05-14",
                "consolidated_facts": ["learned X", "noticed Y"],
                "duration_s": 0.123,
            },
        ))
        # let the bus drain
        await asyncio.sleep(0.05)

        assert adapter.persisted_count == 1
        path = tmpdir_path / "2026-05-14.jsonl"
        assert path.exists()
        rec = json.loads(path.read_text(encoding="utf-8").strip())
        assert rec["phase"] == "rem"
        assert rec["facts"] == ["learned X", "noticed Y"]
        assert rec["source_event"] == "L1.sleep.rem.consolidated"
        assert "created_at" in rec

        await adapter.detach()

    @pytest.mark.asyncio
    async def test_ignores_non_consolidated_events(self, tmpdir_path, fresh_bus):
        adapter = MemoryConsolidationAdapter(backend=JSONLBackend(base_dir=tmpdir_path))
        await adapter.attach(fresh_bus)

        await fresh_bus.publish(Event(
            topic="L1.sleep.rem.start",
            source="test",
            payload={"phase": "rem", "day": "2026-05-14"},
        ))
        await asyncio.sleep(0.05)

        assert adapter.persisted_count == 0
        assert list(tmpdir_path.iterdir()) == []
        await adapter.detach()

    @pytest.mark.asyncio
    async def test_skips_empty_facts(self, tmpdir_path, fresh_bus):
        adapter = MemoryConsolidationAdapter(backend=JSONLBackend(base_dir=tmpdir_path))
        await adapter.attach(fresh_bus)

        await fresh_bus.publish(Event(
            topic="L1.sleep.light.consolidated",
            source="test",
            payload={"phase": "light", "day": "2026-05-14", "consolidated_facts": []},
        ))
        await asyncio.sleep(0.05)

        assert adapter.persisted_count == 0
        assert adapter.skipped_empty == 1
        await adapter.detach()

    @pytest.mark.asyncio
    async def test_emits_l2_persisted_event(self, tmpdir_path, fresh_bus):
        adapter = MemoryConsolidationAdapter(backend=JSONLBackend(base_dir=tmpdir_path))
        await adapter.attach(fresh_bus)

        seen: list[Event] = []
        unsub = fresh_bus.subscribe("L2.memory.persisted", lambda e: seen.append(e))

        await fresh_bus.publish(Event(
            topic="L1.sleep.deep.consolidated",
            source="test",
            payload={
                "phase": "deep", "day": "2026-05-14",
                "consolidated_facts": ["a", "b", "c"],
            },
        ))
        await asyncio.sleep(0.05)

        assert len(seen) == 1
        assert seen[0].payload["phase"] == "deep"
        assert seen[0].payload["count"] == 3
        assert seen[0].payload["backend"] == "jsonl"

        unsub()
        await adapter.detach()

    @pytest.mark.asyncio
    async def test_only_phases_filter(self, tmpdir_path, fresh_bus):
        adapter = MemoryConsolidationAdapter(
            backend=JSONLBackend(base_dir=tmpdir_path),
            only_phases={"deep"},
        )
        await adapter.attach(fresh_bus)

        for phase in ("light", "rem", "deep"):
            await fresh_bus.publish(Event(
                topic=f"L1.sleep.{phase}.consolidated",
                source="test",
                payload={"phase": phase, "day": "d", "consolidated_facts": ["x"]},
            ))
        await asyncio.sleep(0.05)

        # only the deep one should have persisted
        assert adapter.persisted_count == 1
        await adapter.detach()

    @pytest.mark.asyncio
    async def test_bound_context_manager(self, tmpdir_path, fresh_bus):
        adapter = MemoryConsolidationAdapter(backend=JSONLBackend(base_dir=tmpdir_path))

        async with adapter.bound(fresh_bus):
            await fresh_bus.publish(Event(
                topic="L1.sleep.rem.consolidated",
                source="test",
                payload={"phase": "rem", "day": "d", "consolidated_facts": ["x"]},
            ))
            await asyncio.sleep(0.05)
            assert adapter.persisted_count == 1

        # after exit, further events should NOT be persisted
        await fresh_bus.publish(Event(
            topic="L1.sleep.rem.consolidated",
            source="test",
            payload={"phase": "rem", "day": "d", "consolidated_facts": ["y"]},
        ))
        await asyncio.sleep(0.05)
        assert adapter.persisted_count == 1  # unchanged
