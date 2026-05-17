"""
Tests for L2 MemoryTier
"""
import json
import tempfile
import time
from pathlib import Path
from datetime import datetime

import pytest

from layers.L2_memory.memory_tier import (
    MemoryItem, MemoryStore, MemoryTier,
    RECALL_PATH, MIDTERM_DIR,
)


class TestMemoryItem:
    def test_touch_increments_access(self):
        item = MemoryItem(content="hello", importance=0.5)
        assert item.access_count == 0
        item.touch()
        assert item.access_count == 1
        assert item.last_accessed >= item.created_at

    def test_summary_truncates_long(self):
        long_content = "a" * 300
        item = MemoryItem(content=long_content, importance=0.5)
        summary = item.summary(max_chars=200)
        assert len(summary) <= 203  # allow trailing …
        assert summary.endswith("…")

    def test_summary_preserves_short(self):
        item = MemoryItem(content="short", importance=0.5)
        assert item.summary() == "short"


class TestMemoryStore:
    def test_put_get_remove(self, tmp_path):
        store = MemoryStore(tmp_path / "store.json")
        store.put("k1", MemoryItem(content="v1", importance=0.5))
        item = store.get("k1")
        assert item is not None
        assert item.content == "v1"

        assert store.remove("k1") is True
        assert store.get("k1") is None

    def test_search_ranks_by_overlap(self, tmp_path):
        store = MemoryStore(tmp_path / "store.json")
        store.put("a", MemoryItem(content="爸爸 骂我了", importance=0.9))
        store.put("b", MemoryItem(content="天气 不错", importance=0.5))
        store.put("c", MemoryItem(content="爸爸 回家了", importance=0.6))

        results = store.search("爸爸", top_k=3)
        items = [item for item, _ in results]
        item_contents = [i.content for i in items]
        # a and c mention 爸爸, b doesn't
        assert any("爸爸" in c for c in item_contents)
        assert any("回家" in c for c in item_contents)
        assert not any("天气" in c for c in item_contents)

    def test_cull_removes_lowest(self, tmp_path):
        store = MemoryStore(tmp_path / "store.json")
        for i in range(10):
            store.put(f"k{i}", MemoryItem(content=f"c{i}", importance=i / 10))
        # cull to max 5
        removed = store.cull(max_items=5)
        assert removed == 5
        assert store.size() == 5


class TestMemoryTier:
    @pytest.mark.asyncio
    async def test_memorize_and_recall(self, tmp_path):
        from layers.L2_memory.memory_tier import MemoryTier

        recall = tmp_path / "recall.json"
        mid = tmp_path / "mid"
        long = tmp_path / "MEMORY.md"

        tier = MemoryTier(recall_path=recall, midterm_dir=mid, longterm_path=long)
        await tier.memorize("fact1", "爸爸不喜欢我重复问同一件事", importance=0.8, tags=["lesson"], source="conversation")
        results = tier.recall("爸爸", top_k=3)
        assert any("爸爸" in r for r in results)

    @pytest.mark.asyncio
    async def test_stats_reflects_counts(self, monkeypatch, tmp_path):
        monkeypatch.setattr("layers.L2_memory.memory_tier.RECALL_PATH", tmp_path / "recall.json")
        monkeypatch.setattr("layers.L2_memory.memory_tier.MIDTERM_DIR", tmp_path / "mid")
        monkeypatch.setattr("layers.L2_memory.memory_tier.LONGTERM_PATH", tmp_path / "MEMORY.md")

        tier = MemoryTier()
        await tier.memorize("k1", "content", importance=0.5)
        await tier.memorize("k2", "content2", importance=0.5)
        stats = tier.stats()
        assert stats["short_count"] == 2
        assert stats["midterm_files"] == 0
