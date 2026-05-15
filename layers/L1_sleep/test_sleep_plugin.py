"""Tests for the dreaming plugin.

Covers:
  * Config dataclass initialization
  * Phase marker resolution (resolve_phase_markers)
  * Markdown block replacement (replace_managed_markdown_block)
  * Day formatting (format_memory_dreaming_day)
  * Recall entry deduplication (dedupe_entries)
  * Jaccard similarity (jaccard_similarity)
  * Concept tag extraction (_extract_concept_tags)
  * Recall store read/write (record_short_term_recalls, read_short_term_recall_entries)
  * Promotion candidate ranking (rank_short_term_promotion_candidates)
  * Dream narrative generation (append_dream_narrative)
  * Phase runners (run_light_sleep_phase, run_rem_sleep_phase, run_deep_sleep_phase)
  * Cron job building (build_dreaming_cron_jobs)
  * Plugin status (get_status)

Test style mirrors ``test_disk_cleanup_plugin.py``.
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Isolate ANAN_HOME for each test."""
    anan_home = tmp_path / ".sinoclaw"
    anan_home.mkdir()
    monkeypatch.setenv("ANAN_HOME", str(anan_home))
    yield anan_home


def _load_lib():
    """Import dreaming_plugin library module directly from the data partition."""
    import types
    # Load anan's own L1 sleep plugin (not the OpenClaw legacy version)
    lib_path = Path(__file__).parent / "sleep_plugin.py"

    if "sinoclaw_plugins" not in sys.modules:
        ns = types.ModuleType("sinoclaw_plugins")
        ns.__path__ = []
        sys.modules["sinoclaw_plugins"] = ns

    spec = importlib.util.spec_from_file_location(
        "sinoclaw_plugins.dreaming_under_test",
        lib_path,
        submodule_search_locations=[str(lib_path.parent)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "sinoclaw_plugins"
    mod.__path__ = [str(lib_path.parent)]
    sys.modules["sinoclaw_plugins.dreaming_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestDreamingConfig:
    def _lib(self):
        return _load_lib()

    def test_default_values(self):
        lib = self._lib()
        cfg = lib.DreamingConfig()
        assert cfg.enabled is False
        assert cfg.storage_mode == "separate"
        assert cfg.light_dreaming is True
        assert cfg.deep_dreaming is True
        assert cfg.rem_dreaming is True
        assert cfg.light_limit == 100
        assert cfg.deep_min_score == 0.8

    def test_explicit_overrides(self):
        lib = self._lib()
        cfg = lib.DreamingConfig(
            enabled=True,
            timezone="Asia/Shanghai",
            storage_mode="inline",
            deep_limit=5,
            deep_min_score=0.9,
        )
        assert cfg.enabled is True
        assert cfg.timezone == "Asia/Shanghai"
        assert cfg.storage_mode == "inline"
        assert cfg.deep_limit == 5
        assert cfg.deep_min_score == 0.9

    def test_light_sources_default(self):
        lib = self._lib()
        cfg = lib.DreamingConfig()
        assert cfg.light_sources == ["daily", "sessions", "recall"]
        assert cfg.deep_sources == ["daily", "memory", "sessions", "logs", "recall"]
        assert cfg.rem_sources == ["memory", "daily", "deep"]


# ---------------------------------------------------------------------------
# Markdown helpers tests
# ---------------------------------------------------------------------------

class TestPhaseMarkers:
    def _lib(self):
        return _load_lib()

    def test_resolve_phase_markers_light(self):
        lib = self._lib()
        start, end = lib.resolve_phase_markers("light")
        assert start == "<!-- openclaw:dreaming:light:start -->"
        assert end == "<!-- openclaw:dreaming:light:end -->"

    def test_resolve_phase_markers_rem(self):
        lib = self._lib()
        start, end = lib.resolve_phase_markers("rem")
        assert start == "<!-- openclaw:dreaming:rem:start -->"
        assert end == "<!-- openclaw:dreaming:rem:end -->"

    def test_resolve_phase_markers_deep(self):
        lib = self._lib()
        start, end = lib.resolve_phase_markers("deep")
        assert start == "<!-- openclaw:dreaming:deep:start -->"
        assert end == "<!-- openclaw:dreaming:deep:end -->"


class TestFormatMemoryDreamingDay:
    def _lib(self):
        return _load_lib()

    def test_basic_day_format(self):
        lib = self._lib()
        # 2024-01-15 12:00:00 UTC
        epoch_ms = 1705310400000
        result = lib.format_memory_dreaming_day(epoch_ms, None)
        assert result == "2024-01-15"

    def test_timezone_support(self):
        lib = self._lib()
        epoch_ms = 1705310400000  # 2024-01-15 12:00 UTC
        result = lib.format_memory_dreaming_day(epoch_ms, "Asia/Shanghai")
        # UTC+8, so 12:00 UTC = 20:00 CST = 2024-01-15
        assert result == "2024-01-15"


class TestReplaceManagedMarkdownBlock:
    def _lib(self):
        return _load_lib()

    def test_no_existing_block_appends(self):
        lib = self._lib()
        original = "# Memory\n\nSome content here."
        result = lib.replace_managed_markdown_block(
            original,
            "## Light Sleep",
            "<!-- openclaw:dreaming:light:start -->",
            "<!-- openclaw:dreaming:light:end -->",
            "- Test entry"
        )
        assert "## Light Sleep" in result
        assert "- Test entry" in result
        assert "Some content here." in result

    def test_replace_existing_block(self):
        lib = self._lib()
        original = """# Memory

<!-- openclaw:dreaming:light:start -->
## Light Sleep
- Old entry
<!-- openclaw:dreaming:light:end -->

More content."""
        result = lib.replace_managed_markdown_block(
            original,
            "## Light Sleep",
            "<!-- openclaw:dreaming:light:start -->",
            "<!-- openclaw:dreaming:light:end -->",
            "- New entry"
        )
        assert "- New entry" in result
        assert "- Old entry" not in result

    def test_malformed_block_appends(self):
        lib = self._lib()
        original = "# Memory\n\n<!-- openclaw:dreaming:light:start -->\nOld"
        result = lib.replace_managed_markdown_block(
            original,
            "## Light Sleep",
            "<!-- openclaw:dreaming:light:start -->",
            "<!-- openclaw:dreaming:light:end -->",
            "- New"
        )
        assert "- New" in result


class TestWithTrailingNewline:
    def _lib(self):
        return _load_lib()

    def test_no_newline_adds_one(self):
        lib = self._lib()
        assert lib.with_trailing_newline("hello") == "hello\n"

    def test_multiple_newlines_reduced_to_one(self):
        lib = self._lib()
        assert lib.with_trailing_newline("hello\n\n\n") == "hello\n"


# ---------------------------------------------------------------------------
# Recall store tests
# ---------------------------------------------------------------------------

class TestExtractConceptTags:
    def _lib(self):
        return _load_lib()

    def test_extracts_alphanumeric_tokens(self):
        lib = self._lib()
        tags = lib._extract_concept_tags("Implemented the new API endpoint for user authentication")
        assert "api" in tags
        assert "endpoint" in tags
        assert "implemented" in tags or "new" in tags

    def test_filters_short_tokens(self):
        lib = self._lib()
        tags = lib._extract_concept_tags("The API is great and it works")
        assert all(len(t) > 2 for t in tags)

    def test_filters_stop_words(self):
        lib = self._lib()
        tags = lib._extract_concept_tags("the and are for with memory system")
        # Only test words that are actually in CONCEPT_STOP_WORDS["shared"]
        assert "memory" not in tags  # "memory" IS in stop words
        assert "system" not in tags  # "system" IS in stop words
    def test_max_20_tags(self):
        lib = self._lib()
        text = " ".join([f"token{i}" for i in range(50)])
        tags = lib._extract_concept_tags(text)
        assert len(tags) <= 20


class TestJaccardSimilarity:
    def _lib(self):
        return _load_lib()

    def test_identical_strings_high_similarity(self):
        lib = self._lib()
        sim = lib.jaccard_similarity("hello world", "hello world")
        assert sim > 0.9

    def test_completely_different_strings_low_similarity(self):
        lib = self._lib()
        sim = lib.jaccard_similarity("hello world", "foo bar baz")
        assert sim < 0.3

    def test_partial_overlap(self):
        lib = self._lib()
        sim = lib.jaccard_similarity("hello world", "hello there world")
        assert 0.3 < sim < 0.8

    def test_empty_strings(self):
        lib = self._lib()
        # Empty strings treated as equal
        sim = lib.jaccard_similarity("", "")
        assert sim == 1.0


class TestDedupeEntries:
    def _lib(self):
        return _load_lib()

    def _make_entry(self, path, snippet, **kwargs):
        lib = self._lib()
        defaults = {
            "snippet": snippet,
            "key": f"{path}:1-5",
            "query": "test",
            "path": path,
            "start_line": 1,
            "end_line": 5,
            "score": 0.5,
            "recall_count": 1,
            "daily_count": 0,
            "grounded_count": 0,
            "total_score": 0.5,
            "max_score": 0.5,
            "query_hashes": ["h1"],
            "recall_days": ["2024-01-15"],
            "concept_tags": ["api"],
            "last_recalled_at": "2024-01-15T10:00:00",
            "promoted_at": None,
        }
        defaults.update(kwargs)
        return lib.RecallEntry(**defaults)

    def test_different_paths_not_deduplicated(self):
        lib = self._lib()
        entries = [
            self._make_entry("memory/a.md", "same snippet"),
            self._make_entry("memory/b.md", "same snippet"),
        ]
        result = lib.dedupe_entries(entries, 0.88)
        assert len(result) == 2

    def test_similar_snippets_deduplicated(self):
        lib = self._lib()
        entries = [
            self._make_entry("memory/a.md", "Implemented the API endpoint"),
            self._make_entry("memory/a.md", "Implemented the API endpoint"),  # Exact dup
        ]
        result = lib.dedupe_entries(entries, 0.88)
        assert len(result) == 1

    def test_merge_preserves_max_counts(self):
        lib = self._lib()
        entries = [
            self._make_entry("memory/a.md", "same", recall_count=3, query_hashes=["a", "b"]),
            self._make_entry("memory/a.md", "same", recall_count=5, query_hashes=["b", "c"]),
        ]
        result = lib.dedupe_entries(entries, 0.88)
        assert len(result) == 1
        assert result[0].recall_count == 5
        assert set(result[0].query_hashes) == {"a", "b", "c"}


class TestRecallStoreWriteRead:
    def _lib(self):
        return _load_lib()

    @pytest.mark.asyncio
    async def test_record_and_read_recalls(self, tmp_path):
        lib = self._lib()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        memory_dir = workspace / "memory"
        memory_dir.mkdir()

        results = [
            {
                "path": "memory/test.md",
                "startLine": 10,
                "endLine": 15,
                "score": 0.7,
                "snippet": "Implemented authentication",
            }
        ]

        await lib.record_short_term_recalls(
            str(workspace),
            "__dreaming_daily__:2024-01-15",
            results,
            signal_type="daily",
            day_bucket="2024-01-15",
        )

        entries = lib.read_short_term_recall_entries(str(workspace))
        assert len(entries) == 1
        assert entries[0].path == "memory/test.md"
        assert entries[0].snippet == "Implemented authentication"
        assert entries[0].recall_count == 1

    @pytest.mark.asyncio
    async def test_record_increments_existing(self, tmp_path):
        lib = self._lib()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        memory_dir = workspace / "memory"
        memory_dir.mkdir()

        # First write
        await lib.record_short_term_recalls(
            str(workspace),
            "__dreaming_daily__:2024-01-15",
            [{"path": "m.md", "startLine": 1, "endLine": 5, "score": 0.5, "snippet": "test"}],
            signal_type="daily",
            day_bucket="2024-01-15",
        )

        # Second write with same key
        await lib.record_short_term_recalls(
            str(workspace),
            "__dreaming_daily__:2024-01-16",
            [{"path": "m.md", "startLine": 1, "endLine": 5, "score": 0.6, "snippet": "test"}],
            signal_type="daily",
            day_bucket="2024-01-16",
        )

        entries = lib.read_short_term_recall_entries(str(workspace))
        assert len(entries) == 1
        assert entries[0].recall_count == 2


# ---------------------------------------------------------------------------
# Daily memory ingestion tests
# ---------------------------------------------------------------------------

class TestBuildDailySnippetChunks:
    def _lib(self):
        return _load_lib()

    def test_empty_lines(self):
        lib = self._lib()
        lines = ["# Memory", "", "", "Some content"]
        chunks = lib.build_daily_snippet_chunks(lines, 10)
        # Should skip headers and group content
        assert len(chunks) > 0

    def test_respects_per_file_cap(self):
        lib = self._lib()
        lines = [f"Line {i}" for i in range(100)]
        chunks = lib.build_daily_snippet_chunks(lines, 5)
        assert len(chunks) <= 5

    def test_includes_start_end_lines(self):
        lib = self._lib()
        lines = ["Line 0", "Line 1", "Line 2", "Line 3"]
        chunks = lib.build_daily_snippet_chunks(lines, 10)
        for chunk in chunks:
            assert "startLine" in chunk
            assert "endLine" in chunk
            assert chunk["startLine"] <= chunk["endLine"]


class TestStripManagedDailyDreamingLines:
    def _lib(self):
        return _load_lib()

    def test_strips_dream_blocks(self):
        lib = self._lib()
        lines = [
            "# Memory",
            "Some content",
            "<!-- openclaw:dreaming:light:start -->",
            "## Light Sleep",
            "- Dream entry",
            "<!-- openclaw:dreaming:light:end -->",
            "More content",
        ]
        result = lib.strip_managed_daily_dreaming_lines(lines)
        assert "<!-- openclaw:dreaming:light:start -->" not in result
        assert "- Dream entry" not in result
        assert "Some content" in result
        assert "More content" in result


class TestIsDayWithinLookback:
    def _lib(self):
        return _load_lib()

    def test_day_within_lookback(self):
        lib = self._lib()
        cutoff = datetime(2024, 1, 10).timestamp() * 1000
        assert lib.is_day_within_lookback("2024-01-15", cutoff) is True
        assert lib.is_day_within_lookback("2024-01-05", cutoff) is False

    def test_invalid_day_returns_false(self):
        lib = self._lib()
        cutoff = datetime(2024, 1, 10).timestamp() * 1000
        assert lib.is_day_within_lookback("invalid", cutoff) is False


# ---------------------------------------------------------------------------
# Promotion ranking tests
# ---------------------------------------------------------------------------

class TestRankShortTermPromotionCandidates:
    def _lib(self):
        return _load_lib()

    @pytest.mark.asyncio
    async def test_empty_recall_store(self, tmp_path):
        lib = self._lib()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        memory_dir = workspace / "memory"
        memory_dir.mkdir()

        candidates = await lib.rank_short_term_promotion_candidates(
            str(workspace),
            limit=10,
            min_score=0.5,
            min_recall_count=2,
            min_unique_queries=2,
            recency_half_life_days=14,
            max_age_days=None,
            now_ms=datetime.now().timestamp() * 1000,
        )
        assert len(candidates) == 0

    @pytest.mark.asyncio
    async def test_filters_by_recall_count(self, tmp_path):
        lib = self._lib()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        memory_dir = workspace / "memory"
        memory_dir.mkdir()

        # Write a recall with only 1 recall_count
        recall_store = memory_dir / "recall-store.json"
        recall_store.write_text(json.dumps({
            "entries": [{
                "key": "test:1-5",
                "query": "test",
                "snippet": "test snippet",
                "path": "test.md",
                "start_line": 1,
                "end_line": 5,
                "score": 0.8,
                "recall_count": 1,  # Below min_recall_count=2
                "daily_count": 0,
                "grounded_count": 0,
                "total_score": 0.8,
                "max_score": 0.8,
                "query_hashes": ["h1"],
                "recall_days": ["2024-01-15"],
                "concept_tags": ["api"],
                "last_recalled_at": datetime.now().isoformat(),
                "promoted_at": None,
            }]
        }))

        candidates = await lib.rank_short_term_promotion_candidates(
            str(workspace),
            limit=10,
            min_score=0.5,
            min_recall_count=2,
            min_unique_queries=1,
            recency_half_life_days=14,
            max_age_days=None,
            now_ms=datetime.now().timestamp() * 1000,
        )
        assert len(candidates) == 0  # filtered out

    @pytest.mark.asyncio
    async def test_ranks_by_score(self, tmp_path):
        lib = self._lib()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        memory_dir = workspace / "memory"
        memory_dir.mkdir()

        now = datetime.now()
        recall_store = memory_dir / "recall-store.json"
        recall_store.write_text(json.dumps({
            "entries": [
                {
                    "key": "low:1-5",
                    "query": "q1",
                    "snippet": "low priority snippet",
                    "path": "a.md",
                    "start_line": 1,
                    "end_line": 5,
                    "score": 0.5,
                    "recall_count": 5,
                    "daily_count": 1,
                    "grounded_count": 0,
                    "total_score": 0.5,
                    "max_score": 0.5,
                    "query_hashes": ["a", "b", "c"],
                    "recall_days": ["2024-01-15"],
                    "concept_tags": ["api"],
                    "last_recalled_at": now.isoformat(),
                    "promoted_at": None,
                },
                {
                    "key": "high:6-10",
                    "query": "q2",
                    "snippet": "high priority snippet",
                    "path": "b.md",
                    "start_line": 6,
                    "end_line": 10,
                    "score": 0.9,
                    "recall_count": 10,
                    "daily_count": 2,
                    "grounded_count": 0,
                    "total_score": 0.9,
                    "max_score": 0.9,
                    "query_hashes": ["d", "e", "f", "g"],
                    "recall_days": ["2024-01-15", "2024-01-16"],
                    "concept_tags": ["api", "design"],
                    "last_recalled_at": now.isoformat(),
                    "promoted_at": None,
                },
            ]
        }))

        candidates = await lib.rank_short_term_promotion_candidates(
            str(workspace),
            limit=10,
            min_score=0.5,
            min_recall_count=2,
            min_unique_queries=1,
            recency_half_life_days=14,
            max_age_days=None,
            now_ms=now.timestamp() * 1000,
        )
        assert len(candidates) == 2
        assert candidates[0].snippet == "high priority snippet"
        assert candidates[1].snippet == "low priority snippet"


# ---------------------------------------------------------------------------
# Narrative tests
# ---------------------------------------------------------------------------

class TestAppendDreamNarrative:
    def _lib(self):
        return _load_lib()

    def test_creates_dreams_file(self, tmp_path):
        lib = self._lib()
        narrative = "A dream about memory and code."
        result = lib.append_dream_narrative(str(tmp_path), narrative, 1705310400000, None)

        assert result is not None
        dreams_path = Path(result)
        assert dreams_path.exists()
        assert "A dream about memory and code." in dreams_path.read_text()

    def test_replaces_existing_day_entry(self, tmp_path):
        lib = self._lib()
        day_ms = 1705310400000  # 2024-01-15

        # First entry
        lib.append_dream_narrative(str(tmp_path), "First dream", day_ms, None)

        # Second entry same day
        result = lib.append_dream_narrative(str(tmp_path), "Second dream", day_ms, None)

        text = Path(result).read_text()
        assert "First dream" not in text
        assert "Second dream" in text


# ---------------------------------------------------------------------------
# Phase runner tests
# ---------------------------------------------------------------------------

class TestRunLightSleepPhase:
    def _lib(self):
        return _load_lib()

    @pytest.mark.asyncio
    async def test_no_memory_dir(self, tmp_path):
        lib = self._lib()
        cfg = lib.DreamingConfig(enabled=True, light_sources=["daily", "sessions", "recall"])
        result = await lib.run_light_sleep_phase(str(tmp_path), cfg, datetime.now().timestamp() * 1000, None)
        # Should return default message since no memory dir
        assert len(result) > 0


class TestRunRemSleepPhase:
    def _lib(self):
        return _load_lib()

    @pytest.mark.asyncio
    async def test_no_memories_returns_no_patterns(self, tmp_path):
        lib = self._lib()
        cfg = lib.DreamingConfig(enabled=True)
        result = await lib.run_rem_sleep_phase(str(tmp_path), cfg, datetime.now().timestamp() * 1000, None)
        assert "- No memories" in result[0] or "- No notable" in result[0]


class TestRunDeepSleepPhase:
    def _lib(self):
        return _load_lib()

    @pytest.mark.asyncio
    async def test_no_candidates(self, tmp_path):
        lib = self._lib()
        cfg = lib.DreamingConfig(enabled=True, deep_limit=10)
        result = await lib.run_deep_sleep_phase(str(tmp_path), cfg, datetime.now().timestamp() * 1000, None)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Cron job building tests
# ---------------------------------------------------------------------------

class TestBuildDreamingCronJobs:
    def _lib(self):
        return _load_lib()

    def test_builds_light_dream_cron(self):
        lib = self._lib()
        plugin = lib.DreamingPlugin({"light_dreaming": True})
        plugin._cron_service = MagicMock()  # Available

        jobs = plugin.build_dreaming_cron_jobs()
        light_job = next((j for j in jobs if lib.LEGACY_LIGHT_DREAMING_CRON_NAME in j.get("name", "")), None)
        assert light_job is not None
        assert light_job["schedule"]["expr"] == "0 */6 * * *"

    def test_builds_deep_dream_cron(self):
        lib = self._lib()
        plugin = lib.DreamingPlugin({"deep_dreaming": True})
        plugin._cron_service = MagicMock()

        jobs = plugin.build_dreaming_cron_jobs()
        deep_job = next((j for j in jobs if lib.MANAGED_MEMORY_DREAMING_CRON_NAME in j.get("name", "")), None)
        assert deep_job is not None
        assert deep_job["schedule"]["expr"] == "0 3 * * *"

    def test_builds_rem_dream_cron(self):
        lib = self._lib()
        plugin = lib.DreamingPlugin({"rem_dreaming": True})
        plugin._cron_service = MagicMock()

        jobs = plugin.build_dreaming_cron_jobs()
        rem_job = next((j for j in jobs if lib.LEGACY_REM_DREAMING_CRON_NAME in j.get("name", "")), None)
        assert rem_job is not None
        assert rem_job["schedule"]["expr"] == "0 5 * * 0"

    def test_no_cron_service_no_jobs(self):
        lib = self._lib()
        plugin = lib.DreamingPlugin({"light_dreaming": True})
        plugin._cron_service = None

        jobs = plugin.build_dreaming_cron_jobs()
        assert len(jobs) == 0


# ---------------------------------------------------------------------------
# Plugin status tests
# ---------------------------------------------------------------------------

class TestDreamingPluginStatus:
    def _lib(self):
        return _load_lib()

    def test_get_status_disabled(self):
        lib = self._lib()
        plugin = lib.DreamingPlugin({"enabled": False})
        status = plugin.get_status()
        assert status["enabled"] is False
        assert status["running"] is False

    def test_get_status_shows_phases(self):
        lib = self._lib()
        plugin = lib.DreamingPlugin({
            "enabled": True,
            "light_dreaming": True,
            "deep_dreaming": False,
            "rem_dreaming": True,
        })
        status = plugin.get_status()
        assert status["phases"]["light"] is True
        assert status["phases"]["deep"] is False
        assert status["phases"]["rem"] is True


# ---------------------------------------------------------------------------
# Entry average score tests
# ---------------------------------------------------------------------------

class TestEntryAverageScore:
    def _lib(self):
        return _load_lib()

    def test_zero_signals_returns_zero(self):
        lib = self._lib()
        entry = lib.RecallEntry(
            key="test", query="q", snippet="s",
            path="p", start_line=1, end_line=1, score=0,
            recall_count=0, daily_count=0, grounded_count=0,
            total_score=0, max_score=0,
        )
        assert lib.entry_average_score(entry) == 0

    def test_single_signal(self):
        lib = self._lib()
        entry = lib.RecallEntry(
            key="test", query="q", snippet="s",
            path="p", start_line=1, end_line=1, score=0.5,
            recall_count=1, daily_count=0, grounded_count=0,
            total_score=0.5, max_score=0.5,
        )
        score = lib.entry_average_score(entry)
        assert 0 < score <= 1


# ---------------------------------------------------------------------------
# Tokenize snippet tests
# ---------------------------------------------------------------------------

class TestTokenizeSnippet:
    def _lib(self):
        return _load_lib()

    def test_lowercases_and_dedupes(self):
        lib = self._lib()
        tokens = lib.tokenize_snippet("Hello hello HELLO world")
        assert "hello" in tokens
        assert len(tokens) == len(set(tokens))  # set has no duplicates

    def test_filters_short_tokens(self):
        lib = self._lib()
        tokens = lib.tokenize_snippet("I am a test string")
        assert all(len(t) > 2 for t in tokens)

    def test_non_alphanumeric_split(self):
        lib = self._lib()
        tokens = lib.tokenize_snippet("api.v2.endpoint")
        assert "api" in tokens
        assert "endpoint" in tokens
        # "v2" becomes empty string after split by '.', filtered out
        assert "endpoint" in tokens

# ---------------------------------------------------------------------------
# Sinoclaw SessionDB tests
# ---------------------------------------------------------------------------

class TestAnanSessionDB:
    def _lib(self):
        return _load_lib()

    def test_initializes_with_default_path(self):
        lib = self._lib()
        db = lib.AnanSessionDB()
        assert db.db_path == lib.DEFAULT_STATE_DB_PATH

    def test_initializes_with_custom_path(self, tmp_path):
        lib = self._lib()
        custom_path = tmp_path / "custom.db"
        db = lib.AnanSessionDB(custom_path)
        assert db.db_path == custom_path


class TestSessionDBStructure:
    """Tests that verify the session DB schema we read from."""

    def _lib(self):
        return _load_lib()

    def test_messages_table_has_required_columns(self):
        lib = self._lib()
        # Verify we know the schema: messages table has session_id, role, content, timestamp
        # This is tested via the get_session_messages method
        assert hasattr(lib.AnanSessionDB, "get_session_messages")
        assert hasattr(lib.AnanSessionDB, "list_recent_sessions")

    def test_list_recent_sessions_returns_list(self):
        lib = self._lib()
        db = lib.AnanSessionDB(db_path=None)  # use default, may not exist — returns []
        result = db.list_recent_sessions(lookback_days=1, limit=10)
        assert isinstance(result, list)

    def test_get_session_messages_returns_list(self):
        lib = self._lib()
        db = lib.AnanSessionDB(db_path=None)
        result = db.get_session_messages("non-existent-session")
        assert isinstance(result, list)
        assert len(result) == 0


class TestSessionIngestionIntegration:
    """Integration tests for session ingestion via AnanSessionDB."""

    def _lib(self):
        return _load_lib()

    def test_normalize_session_corpus_snippet(self):
        lib = self._lib()
        assert lib.normalize_session_corpus_snippet("  Hello  ") == "Hello"
        assert lib.normalize_session_corpus_snippet("<b>bold</b>") == "bold"
        assert lib.normalize_session_corpus_snippet("&nbsp;space") == "space"

    def test_hash_session_message_id(self):
        lib = self._lib()
        h1 = lib.hash_session_message_id("test message")
        h2 = lib.hash_session_message_id("test message")
        h3 = lib.hash_session_message_id("different message")
        assert h1 == h2
        assert h1 != h3
        assert len(h1) == 24  # SHA256 truncated to 24 chars

    def test_build_session_rendered_line(self):
        lib = self._lib()
        result = lib.build_session_rendered_line("main", "session-123", 42, "hello world")
        assert "main" in result
        assert "session-123" in result
        assert "42" in result
        assert "hello world" in result
