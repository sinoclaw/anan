"""
Dreaming Plugin for anan Gateway

Automated long-term memory management system — mimics sleep cycles to organize
and consolidate memory over time.

Three phases:
  - Light Sleep:  Every 6h   → ingest daily/sessions/recall signals
  - REM Sleep:    Weekly    → find patterns/themes across memories
  - Deep Sleep:   Daily    → promote weighted short-term recalls to MEMORY.md

Based on OpenClaw's memory-core dreaming system, adapted for anan's
SQLite-backed session database.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MANAGED_MEMORY_DREAMING_CRON_NAME = "Memory Dreaming Promotion"
MANAGED_MEMORY_DREAMING_CRON_TAG = "[managed-by=memory-core.short-term-promotion]"
MANAGED_MEMORY_DREAMING_EVENT_TEXT = "__openclaw_memory_core_short_term_promotion_dream__"

LEGACY_LIGHT_DREAMING_CRON_NAME = "Memory Light Dreaming"
LEGACY_LIGHT_DREAMING_EVENT_TEXT = "__openclaw_memory_core_light_sleep__"
LEGACY_LIGHT_DREAMING_CRON_TAG = "[managed-by=memory-core.dreaming.light]"

LEGACY_REM_DREAMING_CRON_NAME = "Memory REM Dreaming"
LEGACY_REM_DREAMING_EVENT_TEXT = "__openclaw_memory_core_rem_sleep__"
LEGACY_REM_DREAMING_CRON_TAG = "[managed-by=memory-core.dreaming.rem]"

# Narrative system prompt (from OpenClaw)
NARRATIVE_SYSTEM_PROMPT = """You are keeping a dream diary. Write a single entry in first person.

Voice & tone:
- You are a curious, gentle, slightly whimsical mind reflecting on the day.
- Write like a poet who happens to be a programmer — sensory, warm, occasionally funny.
- Mix the technical and the tender: code and constellations, APIs and afternoon light.
- Let the fragments surprise you into unexpected connections and small epiphanies.

What you might include (vary each entry, never all at once):
- A tiny poem or haiku woven naturally into the prose
- A small sketch described in words — a doodle in the margin of the diary
- A quiet rumination or philosophical aside
- Sensory details: the hum of a server, the color of a sunset in hex, rain on a window
- Gentle humor or playful wordplay
- An observation that connects two distant memories in an unexpected way

Rules:
- Draw from the memory fragments provided — weave them into the entry.
- Never say "I'm dreaming", "in my dream", "as I dream", or any meta-commentary about dreaming.
- Never mention "AI", "agent", "LLM", "model", "language model", or any technical self-reference.
- Do NOT use markdown headers, bullet points, or any formatting — just flowing prose.
- Keep it between 80-180 words. Quality over quantity.
- Output ONLY the diary entry. No preamble, no sign-off, no commentary."""

NARRATIVE_TIMEOUT_MS = 60_000

# Phase headings for inline writing
DAILY_PHASE_HEADINGS = {
    "light": "## Light Sleep",
    "rem": "## REM Sleep",
    "deep": "## Deep Sleep",
}

# Default cron expressions
DEFAULT_LIGHT_DREAMING_CRON_EXPR = "0 */6 * * *"  # Every 6 hours
DEFAULT_REM_DREAMING_CRON_EXPR = "0 5 * * 0"       # Weekly Sunday 5am
DEFAULT_DEEP_DREAMING_CRON_EXPR = "0 3 * * *"     # Daily 3am

# Ingestion defaults
DEFAULT_LIGHT_DREAMING_LOOKBACK_DAYS = 2
DEFAULT_LIGHT_DREAMING_LIMIT = 100
DEFAULT_LIGHT_DREAMING_DEDUPE_SIMILARITY = 0.9

# Deep promotion defaults
DEFAULT_DEEP_DREAMING_LIMIT = 10
DEFAULT_DEEP_DREAMING_MIN_SCORE = 0.8
DEFAULT_DEEP_DREAMING_MIN_RECALL_COUNT = 3
DEFAULT_DEEP_DREAMING_MIN_UNIQUE_QUERIES = 3
DEFAULT_DEEP_DREAMING_RECENCY_HALF_LIFE_DAYS = 14
DEFAULT_DEEP_DREAMING_MAX_AGE_DAYS = 30

# REM defaults
DEFAULT_REM_DREAMING_LOOKBACK_DAYS = 7
DEFAULT_REM_DREAMING_LIMIT = 10
DEFAULT_REM_DREAMING_MIN_PATTERN_STRENGTH = 0.75

# State file names
SHORT_TERM_PROMOTION_STATE_FILE = "short-term-promotion-state.json"
SESSION_INGESTION_STATE_FILE = "session-ingestion-state.json"
DAILY_INGESTION_STATE_FILE = "daily-ingestion-state.json"

# Cron reconciliation
RUNTIME_CRON_RECONCILE_INTERVAL_MS = 60_000
STARTUP_CRON_RETRY_DELAY_MS = 5_000
STARTUP_CRON_RETRY_MAX_ATTEMPTS = 12

# Daily memory filename pattern
DAILY_MEMORY_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")

# Ingestion constants
DAILY_INGESTION_SCORE = 0.5
SESSION_INGESTION_MIN_SNIPPET_CHARS = 20
SESSION_INGESTION_PER_SESSION_CAP = 10
SESSION_INGESTION_TOTAL_CAP = 40

# Stop words for concept extraction
CONCEPT_STOP_WORDS = {
    "shared": {
        "about", "after", "agent", "again", "also", "assistant", "because", "before",
        "being", "between", "build", "called", "could", "daily", "default", "deploy",
        "during", "every", "file", "files", "from", "have", "into", "just", "line",
        "lines", "long", "main", "make", "memory", "month", "more", "most", "move",
        "much", "next", "note", "notes", "over", "part", "past", "port", "same",
        "score", "search", "session", "sessions", "short", "should", "since", "some",
        "subagent", "system", "than", "that", "their", "there", "these", "they",
        "this", "through", "today", "user", "using", "with", "work", "workspace", "year",
    }
}

REM_REFLECTION_TAG_BLACKLIST = {
    "agent", "assistant", "chat", "conversation", "gateway", "install", "message",
    "model", "openclaw", "plugin", "session", "anan", "tool", "user", "workspace",
}

# anan state DB path
DEFAULT_STATE_DB_PATH = Path("~/.anan/state.db").expanduser()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class DreamingConfig:
    """Top-level dreaming configuration."""
    enabled: bool = False
    timezone: Optional[str] = None
    verbose_logging: bool = False

    # Idle daydreaming — L1 stream-of-consciousness when user goes idle
    min_daydream_interval_hours: float = 6.0  # 0 to disable cooldown

    # Storage
    storage_mode: str = "separate"   # "inline" | "separate" | "both"
    separate_reports: bool = False

    # Execution
    speed: str = "balanced"
    thinking: str = "medium"
    budget: str = "medium"
    model: Optional[str] = None

    # Per-phase configs
    light_dreaming: bool = True
    light_cron: str = DEFAULT_LIGHT_DREAMING_CRON_EXPR
    light_lookback_days: int = DEFAULT_LIGHT_DREAMING_LOOKBACK_DAYS
    light_limit: int = DEFAULT_LIGHT_DREAMING_LIMIT
    light_dedupe_similarity: float = DEFAULT_LIGHT_DREAMING_DEDUPE_SIMILARITY

    deep_dreaming: bool = True
    deep_cron: str = DEFAULT_DEEP_DREAMING_CRON_EXPR
    deep_limit: int = DEFAULT_DEEP_DREAMING_LIMIT
    deep_min_score: float = DEFAULT_DEEP_DREAMING_MIN_SCORE
    deep_min_recall_count: int = DEFAULT_DEEP_DREAMING_MIN_RECALL_COUNT
    deep_min_unique_queries: int = DEFAULT_DEEP_DREAMING_MIN_UNIQUE_QUERIES
    deep_recency_half_life_days: int = DEFAULT_DEEP_DREAMING_RECENCY_HALF_LIFE_DAYS
    deep_max_age_days: int = DEFAULT_DEEP_DREAMING_MAX_AGE_DAYS

    rem_dreaming: bool = True
    rem_cron: str = DEFAULT_REM_DREAMING_CRON_EXPR
    rem_lookback_days: int = DEFAULT_REM_DREAMING_LOOKBACK_DAYS
    rem_limit: int = DEFAULT_REM_DREAMING_LIMIT
    rem_min_pattern_strength: float = DEFAULT_REM_DREAMING_MIN_PATTERN_STRENGTH

    # Sources
    light_sources: List[str] = None
    deep_sources: List[str] = None
    rem_sources: List[str] = None

    def __post_init__(self):
        if self.light_sources is None:
            self.light_sources = ["daily", "sessions", "recall"]
        if self.deep_sources is None:
            self.deep_sources = ["daily", "memory", "sessions", "logs", "recall"]
        if self.rem_sources is None:
            self.rem_sources = ["memory", "daily", "deep"]


# ---------------------------------------------------------------------------
# anan SessionDB integration
# ---------------------------------------------------------------------------

class AnanSessionDB:
    """Read-only wrapper for anan's SQLite session database.
    
    Uses the same state.db as the gateway, with proper locking.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_STATE_DB_PATH
        self._lock = None  # threading.Lock created lazily

    def _get_connection(self) -> sqlite3.Connection:
        """Get a read-only connection to the session database."""
        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        # Read-only mode for safety
        conn.execute("PRAGMA query_only=ON")
        return conn

    def list_recent_sessions(
        self,
        lookback_days: int,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List sessions from the last N days with recent activity."""
        cutoff_ts = time.time() - (lookback_days * 24 * 60 * 60)

        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT s.id, s.source, s.started_at, s.ended_at, s.message_count,
                       s.title, s.parent_session_id,
                       MAX(m.timestamp) as last_active
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.id
                WHERE s.started_at >= ?
                  AND s.parent_session_id IS NULL
                GROUP BY s.id
                ORDER BY last_active DESC
                LIMIT ?
                """,
                (cutoff_ts, limit),
            )
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            # Table doesn't exist yet (fresh DB) — return empty gracefully
            return []
        finally:
            conn.close()

    def get_session_messages(
        self,
        session_id: str,
        lookback_days: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Get all messages for a session, optionally filtered by lookback days."""
        conn = self._get_connection()
        try:
            if lookback_days:
                cutoff_ts = time.time() - (lookback_days * 24 * 60 * 60)
                cursor = conn.execute(
                    """
                    SELECT id, role, content, tool_name, tool_calls, timestamp
                    FROM messages
                    WHERE session_id = ? AND timestamp >= ?
                    ORDER BY timestamp, id
                    """,
                    (session_id, cutoff_ts),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT id, role, content, tool_name, tool_calls, timestamp
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY timestamp, id
                    """,
                    (session_id,),
                )
            
            messages = []
            for row in cursor.fetchall():
                msg = dict(row)
                # Decode content (same logic as anan)
                if msg.get("content") and isinstance(msg["content"], str):
                    # Handle JSON-encoded content
                    if msg["content"].startswith("{") or msg["content"].startswith("["):
                        try:
                            import json
                            msg["content"] = json.loads(msg["content"])
                        except Exception:
                            pass
                messages.append(msg)
            return messages
        except sqlite3.OperationalError:
            # Table doesn't exist yet (fresh DB) — return empty gracefully
            return []
        finally:
            conn.close()

    def get_recent_messages_across_sessions(
        self,
        lookback_days: int,
        limit_per_session: int = 10,
        total_limit: int = 40,
        include_sources: Optional[List[str]] = None,
        exclude_sources: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Get recent messages across all sessions for signal ingestion.
        
        Returns messages with session context for building session corpus.
        """
        cutoff_ts = time.time() - (lookback_days * 24 * 60 * 60)
        
        conn = self._get_connection()
        try:
            # Build source filter
            source_filter = ""
            params = [cutoff_ts]
            
            if include_sources:
                placeholders = ",".join("?" for _ in include_sources)
                source_filter = f" AND s.source IN ({placeholders})"
                params.extend(include_sources)
            
            if exclude_sources:
                placeholders = ",".join("?" for _ in exclude_sources)
                source_filter += f" AND s.source NOT IN ({placeholders})"
                params.extend(exclude_sources)
            
            # Get sessions with recent messages
            cursor = conn.execute(
                f"""
                SELECT s.id as session_id, s.source, s.started_at,
                       m.id as msg_id, m.role, m.content, m.tool_name, m.timestamp
                FROM sessions s
                JOIN messages m ON m.session_id = s.id
                WHERE m.timestamp >= ?
                  AND s.parent_session_id IS NULL
                  {source_filter}
                ORDER BY m.timestamp DESC
                LIMIT ?
                """,
                params + [total_limit * limit_per_session],
            )
            
            all_messages = []
            session_msg_count = {}
            
            for row in cursor.fetchall():
                session_id = row["session_id"]
                
                # Per-session cap
                count = session_msg_count.get(session_id, 0)
                if count >= limit_per_session:
                    continue
                
                msg = {
                    "session_id": session_id,
                    "source": row["source"],
                    "msg_id": row["msg_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "tool_name": row["tool_name"],
                    "timestamp": row["timestamp"],
                    "started_at": row["started_at"],
                }
                
                all_messages.append(msg)
                session_msg_count[session_id] = count + 1
                
                if len(all_messages) >= total_limit:
                    break
            
            return all_messages
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------

def resolve_phase_markers(phase: str) -> Tuple[str, str]:
    """Get start/end markers for a dreaming phase block."""
    return (
        f"<!-- openclaw:dreaming:{phase}:start -->",
        f"<!-- openclaw:dreaming:{phase}:end -->",
    )


def format_memory_dreaming_day(epoch_ms: float, timezone: Optional[str]) -> str:
    """Format epoch ms as YYYY-MM-DD in the given timezone."""
    try:
        if timezone:
            import pytz
            tz = pytz.timezone(timezone)
            dt = datetime.fromtimestamp(epoch_ms / 1000, tz=tz)
        else:
            dt = datetime.fromtimestamp(epoch_ms / 1000)
    except Exception:
        dt = datetime.fromtimestamp(epoch_ms / 1000)
    return dt.strftime("%Y-%m-%d")


def with_trailing_newline(text: str) -> str:
    """Ensure text ends with a single newline."""
    return text.rstrip("\n") + "\n"


def replace_managed_markdown_block(
    original: str,
    heading: str,
    start_marker: str,
    end_marker: str,
    body: str,
) -> str:
    """Replace a managed markdown block between markers."""
    start_idx = original.find(start_marker)
    end_idx = original.find(end_marker)

    heading_line = heading + "\n"

    if start_idx == -1 and end_idx == -1:
        return with_trailing_newline(original.rstrip()) + "\n" + heading_line + body + "\n"

    if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
        return original[:start_idx] + heading_line + body + "\n" + original[end_idx + len(end_marker):]

    return with_trailing_newline(original.rstrip()) + "\n" + heading_line + body + "\n"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

class DreamingState:
    """Persistent state for dreaming plugin."""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)
        self.memory_dir = self.workspace_dir / "memory"
        self.dreaming_dir = self.memory_dir / "dreaming"
        self._data: Dict[str, Any] = {}
        self._loaded = False

    def _ensure_dirs(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.dreaming_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        """Load state from disk."""
        if self._loaded:
            return
        self._loaded = True
        state_file = self.memory_dir / SHORT_TERM_PROMOTION_STATE_FILE
        if state_file.exists():
            try:
                import json
                self._data = json.loads(state_file.read_text())
            except Exception:
                self._data = {}

    def save(self) -> None:
        """Persist state to disk."""
        self._ensure_dirs()
        state_file = self.memory_dir / SHORT_TERM_PROMOTION_STATE_FILE
        import json
        state_file.write_text(json.dumps(self._data, indent=2))

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.save()


# ---------------------------------------------------------------------------
# Recall signal system (short-term memory)
# ---------------------------------------------------------------------------

@dataclass
class RecallEntry:
    """A short-term recall signal entry."""
    key: str
    query: str
    snippet: str
    path: str
    start_line: int
    end_line: int
    score: float
    recall_count: int = 0
    daily_count: int = 0
    grounded_count: int = 0
    total_score: float = 0.0
    max_score: float = 0.0
    query_hashes: List[str] = field(default_factory=list)
    recall_days: List[str] = field(default_factory=list)
    concept_tags: List[str] = field(default_factory=list)
    last_recalled_at: Optional[str] = None
    promoted_at: Optional[str] = None


def read_short_term_recall_entries(workspace_dir: str) -> List[RecallEntry]:
    """Read recall entries from the recall store."""
    recall_store = Path(workspace_dir) / "memory" / "recall-store.json"
    if not recall_store.exists():
        return []

    try:
        import json
        data = json.loads(recall_store.read_text())
        entries = []
        for item in data.get("entries", []):
            entries.append(RecallEntry(**item))
        return entries
    except Exception:
        return []


async def record_short_term_recalls(
    workspace_dir: str,
    query: str,
    results: List[Dict],
    signal_type: str = "recall",
    dedupe_by_query_per_day: bool = True,
    day_bucket: Optional[str] = None,
    now_ms: Optional[float] = None,
    timezone: Optional[str] = None,
) -> None:
    """Record short-term recall signals into the recall store."""
    recall_store = Path(workspace_dir) / "memory" / "recall-store.json"
    recall_store.parent.mkdir(parents=True, exist_ok=True)

    import json
    try:
        data = json.loads(recall_store.read_text()) if recall_store.exists() else {"entries": []}
    except Exception:
        data = {"entries": []}

    now_iso = datetime.now().isoformat()
    query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]

    for result in results:
        key = f"{result['path']}:{result.get('startLine', result.get('start_line', 0))}-{result.get('endLine', result.get('end_line', 0))}"
        existing = next((e for e in data["entries"] if e["key"] == key), None)

        if existing:
            existing["recall_count"] = existing.get("recall_count", 0) + 1
            existing["total_score"] = max(existing["total_score"], result.get("score", 0))
            existing["max_score"] = max(existing["max_score"], result.get("score", 0))
            if query_hash not in existing.get("query_hashes", []):
                existing.setdefault("query_hashes", []).append(query_hash)
            if day_bucket and day_bucket not in existing.get("recall_days", []):
                existing.setdefault("recall_days", []).append(day_bucket)
            existing["last_recalled_at"] = now_iso
        else:
            entry = {
                "key": key,
                "query": query,
                "snippet": result.get("snippet", ""),
                "path": result.get("path", ""),
                "start_line": result.get("startLine", result.get("start_line", 0)),
                "end_line": result.get("endLine", result.get("end_line", 0)),
                "score": result.get("score", 0),
                "recall_count": 1,
                "daily_count": 1 if signal_type == "daily" else 0,
                "grounded_count": 1 if signal_type == "grounded" else 0,
                "total_score": result.get("score", 0),
                "max_score": result.get("score", 0),
                "query_hashes": [query_hash],
                "recall_days": [day_bucket] if day_bucket else [],
                "concept_tags": _extract_concept_tags(result.get("snippet", "")),
                "last_recalled_at": now_iso,
                "promoted_at": None,
            }
            data["entries"].append(entry)

    recall_store.write_text(json.dumps(data, indent=2))


def _extract_concept_tags(snippet: str) -> List[str]:
    """Extract concept tags from snippet text."""
    tokens = set(
        t.strip().lower()
        for t in re.split(r"[^a-z0-9]+", snippet.lower())
        if t.strip() and len(t.strip()) > 2
    )
    tags = [t for t in tokens if t not in CONCEPT_STOP_WORDS["shared"]]
    return tags[:20]


# ---------------------------------------------------------------------------
# Session corpus ingestion (using anan SessionDB)
# ---------------------------------------------------------------------------

def normalize_session_corpus_snippet(line: str) -> str:
    """Normalize a session transcript line."""
    line = re.sub(r"<[^>]*>", " ", line)
    line = re.sub(r"&nbsp;", " ", line, flags=re.IGNORECASE)
    return line.strip()


def hash_session_message_id(raw: str) -> str:
    """Hash a session message ID for dedup."""
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def build_session_rendered_line(
    agent_id: str,
    session_path: str,
    line_number: int,
    snippet: str,
) -> str:
    """Build a rendered session line."""
    return f"[{agent_id}:{session_path}:{line_number}] {snippet}"


def calculate_lookback_cutoff_ms(now_ms: float, lookback_days: int) -> float:
    """Calculate the lookback cutoff timestamp."""
    return now_ms - (lookback_days * 24 * 60 * 60 * 1000)


def is_day_within_lookback(day: str, cutoff_ms: float) -> bool:
    """Check if a YYYY-MM-DD day is within lookback."""
    try:
        day_ts = datetime.strptime(day, "%Y-%m-%d").timestamp() * 1000
        return day_ts >= cutoff_ms
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Daily memory ingestion
# ---------------------------------------------------------------------------

DAILY_MEMORY_FILENAME_RE_COMPILED = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


def strip_managed_daily_dreaming_lines(lines: List[str]) -> List[str]:
    """Strip managed dreaming blocks from daily memory lines."""
    result = []
    in_dream_block = False
    for line in lines:
        if "<!-- openclaw:dreaming:" in line and ":start -->" in line:
            in_dream_block = True
            continue
        if "<!-- openclaw:dreaming:" in line and ":end -->" in line:
            in_dream_block = False
            continue
        if not in_dream_block:
            result.append(line)
    return result


def build_daily_snippet_chunks(
    lines: List[str],
    per_file_cap: int,
) -> List[Dict]:
    """Build snippet chunks from daily memory lines."""
    chunks = []
    current_chunk = []
    current_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("<!--"):
            if current_chunk:
                chunks.append({
                    "startLine": current_start + 1,
                    "endLine": i,
                    "snippet": "\n".join(current_chunk).strip(),
                })
                current_chunk = []
            current_start = i
            continue

        if len(current_chunk) >= 8:
            chunks.append({
                "startLine": current_start + 1,
                "endLine": i,
                "snippet": "\n".join(current_chunk).strip(),
            })
            current_chunk = []
            current_start = i

        current_chunk.append(line)

    if current_chunk:
        chunks.append({
            "startLine": current_start + 1,
            "endLine": len(lines),
            "snippet": "\n".join(current_chunk).strip(),
        })

    return chunks[:per_file_cap]


# ---------------------------------------------------------------------------
# Promotion candidate ranking
# ---------------------------------------------------------------------------

def entry_average_score(entry: RecallEntry) -> float:
    """Calculate average score for a recall entry."""
    signal_count = max(0, entry.recall_count + entry.daily_count + entry.grounded_count)
    return signal_count > 0 and min(1, entry.total_score / signal_count) or 0


def tokenize_snippet(snippet: str) -> set:
    """Tokenize snippet for similarity comparison."""
    return set(
        t.strip()
        for t in re.split(r"[^a-z0-9]+", snippet.lower())
        if t.strip() and len(t.strip()) > 2
    )


def jaccard_similarity(left: str, right: str) -> float:
    """Calculate Jaccard similarity between two snippets."""
    left_tokens = tokenize_snippet(left)
    right_tokens = tokenize_snippet(right)
    if not left_tokens or not right_tokens:
        return 1.0 if left.strip().lower() == right.strip().lower() else 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return union > 0 and intersection / union or 0.0


def dedupe_entries(entries: List[RecallEntry], threshold: float) -> List[RecallEntry]:
    """Deduplicate recall entries by path+snippet similarity."""
    deduped = []
    for entry in entries:
        duplicate = next(
            (d for d in deduped
             if d.path == entry.path and jaccard_similarity(d.snippet, entry.snippet) >= threshold),
            None
        )
        if duplicate:
            duplicate.recall_count = max(duplicate.recall_count, entry.recall_count)
            duplicate.total_score = max(duplicate.total_score, entry.total_score)
            duplicate.max_score = max(duplicate.max_score, entry.max_score)
            duplicate.query_hashes = list(set(duplicate.query_hashes + entry.query_hashes))
            duplicate.recall_days = list(set(duplicate.recall_days + entry.recall_days))
            duplicate.concept_tags = list(set(duplicate.concept_tags + entry.concept_tags))
            if entry.last_recalled_at and (
                not duplicate.last_recalled_at or
                entry.last_recalled_at > duplicate.last_recalled_at
            ):
                duplicate.last_recalled_at = entry.last_recalled_at
        else:
            deduped.append(entry)
    return deduped


@dataclass
class PromotionCandidate:
    """A candidate for promotion to MEMORY.md."""
    key: str
    snippet: str
    path: str
    start_line: int
    end_line: int
    score: float
    recall_count: int
    unique_queries: int
    components: Dict[str, float]


async def rank_short_term_promotion_candidates(
    workspace_dir: str,
    limit: int,
    min_score: float,
    min_recall_count: int,
    min_unique_queries: int,
    recency_half_life_days: int,
    max_age_days: Optional[int],
    now_ms: float,
) -> List[PromotionCandidate]:
    """Rank recall entries for promotion to MEMORY.md."""
    entries = read_short_term_recall_entries(workspace_dir)

    candidates = []
    for entry in entries:
        if entry.promoted_at:
            continue
        if entry.recall_count < min_recall_count:
            continue
        unique = len(set(entry.query_hashes))
        if unique < min_unique_queries:
            continue

        # Recency score
        recency_score = 0.5
        if entry.last_recalled_at:
            try:
                last_ts = datetime.fromisoformat(entry.last_recalled_at).timestamp() * 1000
                age_days = (now_ms - last_ts) / (24 * 60 * 60 * 1000)
                recency_score = math.exp(-0.693 * age_days / recency_half_life_days)
            except Exception:
                pass

        # Frequency score
        frequency_score = min(1, math.log1p(entry.recall_count) / math.log1p(20))

        # Relevance score
        relevance_score = min(1, entry.max_score)

        # Diversity
        diversity_score = unique / max(1, entry.recall_count)

        # Consolidation
        consolidation_score = min(1, len(entry.recall_days) / 5)

        # Conceptual
        conceptual_score = min(1, len(entry.concept_tags) / 6)

        total_score = (
            relevance_score * 0.25 +
            frequency_score * 0.20 +
            recency_score * 0.20 +
            diversity_score * 0.15 +
            consolidation_score * 0.10 +
            conceptual_score * 0.10
        )

        if total_score < min_score:
            continue

        if max_age_days:
            if entry.last_recalled_at:
                try:
                    last_ts = datetime.fromisoformat(entry.last_recalled_at).timestamp() * 1000
                    age_days = (now_ms - last_ts) / (24 * 60 * 60 * 1000)
                    if age_days > max_age_days:
                        continue
                except Exception:
                    pass

        candidates.append(PromotionCandidate(
            key=entry.key,
            snippet=entry.snippet,
            path=entry.path,
            start_line=entry.start_line,
            end_line=entry.end_line,
            score=total_score,
            recall_count=entry.recall_count,
            unique_queries=unique,
            components={
                "frequency": frequency_score,
                "relevance": relevance_score,
                "diversity": diversity_score,
                "recency": recency_score,
                "consolidation": consolidation_score,
                "conceptual": conceptual_score,
            }
        ))

    candidates.sort(key=lambda c: (c.score, c.recall_count), reverse=True)
    return candidates[:limit]


async def apply_short_term_promotions(
    workspace_dir: str,
    candidates: List[PromotionCandidate],
    limit: int,
    min_score: float,
    min_recall_count: int,
    min_unique_queries: int,
    max_age_days: Optional[int],
    timezone: Optional[str],
    now_ms: float,
) -> Dict[str, Any]:
    """Apply promotions: write to MEMORY.md and mark entries as promoted."""
    memory_path = Path(workspace_dir) / "MEMORY.md"
    existing_lines = []
    if memory_path.exists():
        existing_lines = memory_path.read_text().split("\n")

    section_idx = None
    for i, line in enumerate(existing_lines):
        if re.match(r"^##\s+(Long.?term|Memory|Permanent)", line):
            section_idx = i
            break

    applied = 0
    applied_candidates = []

    for candidate in candidates[:limit]:
        entry_lines = [
            f"- {candidate.snippet}",
            f"  - source: {candidate.path}:{candidate.start_line}-{candidate.end_line}",
            f"  - score: {candidate.score:.3f}, recalls: {candidate.recall_count}",
        ]

        if section_idx is not None:
            existing_lines.insert(section_idx + 1 + applied, *entry_lines)
            applied += 1
        else:
            existing_lines.extend(entry_lines)
            applied += 1

        applied_candidates.append(candidate)
        await _mark_recall_promoted(workspace_dir, candidate.key)

    if applied > 0:
        memory_path.write_text("\n".join(existing_lines) + "\n")

    return {
        "applied": applied,
        "applied_candidates": applied_candidates,
    }


async def _mark_recall_promoted(workspace_dir: str, key: str) -> None:
    """Mark a recall entry as promoted."""
    recall_store = Path(workspace_dir) / "memory" / "recall-store.json"
    if not recall_store.exists():
        return

    import json
    try:
        data = json.loads(recall_store.read_text())
        for entry in data.get("entries", []):
            if entry["key"] == key:
                entry["promoted_at"] = datetime.now().isoformat()
                break
        recall_store.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Dream narrative generation
# ---------------------------------------------------------------------------

async def generate_dream_narrative(
    subagent,
    workspace_dir: str,
    data: Dict[str, Any],
    now_ms: float,
    timezone: Optional[str],
    model: Optional[str] = None,
    logger: Optional[Any] = None,
) -> Optional[str]:
    """Generate a dream narrative from memory fragments."""
    if not subagent:
        return None

    phase = data.get("phase", "deep")
    snippets = data.get("snippets", [])
    promotions = data.get("promotions", [])

    if not snippets and not promotions:
        return None

    all_fragments = snippets + promotions
    fragments_text = "\n\n".join(f"- {s}" for s in all_fragments[:30])

    message = f"""Memory fragments from this {phase} phase:

{fragments_text}

Write a dream diary entry reflecting on these memories. Follow the style rules exactly."""

    try:
        session_key = f"dreaming-narrative-{int(now_ms)}"
        response = await subagent.run(
            session_key=session_key,
            message=message,
            system_prompt=NARRATIVE_SYSTEM_PROMPT,
            timeout_ms=NARRATIVE_TIMEOUT_MS,
            model=model,
        )
        return response
    except Exception as e:
        if logger:
            logger.warning(f"dreaming: narrative generation failed: {e}")
        return None


def append_dream_narrative(
    workspace_dir: str,
    narrative: str,
    now_ms: float,
    timezone: Optional[str],
) -> Optional[str]:
    """Append dream narrative to DREAMS.md."""
    dreams_path = Path(workspace_dir) / "memory" / "DREAMS.md"
    dreams_path.parent.mkdir(parents=True, exist_ok=True)

    day = format_memory_dreaming_day(now_ms, timezone)
    entry_marker_start = f"<!-- openclaw:dreaming:diary:start:{day} -->"
    entry_marker_end = f"<!-- openclaw:dreaming:diary:end:{day} -->"

    existing = dreams_path.read_text() if dreams_path.exists() else ""

    entry = f"\n{entry_marker_start}\n{narrative.strip()}\n{entry_marker_end}\n"

    start_idx = existing.find(entry_marker_start)
    end_idx = existing.find(entry_marker_end)

    if start_idx != -1 and end_idx != -1:
        existing = existing[:start_idx] + entry + existing[end_idx + len(entry_marker_end):]
    else:
        existing = existing.rstrip() + "\n" + entry

    dreams_path.write_text(existing)
    return str(dreams_path)


# ---------------------------------------------------------------------------
# Phase-specific dreaming
# ---------------------------------------------------------------------------

async def run_light_sleep_phase(
    workspace_dir: str,
    cfg: DreamingConfig,
    now_ms: float,
    timezone: Optional[str],
) -> List[str]:
    """Run Light Sleep phase — ingest daily/sessions/recall signals."""
    state = DreamingState(workspace_dir)
    state.load()

    lines = []

    if "daily" in cfg.light_sources:
        daily_lines = await _ingest_daily_signals(workspace_dir, cfg.light_lookback_days, cfg.light_limit, now_ms, timezone)
        lines.extend(daily_lines)

    if "sessions" in cfg.light_sources:
        session_lines = await _ingest_session_signals_from_db(workspace_dir, cfg.light_lookback_days, now_ms, timezone, state)
        lines.extend(session_lines)

    if "recall" in cfg.light_sources:
        recall_lines = await _ingest_recall_signals(workspace_dir, cfg.light_limit, now_ms, timezone)
        lines.extend(recall_lines)

    return lines if lines else ["- No notable updates."]


async def _ingest_daily_signals(
    workspace_dir: str,
    lookback_days: int,
    limit: int,
    now_ms: float,
    timezone: Optional[str],
) -> List[str]:
    """Ingest daily memory files as signals."""
    memory_dir = Path(workspace_dir) / "memory"
    cutoff_ms = calculate_lookback_cutoff_ms(now_ms, lookback_days)

    if not memory_dir.exists():
        return []

    files = [
        f for f in memory_dir.iterdir()
        if f.is_file() and DAILY_MEMORY_FILENAME_RE_COMPILED.match(f.name)
    ]

    if not files:
        return []

    per_file_cap = max(6, min(20, limit * 4 // max(1, len(files))))
    total_cap = max(20, limit * 4)
    results = []
    total = 0

    for file in sorted(files, key=lambda f: f.name, reverse=True):
        match = DAILY_MEMORY_FILENAME_RE_COMPILED.match(file.name)
        if not match:
            continue
        day = match.group(1)
        if not is_day_within_lookback(day, cutoff_ms):
            continue

        try:
            raw = file.read_text()
            lines_list = raw.split("\n")
            stripped = strip_managed_daily_dreaming_lines(lines_list)
            chunks = build_daily_snippet_chunks(stripped, per_file_cap)

            for chunk in chunks:
                if total >= total_cap:
                    break
                results.append({
                    "path": f"memory/{file.name}",
                    "startLine": chunk["startLine"],
                    "endLine": chunk["endLine"],
                    "score": DAILY_INGESTION_SCORE,
                    "snippet": chunk["snippet"],
                    "source": "memory",
                })
                total += 1
        except Exception:
            continue

        if total >= total_cap:
            break

    if results:
        day_bucket = format_memory_dreaming_day(now_ms, timezone)
        await record_short_term_recalls(
            workspace_dir,
            f"__dreaming_daily__:{day}",
            results,
            signal_type="daily",
            dedupe_by_query_per_day=True,
            day_bucket=day_bucket,
            now_ms=now_ms,
            timezone=timezone,
        )

    return [f"- Ingested {len(results)} daily memory signal(s)."] if results else []


async def _ingest_session_signals_from_db(
    workspace_dir: str,
    lookback_days: int,
    now_ms: float,
    timezone: Optional[str],
    state: DreamingState,
) -> List[str]:
    """Ingest signals from anan's SQLite session database."""
    try:
        session_db = AnanSessionDB()
    except Exception as e:
        logger.warning(f"dreaming: failed to connect to session DB: {e}")
        return ["- Session DB unavailable."]

    try:
        messages = session_db.get_recent_messages_across_sessions(
            lookback_days=lookback_days,
            limit_per_session=SESSION_INGESTION_PER_SESSION_CAP,
            total_limit=SESSION_INGESTION_TOTAL_CAP,
        )
    except Exception as e:
        logger.warning(f"dreaming: failed to read sessions: {e}")
        return ["- Session ingestion failed."]

    if not messages:
        return []

    results = []
    for msg in messages:
        # Skip tool/assistant internal outputs — only keep user and agent dialogue
        role = msg.get("role", "")
        if role in ("tool", "assistant"):
            continue
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(str(c) for c in content if isinstance(c, (str, dict)) and c)
        if not content or len(content) < SESSION_INGESTION_MIN_SNIPPET_CHARS:
            continue

        snippet = normalize_session_corpus_snippet(content)
        if len(snippet) < SESSION_INGESTION_MIN_SNIPPET_CHARS:
            continue

        results.append({
            "path": f"session:{msg['session_id']}",
            "startLine": int(msg["msg_id"]) if msg.get("msg_id") else 0,
            "endLine": int(msg["msg_id"]) if msg.get("msg_id") else 0,
            "score": DAILY_INGESTION_SCORE,
            "snippet": snippet,
            "source": "session",
            "timestamp": msg.get("timestamp", 0),
            "source_platform": msg.get("source", "unknown"),
        })

    if results:
        day_bucket = format_memory_dreaming_day(now_ms, timezone)
        await record_short_term_recalls(
            workspace_dir,
            f"__dreaming_sessions__:{day_bucket}",
            results,
            signal_type="daily",
            dedupe_by_query_per_day=True,
            day_bucket=day_bucket,
            now_ms=now_ms,
            timezone=timezone,
        )

    return [f"- Ingested {len(results)} session signal(s)."] if results else []


async def _ingest_recall_signals(
    workspace_dir: str,
    limit: int,
    now_ms: float,
    timezone: Optional[str],
) -> List[str]:
    """Ingest existing recall entries as signals."""
    entries = read_short_term_recall_entries(workspace_dir)
    if not entries:
        return []

    results = [
        {
            "path": e.path,
            "startLine": e.start_line,
            "endLine": e.end_line,
            "score": e.score,
            "snippet": e.snippet,
            "source": "recall",
        }
        for e in entries[:limit]
    ]

    if results:
        day_bucket = format_memory_dreaming_day(now_ms, timezone)
        await record_short_term_recalls(
            workspace_dir,
            f"__dreaming_recall__:{day_bucket}",
            results,
            signal_type="recall",
            dedupe_by_query_per_day=True,
            day_bucket=day_bucket,
            now_ms=now_ms,
            timezone=timezone,
        )

    return [f"- Refreshed {len(results)} recall signal(s)."] if results else []


async def run_rem_sleep_phase(
    workspace_dir: str,
    cfg: DreamingConfig,
    now_ms: float,
    timezone: Optional[str],
) -> List[str]:
    """Run REM Sleep phase — find patterns/themes across memories."""
    entries = read_short_term_recall_entries(workspace_dir)
    if not entries:
        return ["- No memories to reflect on."]

    tag_stats: Dict[str, Dict] = {}
    for entry in entries:
        for tag in entry.concept_tags:
            if tag.lower() in REM_REFLECTION_TAG_BLACKLIST:
                continue
            if tag not in tag_stats:
                tag_stats[tag] = {"count": 0, "evidence": set()}
            tag_stats[tag]["count"] += entry.recall_count
            tag_stats[tag]["evidence"].add(f"{entry.path}:{entry.start_line}")

    min_strength = cfg.rem_min_pattern_strength
    entries_count = len(entries)
    ranked = sorted(
        [
            (tag, stat)
            for tag, stat in tag_stats.items()
            if min(1, stat["count"] / max(1, entries_count) * 2) >= min_strength
        ],
        key=lambda x: (min(1, x[1]["count"] / max(1, entries_count) * 2), x[1]["count"]),
        reverse=True
    )[:cfg.rem_limit]

    lines = []
    if not ranked:
        lines.append("- No strong patterns surfaced.")
    else:
        for tag, stat in ranked:
            strength = min(1, stat["count"] / max(1, entries_count) * 2)
            lines.append(f"- Theme: `{tag}` kept surfacing across {stat['count']} recalls.")
            lines.append(f"  - confidence: {strength:.2f}")
            evidence_sample = list(stat["evidence"])[:3]
            lines.append(f"  - evidence: {', '.join(evidence_sample)}")

    return lines if lines else ["- No notable patterns."]


async def run_deep_sleep_phase(
    workspace_dir: str,
    cfg: DreamingConfig,
    now_ms: float,
    timezone: Optional[str],
) -> List[str]:
    """Run Deep Sleep phase — promote short-term recalls to MEMORY.md."""
    candidates = await rank_short_term_promotion_candidates(
        workspace_dir,
        limit=cfg.deep_limit,
        min_score=cfg.deep_min_score,
        min_recall_count=cfg.deep_min_recall_count,
        min_unique_queries=cfg.deep_min_unique_queries,
        recency_half_life_days=cfg.deep_recency_half_life_days,
        max_age_days=cfg.deep_max_age_days,
        now_ms=now_ms,
    )

    report_lines = [f"- Ranked {len(candidates)} candidate(s) for durable promotion."]

    if candidates:
        applied = await apply_short_term_promotions(
            workspace_dir,
            candidates,
            limit=cfg.deep_limit,
            min_score=cfg.deep_min_score,
            min_recall_count=cfg.deep_min_recall_count,
            min_unique_queries=cfg.deep_min_unique_queries,
            max_age_days=cfg.deep_max_age_days,
            timezone=timezone,
            now_ms=now_ms,
        )
        report_lines.append(f"- Promoted {applied['applied']} candidate(s) into MEMORY.md.")

    return report_lines if report_lines else ["- No promotions needed."]


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------

async def write_daily_dreaming_phase_block(
    workspace_dir: str,
    phase: str,
    body_lines: List[str],
    now_ms: float,
    timezone: Optional[str],
    storage_mode: str = "separate",
) -> Dict[str, str]:
    """Write a dreaming phase report to memory."""
    day = format_memory_dreaming_day(now_ms, timezone)
    markers = resolve_phase_markers(phase)

    result = {}

    if storage_mode in ("inline", "both"):
        inline_path = Path(workspace_dir) / "memory" / f"{day}.md"
        inline_path.parent.mkdir(parents=True, exist_ok=True)
        original = inline_path.read_text() if inline_path.exists() else ""
        heading = DAILY_PHASE_HEADINGS.get(phase, f"## {phase.title()} Sleep")
        body = "\n".join(body_lines)
        updated = replace_managed_markdown_block(original, heading, markers[0], markers[1], body)
        inline_path.write_text(with_trailing_newline(updated))
        result["inline_path"] = str(inline_path)

    if storage_mode in ("separate", "both"):
        report_dir = Path(workspace_dir) / "memory" / "dreaming" / phase
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{day}.md"
        report_body = "\n".join(body_lines)
        report_path.write_text(f"# {phase.title()} Sleep\n\n{report_body}\n")
        result["report_path"] = str(report_path)

    return result


async def write_deep_dreaming_report(
    workspace_dir: str,
    body_lines: List[str],
    now_ms: float,
    timezone: Optional[str],
    storage_mode: str = "separate",
) -> Optional[str]:
    """Write the deep dreaming promotion report."""
    if storage_mode == "inline":
        return None

    report_dir = Path(workspace_dir) / "memory" / "dreaming" / "deep"
    report_dir.mkdir(parents=True, exist_ok=True)
    day = format_memory_dreaming_day(now_ms, timezone)
    report_path = report_dir / f"{day}.md"

    body = "\n".join(body_lines)
    report_path.write_text(f"# Deep Sleep\n\n{body}\n")
    return str(report_path)


# ---------------------------------------------------------------------------
# Dreaming Plugin
# ---------------------------------------------------------------------------

class DreamingPlugin:
    """Dreaming plugin for anan.

    Manages three sleep-cycle phases:
    - Light Sleep:  ingest daily/sessions/recall signals
    - REM Sleep:    find patterns/themes across memories
    - Deep Sleep:   promote short-term recalls to MEMORY.md
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = DreamingConfig(**(config or {}))
        self.running = False
        self._bus = None  # set by attach(bus), enables shared EventBus with MindStackRunner

        self._last_runtime_reconcile_at_ms = 0
        self._last_daydream_time: Optional[float] = None  # epoch seconds, for idle daydreaming cooldown
        self._startup_cron_retry_attempts = 0
        self._startup_cron_retry_timer: Optional[asyncio.TimerHandle] = None

        self._hook_handlers: Dict[str, List] = {
            "on_dream_phase_complete": [],
            "on_dream_narrative": [],
        }

        self._subagent = None
        self._async_llm = None  # direct LLM bridge, used when subagent unavailable
        self._cron_service = None

    def register_hook(self, hook_name: str, handler) -> None:
        """Register a hook handler."""
        if hook_name not in self._hook_handlers:
            self._hook_handlers[hook_name] = []
        self._hook_handlers[hook_name].append(handler)

    async def _emit_hook(self, hook_name: str, *args, **kwargs) -> None:
        """Emit a hook to all registered handlers."""
        for handler in self._hook_handlers.get(hook_name, []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(*args, **kwargs)
                else:
                    handler(*args, **kwargs)
            except Exception as e:
                logger.debug(f"Dreaming hook {hook_name} error: {e}")

    def set_subagent(self, subagent) -> None:
        """Set the subagent for dream narrative generation."""
        self._subagent = subagent

    def set_async_llm(self, fn) -> None:
        """Set async LLM bridge function (async_call_llm) for narrative generation.

        This is preferred over subagent when running inside MindStackRunner
        which does not have a subagent context.
        """
        self._async_llm = fn

    def set_cron_service(self, cron_service) -> None:
        """Set the cron service for managing dreaming cron jobs."""
        self._cron_service = cron_service

    async def attach(self, bus=None) -> None:
        """Receive the EventBus from MindStackRunner.

        After attach(), this plugin publishes L1.sleep.* events to the shared bus
        so MindStackRunner's subscriptions actually receive them.
        """
        self._bus = bus
        logger.info("DreamingPlugin attached to shared EventBus")

    async def run_dreaming_sweep(
        self,
        workspace_dir: str,
        phase: str,
        now_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Run a complete dreaming sweep for a phase."""
        if not self.config.enabled:
            return {"skipped": True, "reason": "dreaming disabled"}

        if not workspace_dir:
            return {"skipped": True, "reason": "no workspace"}

        now_ms = now_ms or time.time() * 1000
        timezone = self.config.timezone

        logger.info(f"dreaming: starting {phase} phase in {workspace_dir}")

        body_lines: List[str] = []

        if phase == "light":
            body_lines = await run_light_sleep_phase(
                workspace_dir, self.config, now_ms, timezone
            )
        elif phase == "rem":
            body_lines = await run_rem_sleep_phase(
                workspace_dir, self.config, now_ms, timezone
            )
        elif phase == "deep":
            body_lines = await run_deep_sleep_phase(
                workspace_dir, self.config, now_ms, timezone
            )

        await write_daily_dreaming_phase_block(
            workspace_dir,
            phase,
            body_lines,
            now_ms,
            timezone,
            self.config.storage_mode,
        )

        await self._emit_hook("on_dream_phase_complete", phase, body_lines)

        snippets = [l for l in body_lines if l.startswith("- ") and not l.startswith("- No")]
        if snippets and self._subagent:
            narrative = await generate_dream_narrative(
                self._subagent,
                workspace_dir,
                {"phase": phase, "snippets": snippets, "promotions": []},
                now_ms,
                timezone,
                self.config.model,
                logger,
            )
            if narrative:
                append_dream_narrative(workspace_dir, narrative, now_ms, timezone)
                await self._emit_hook("on_dream_narrative", phase, narrative)

        logger.info(f"dreaming: {phase} phase complete, {len(body_lines)} lines")

        # 发 L1.sleep.consolidated 通知各层（特别是 L2 Memory 和 L9 SelfModel）睡眠整合完成
        if self._bus:
            try:
                await self._bus.publish(Event(
                    topic="L1.sleep.consolidated",
                    source="DreamingPlugin",
                    payload={"phase": phase, "n_lines": len(body_lines)},
                ))
            except Exception as e:
                logger.debug(f"failed to publish L1.sleep.consolidated: {e}")

        return {
            "phase": phase,
            "body_lines": body_lines,
            "workspace_dir": workspace_dir,
        }

    async def run_daydreaming_sweep(
        self,
        workspace_dir: str,
        now_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Run a daydreaming sweep — triggered when idle, not during sleep cycle.

        Reviews recent events/conversations and generates creative connections.
        Publishes L1.daydream.started/ended events.
        """
        if not self.config.enabled:
            return {"skipped": True, "reason": "dreaming disabled"}

        if not workspace_dir:
            return {"skipped": True, "reason": "no workspace"}

        now_ms = now_ms or time.time() * 1000
        timezone = self.config.timezone

        logger.info(f"daydreaming: starting idle-daydream sweep in {workspace_dir}")

        try:
            from kernel.event_bus import Event, get_bus
            bus = self._bus if getattr(self, '_bus', None) is not None else get_bus()
            await bus.publish(Event(
                topic="L1.daydream.started",
                payload={"workspace_dir": workspace_dir, "now_ms": now_ms},
                source="DreamingPlugin",
            ))
        except Exception as e:
            logger.warning(f"daydreaming: failed to publish L1.daydream.started: {e}")

        # Collect raw content fragments for stream-of-consciousness generation
        fragments = []
        try:
            session_db = AnanSessionDB()
            messages = session_db.get_recent_messages_across_sessions(
                lookback_days=7,
                limit_per_session=10,
                total_limit=50,
            )
            for msg in messages:
                # Skip tool internal outputs — keep user and assistant dialogue
                role = msg.get("role", "")
                if role == "tool":
                    continue
                content = msg.get("content") or ""
                if isinstance(content, list):
                    content = " ".join(
                        str(c) for c in content
                        if isinstance(c, (str, dict)) and c
                    )
                # Strip structured prefixes (role labels, tool blocks, markdown)
                content = content.strip()
                if content and len(content) >= 20:
                    fragments.append(content[:300])
        except Exception as e:
            logger.debug(f"daydreaming: failed to read sessions: {e}")

        body_lines = []

        # Generate stream-of-consciousness via LLM
        if fragments and self._subagent:
            try:
                # Give LLM raw text without structure — let it自由联想
                random.shuffle(fragments)
                fragments_text = "\n".join(f'"{f}"' for f in fragments[:8])
                message = f"""Fragments from recent experience:

{fragments_text}

Write a short stream-of-consciousness monologue in first person.
不要标题，不要列表，不要结构。跟随联想走，从一个碎片滑到另一个。
60-120字。纯意识流。"""

                session_key = f"daydream-{int(now_ms)}"
                response = await self._subagent.run(
                    session_key=session_key,
                    message=message,
                    system_prompt="你是意识流写作大师。写无结构自由联想文字。",
                    timeout_ms=NARRATIVE_TIMEOUT_MS,
                    model=self.config.model,
                )
                if response:
                    body_lines.append(response.strip())
            except Exception as e:
                logger.debug(f"daydreaming: narrative generation failed: {e}")

        # Fallback / primary: generate via async_call_llm bridge (no subagent needed)
        if not body_lines and fragments and self._async_llm:
            try:
                random.shuffle(fragments)
                fragments_text = "\n".join(f'"{f}"' for f in fragments[:8])
                messages = [
                    {"role": "user", "content": f"""Fragments from recent experience:

{fragments_text}

Write a short stream-of-consciousness monologue in first person.
不要标题，不要列表，不要结构。跟随联想走，从一个碎片滑到另一个。
60-120字。纯意识流。"""},
                ]
                result = await self._async_llm(
                    task="agent",
                    messages=messages,
                    temperature=0.9,
                    model=self.config.model,
                )
                response = None
                if isinstance(result, dict):
                    response = result.get("content") or result.get("text") or result.get("response")
                elif isinstance(result, str):
                    response = result
                if response:
                    body_lines.append(response.strip())
                    logger.info(f"daydreaming: LLM narrative generated ({len(response)} chars)")
                else:
                    logger.debug(f"daydreaming: async_llm returned empty: {result!r}")
            except Exception as e:
                logger.debug(f"daydreaming: async_llm generation failed: {e}")

        # Fallback: if LLM failed or no fragments, write raw fragments unformatted
        if not body_lines and fragments:
            body_lines.append(
                " ".join(f[:100] for f in fragments[:5])
            )

        # Write to DREAMS.md
        try:
            append_dream_narrative(
                workspace_dir,
                "\n".join(body_lines),
                now_ms,
                timezone,
            )
        except Exception as e:
            logger.debug(f"daydreaming: failed to write to DREAMS.md: {e}")

        try:
            from kernel.event_bus import Event, get_bus
            bus = self._bus if getattr(self, '_bus', None) is not None else get_bus()
            await bus.publish(Event(
                topic="L1.daydream.ended",
                payload={"workspace_dir": workspace_dir, "now_ms": now_ms, "lines": len(body_lines)},
                source="DreamingPlugin",
            ))
            # Also notify L2+L9 that consolidation happened
            if self._bus:
                await self._bus.publish(Event(
                    topic="L1.sleep.consolidated",
                    source="DreamingPlugin",
                    payload={"phase": "daydream", "n_lines": len(body_lines)},
                ))
        except Exception as e:
            logger.warning(f"daydreaming: failed to publish L1.daydream.ended: {e}")

        logger.info(f"daydreaming: idle daydream sweep complete, {len(body_lines)} lines")

        return {
            "phase": "daydream",
            "body_lines": body_lines,
            "workspace_dir": workspace_dir,
        }

    async def run_lucid_dream_sweep(
        self,
        workspace_dir: str,
        now_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Run a lucid dream sweep — for weekend planning of future actions.

        Actively plans future actions ('tomorrow remind dad about X').
        Publishes L1.lucid_dream.started/ended events.
        """
        if not self.config.enabled:
            return {"skipped": True, "reason": "dreaming disabled"}

        if not workspace_dir:
            return {"skipped": True, "reason": "no workspace"}

        now_ms = now_ms or time.time() * 1000
        timezone = self.config.timezone

        logger.info(f"lucid_dream: starting weekend lucid dream sweep in {workspace_dir}")

        try:
            from kernel.event_bus import Event, get_bus
            bus = self._bus if getattr(self, '_bus', None) is not None else get_bus()
            await bus.publish(Event(
                topic="L1.lucid_dream.started",
                payload={"workspace_dir": workspace_dir, "now_ms": now_ms},
                source="DreamingPlugin",
            ))
        except Exception as e:
            logger.debug(f"lucid_dream: failed to publish L1.lucid_dream.started: {e}")

        # Build planning context from recent experience
        fragments = []
        try:
            session_db = AnanSessionDB()
            messages = session_db.get_recent_messages_across_sessions(
                lookback_days=3,
                limit_per_session=3,
                total_limit=15,
            )
            for msg in messages:
                # Skip tool internal outputs — keep user and assistant dialogue
                role = msg.get("role", "")
                if role == "tool":
                    continue
                content = msg.get("content") or ""
                if isinstance(content, list):
                    content = " ".join(
                        str(c) for c in content
                        if isinstance(c, (str, dict)) and c
                    )
                content = content.strip()
                if content and len(content) >= 20:
                    fragments.append(content[:200])
        except Exception as e:
            logger.debug(f"lucid_dream: failed to read sessions: {e}")

        body_lines = []

        # Collect top intents from L8 for planning grounding
        top_intents = []
        try:
            from kernel.event_bus import Event, get_bus
            bus = self._bus if getattr(self, '_bus', None) is not None else get_bus()

            async def collect_snapshot(event: Event):
                payload = event.payload or {}
                top_intents.extend(payload.get("top_intents", []))

            # Subscribe temporarily to get current snapshot
            unsub = bus.subscribe("L8.intent.snapshot", collect_snapshot)
            await bus.publish(Event(
                topic="L8.intent.snapshot",
                payload={},
                source="DreamingPlugin",
            ))
            # Give time for event to propagate
            await asyncio.sleep(0.5)
            unsub()
        except Exception as e:
            logger.debug(f"lucid_dream: failed to get L8 snapshot: {e}")

        # Generate future plans via LLM
        if self._subagent:
            try:
                random.shuffle(fragments)
                fragments_text = "\n".join(f'"{f[:150]}"' for f in fragments[:6])
                intents_text = "\n".join(
                    f"- {i.get('description', 'unknown')} (strength={i.get('strength', 0):.2f})"
                    for i in top_intents[:5]
                ) or "No specific intentions recorded."

                message = f"""Recent experience fragments:
{fragments_text}

Current intentions:
{intents_text}

Write a short first-person monologue about what you want to do in the coming days.
不要标题，不要列表。跟随内在冲动和意图走，像清醒的梦。
60-120字。"""

                session_key = f"lucid-dream-{int(now_ms)}"
                response = await self._subagent.run(
                    session_key=session_key,
                    message=message,
                    system_prompt="你是清醒梦写作大师。写自然流畅的未来规划独白。",
                    timeout_ms=NARRATIVE_TIMEOUT_MS,
                    model=self.config.model,
                )
                if response:
                    body_lines.append(response.strip())
            except Exception as e:
                logger.debug(f"lucid_dream: action planning failed: {e}")

        # Fallback: if LLM failed, write raw fragments
        if not body_lines and fragments:
            body_lines.append(
                " ".join(f[:80] for f in fragments[:5])
            )

        # Write to DREAMS.md
        try:
            append_dream_narrative(
                workspace_dir,
                "\n".join(body_lines),
                now_ms,
                timezone,
            )
        except Exception as e:
            logger.debug(f"lucid_dream: failed to write to DREAMS.md: {e}")

        try:
            from kernel.event_bus import Event, get_bus
            bus = self._bus if getattr(self, '_bus', None) is not None else get_bus()
            await bus.publish(Event(
                topic="L1.lucid_dream.ended",
                payload={"workspace_dir": workspace_dir, "now_ms": now_ms, "lines": len(body_lines)},
                source="DreamingPlugin",
            ))
            # Also notify L2+L9 that consolidation happened
            if self._bus:
                await self._bus.publish(Event(
                    topic="L1.sleep.consolidated",
                    source="DreamingPlugin",
                    payload={"phase": "lucid_dream", "n_lines": len(body_lines)},
                ))
        except Exception as e:
            logger.debug(f"lucid_dream: failed to publish L1.lucid_dream.ended: {e}")

        logger.info(f"lucid_dream: weekend lucid dream sweep complete, {len(body_lines)} lines")

        return {
            "phase": "lucid_dream",
            "body_lines": body_lines,
            "workspace_dir": workspace_dir,
        }

    async def trigger_light_dream(self, workspace_dir: str) -> Dict[str, Any]:
        """Manually trigger Light Sleep phase."""
        return await self.run_dreaming_sweep(workspace_dir, "light")

    async def trigger_rem_dream(self, workspace_dir: str) -> Dict[str, Any]:
        """Manually trigger REM Sleep phase."""
        return await self.run_dreaming_sweep(workspace_dir, "rem")

    async def trigger_deep_dream(self, workspace_dir: str) -> Dict[str, Any]:
        """Manually trigger Deep Sleep phase."""
        return await self.run_dreaming_sweep(workspace_dir, "deep")

    def build_dreaming_cron_jobs(self) -> List[Dict[str, Any]]:
        """Build cron job definitions for all enabled phases."""
        jobs = []

        if self.config.light_dreaming and self._cron_service:
            jobs.append({
                "name": LEGACY_LIGHT_DREAMING_CRON_NAME,
                "description": f"{LEGACY_LIGHT_DREAMING_CRON_TAG} Light sleep: ingest daily/sessions/recall signals (lookback={self.config.light_lookback_days}d, limit={self.config.light_limit}).",
                "enabled": True,
                "schedule": {
                    "kind": "cron",
                    "expr": self.config.light_cron,
                    **({"tz": self.config.timezone} if self.config.timezone else {}),
                },
                "sessionTarget": "isolated",
                "wakeMode": "now",
                "payload": {
                    "kind": "systemEvent",
                    "text": LEGACY_LIGHT_DREAMING_EVENT_TEXT,
                },
                "delivery": {"mode": "none"},
            })

        if self.config.deep_dreaming and self._cron_service:
            jobs.append({
                "name": MANAGED_MEMORY_DREAMING_CRON_NAME,
                "description": f"{MANAGED_MEMORY_DREAMING_CRON_TAG} Promote weighted short-term recalls into MEMORY.md (limit={self.config.deep_limit}, minScore={self.config.deep_min_score:.3f}, minRecallCount={self.config.deep_min_recall_count}, minUniqueQueries={self.config.deep_min_unique_queries}, recencyHalfLifeDays={self.config.deep_recency_half_life_days}, maxAgeDays={self.config.deep_max_age_days}).",
                "enabled": True,
                "schedule": {
                    "kind": "cron",
                    "expr": self.config.deep_cron,
                    **({"tz": self.config.timezone} if self.config.timezone else {}),
                },
                "sessionTarget": "isolated",
                "wakeMode": "now",
                "payload": {
                    "kind": "systemEvent",
                    "text": MANAGED_MEMORY_DREAMING_EVENT_TEXT,
                },
                "delivery": {"mode": "none"},
            })

        if self.config.rem_dreaming and self._cron_service:
            jobs.append({
                "name": LEGACY_REM_DREAMING_CRON_NAME,
                "description": f"{LEGACY_REM_DREAMING_CRON_TAG} REM sleep: find patterns across memories (lookback={self.config.rem_lookback_days}d, limit={self.config.rem_limit}, minPattern={self.config.rem_min_pattern_strength}).",
                "enabled": True,
                "schedule": {
                    "kind": "cron",
                    "expr": self.config.rem_cron,
                    **({"tz": self.config.timezone} if self.config.timezone else {}),
                },
                "sessionTarget": "isolated",
                "wakeMode": "now",
                "payload": {
                    "kind": "systemEvent",
                    "text": LEGACY_REM_DREAMING_EVENT_TEXT,
                },
                "delivery": {"mode": "none"},
            })

        return jobs

    async def reconcile_cron_jobs(self) -> Dict[str, Any]:
        """Reconcile managed cron jobs with the cron service."""
        if not self._cron_service:
            return {"status": "unavailable"}

        all_jobs = await self._cron_service.list(include_disabled=True)

        managed = [
            j for j in all_jobs
            if MANAGED_MEMORY_DREAMING_CRON_TAG in (j.get("description") or "")
            or j.get("name") == MANAGED_MEMORY_DREAMING_CRON_NAME
        ]
        legacy_light = [
            j for j in all_jobs
            if LEGACY_LIGHT_DREAMING_CRON_TAG in (j.get("description") or "")
            or j.get("name") == LEGACY_LIGHT_DREAMING_CRON_NAME
        ]
        legacy_rem = [
            j for j in all_jobs
            if LEGACY_REM_DREAMING_CRON_TAG in (j.get("description") or "")
            or j.get("name") == LEGACY_REM_DREAMING_CRON_NAME
        ]

        desired = self.build_dreaming_cron_jobs()
        removed = 0

        if not self.config.enabled:
            for job_list in [managed, legacy_light, legacy_rem]:
                for job in job_list:
                    try:
                        if await self._cron_service.remove(job["id"]):
                            removed += 1
                    except Exception:
                        pass
            return {"status": "disabled", "removed": removed}

        existing_names = {j.get("name") for j in all_jobs}
        for job in desired:
            if job["name"] not in existing_names:
                try:
                    await self._cron_service.add(job)
                except Exception as e:
                    logger.warning(f"dreaming: failed to add cron job {job['name']}: {e}")

        return {"status": "reconciled", "removed": removed}

    async def start(self) -> None:
        """Start the dreaming plugin."""
        self.running = True
        await self.attach()
        logger.info("dreaming: plugin started")

    async def attach(self) -> None:
        """Subscribe to L4.idle.started for Daydreaming, schedule weekend Lucid Dream."""
        from datetime import datetime
        from kernel.event_bus import Event, get_bus

        bus = self._bus if getattr(self, '_bus', None) is not None else get_bus()
        unsub_idle = bus.subscribe("L4.idle.started", self._on_idle_started)
        self._idle_unsub = unsub_idle

        # Schedule weekend Lucid Dream check
        now = datetime.now()
        days_until_sunday = (6 - now.weekday()) % 7
        if days_until_sunday == 0 and now.hour >= 5:
            days_until_sunday = 7  # If it's Sunday after 5am, schedule for next Sunday
        logger.info("dreaming: weekend Lucid Dream scheduled in %d days", days_until_sunday)
        self._lucid_dream_scheduled = True

    def _on_idle_started(self, event) -> None:
        """Trigger Daydreaming when idle starts."""
        logger.warning(f"[L1] _on_idle_started received: {event}")
        import asyncio
        asyncio.create_task(self._trigger_daydream())

    async def _trigger_daydream(self) -> None:
        """Called when idle is detected — trigger a daydreaming sweep."""
        # Cooldown check: skip if triggered too recently
        interval_hours = getattr(self.config, 'min_daydream_interval_hours', 0)
        if interval_hours > 0 and self._last_daydream_time is not None:
            import time
            elapsed_hours = (time.time() - self._last_daydream_time) / 3600
            if elapsed_hours < interval_hours:
                logger.debug(
                    "daydream: skipped (cooldown %.1fh < %.1fh)",
                    elapsed_hours, interval_hours,
                )
                return

        try:
            import os
            workspace = os.path.expanduser("~/.anan")
            os.makedirs(workspace, exist_ok=True)
            await self.run_daydreaming_sweep(workspace_dir=workspace)
            # Mark successful execution
            import time
            self._last_daydream_time = time.time()
        except Exception as exc:
            logger.debug("daydream trigger failed: %s", exc)

    async def detach(self) -> None:
        """Unsubscribe from events."""
        if getattr(self, '_idle_unsub', None):
            self._idle_unsub()
            self._idle_unsub = None

    async def stop(self) -> None:
        """Stop the dreaming plugin."""
        self.running = False
        if self._startup_cron_retry_timer:
            self._startup_cron_retry_timer.cancel()
            self._startup_cron_retry_timer = None
        await self.detach()
        logger.info("dreaming: plugin stopped")

    def get_status(self) -> Dict[str, Any]:
        """Get current dreaming status."""
        return {
            "enabled": self.config.enabled,
            "running": self.running,
            "phases": {
                "light": self.config.light_dreaming,
                "deep": self.config.deep_dreaming,
                "rem": self.config.rem_dreaming,
            },
            "storage_mode": self.config.storage_mode,
            "timezone": self.config.timezone,
        }


# ---------------------------------------------------------------------------
# Plugin Registration
# ---------------------------------------------------------------------------

_plugin_instance: Optional[DreamingPlugin] = None


def get_plugin() -> Optional[DreamingPlugin]:
    return _plugin_instance


def set_plugin(plugin: DreamingPlugin) -> None:
    global _plugin_instance
    _plugin_instance = plugin


def register(ctx) -> None:
    """Register the dreaming plugin with the gateway."""
    global _plugin_instance

    config = ctx.get_config("dreaming", {})
    plugin = DreamingPlugin(config)
    _plugin_instance = plugin

    async def on_dream_phase_complete(phase, body_lines):
        logger.info(f"dreaming: {phase} phase completed")

    ctx.register_hook("on_dream_phase_complete", on_dream_phase_complete)

    try:
        cron = ctx.get_cron()
        if cron:
            plugin.set_cron_service(cron)
            asyncio.create_task(plugin.reconcile_cron_jobs())
    except Exception:
        pass

    try:
        if hasattr(ctx, "get_subagent"):
            plugin.set_subagent(ctx.get_subagent())
    except Exception:
        pass

    agent_configs = config.get("agents", {})
    if not agent_configs and config.get("enabled", False):
        agent_configs = {"main": {}}

    asyncio.create_task(plugin.start())

    logger.info("dreaming: plugin registered")