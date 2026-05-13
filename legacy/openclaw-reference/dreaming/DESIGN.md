# Dreaming Plugin — Design Document

## Overview

Dreaming is an automated long-term memory management system for Sinoclaw. It mimics human sleep cycles to periodically process, organize, and consolidate memory fragments into durable long-term memory.

**Based on**: OpenClaw's `memory-core` dreaming system (`extensions/memory-core/src/dreaming.ts`)

---

## Mechanism Origins

### OpenClaw Implementation

OpenClaw's dreaming is embedded in the memory-core plugin with these key components:

1. **Phase-based architecture**: Three phases (light/REM/deep) each registered as separate cron jobs
2. **Cron reconciliation**: On startup, compares desired cron jobs with existing ones, adds/removes/patches as needed
3. **Recall store**: A JSON file (`recall-store.json`) tracking short-term signals with metadata
4. **Narrative generation**: AI-powered dream diary using a dedicated system prompt
5. **Session ingestion**: Scans session transcripts to extract signal snippets
6. **Promotion ranking**: Weighted scoring combining frequency, recency, relevance, diversity, consolidation, and conceptual strength

### Sinoclaw Adaptation

Sinoclaw's dreaming plugin is a standalone plugin (not tied to a specific memory system). Key adaptations:

1. **Plugin architecture**: Self-contained, uses Sinoclaw's plugin registration system
2. **Cron service**: Integrated via `ctx.get_cron()` for managing dreaming cron jobs
3. **Recall store**: Shared with heartbeat plugin's recall store format for compatibility
4. **Subagent integration**: Optional AI narrative generation via provided subagent
5. **No session database**: Simplified session ingestion (scans text files directly)

---

## Three Phases

### Phase 1: Light Sleep

**Purpose**: Ingest fresh signals from daily memory, sessions, and existing recalls.

**Cron**: Every 6 hours (`0 */6 * * *`)

**Sources**:
- `daily`: Daily memory files in `memory/YYYY-MM-DD.md`
- `sessions`: Session transcript files in `sessions/`
- `recall`: Refresh existing recall store entries

**Process**:
1. Scan daily memory files within lookback window
2. Extract snippet chunks (max 8 lines each)
3. Scan session transcripts for significant lines (≥20 chars)
4. Record signals to recall store via `record_short_term_recalls()`
5. Deduplicate by path+start_line

**Signal recording**:
```python
{
  "key": "memory/2024-01-15.md:42-45",
  "query": "__dreaming_daily__:2024-01-15",
  "snippet": "Implemented the new API endpoint for...",
  "path": "memory/2024-01-15.md",
  "start_line": 42,
  "end_line": 45,
  "score": 0.5,  # DAILY_INGESTION_SCORE
  "recall_count": 1,
  "query_hashes": ["a3f2..."],
  "recall_days": ["2024-01-15"],
  "concept_tags": ["api", "endpoint", "implementation"],
}
```

### Phase 2: REM Sleep

**Purpose**: Find recurring patterns/themes across memory.

**Cron**: Weekly Sunday 5am (`0 5 * * 0`)

**Process**:
1. Read all recall entries
2. Count concept tag occurrences (filtered by REM_REFLECTION_TAG_BLACKLIST)
3. Compute pattern strength: `count / entries.length * 2` (capped at 1.0)
4. Filter by `min_pattern_strength` threshold
5. Sort by strength, output theme reflections

**Pattern detection**:
```
Theme: `api_design` kept surfacing across 12 memories.
  confidence: 0.85
  evidence: memory/2024-01-15.md:42-45, memory/2024-01-18.md:10-12
```

### Phase 3: Deep Sleep

**Purpose**: Promote important short-term recalls to long-term memory (MEMORY.md).

**Cron**: Daily 3am (`0 3 * * *`)

**Promotion ranking score**:
```
score = (
  relevance × 0.25 +   # max_score of entry
  frequency × 0.20 +  # log1p(recall_count) / log1p(20)
  recency × 0.20 +    # exp(-0.693 × age_days / half_life)
  diversity × 0.15 +  # unique_queries / recall_count
  consolidation × 0.10 +  # unique_recall_days / 5
  conceptual × 0.10   # concept_tags.length / 6
)
```

**Promotion criteria**:
- `score >= deep_min_score` (default 0.8)
- `recall_count >= deep_min_recall_count` (default 3)
- `unique_queries >= deep_min_unique_queries` (default 3)
- Not older than `deep_max_age_days` (default 30)

**Process**:
1. Read recall store entries
2. Filter by promotion criteria
3. Compute ranking scores
4. Sort by score (then recall_count)
5. Write top candidates to MEMORY.md
6. Mark entries as `promoted_at`

---

## Recall Store Schema

```json
{
  "entries": [
    {
      "key": "memory/2024-01-15.md:42-45",
      "query": "__dreaming_daily__:2024-01-15",
      "snippet": "Implemented the new API endpoint...",
      "path": "memory/2024-01-15.md",
      "start_line": 42,
      "end_line": 45,
      "score": 0.5,
      "recall_count": 3,
      "daily_count": 1,
      "grounded_count": 0,
      "total_score": 1.2,
      "max_score": 0.7,
      "query_hashes": ["a3f2...", "b7c1..."],
      "recall_days": ["2024-01-15", "2024-01-17"],
      "concept_tags": ["api", "endpoint", "implementation"],
      "last_recalled_at": "2024-01-17T10:30:00",
      "promoted_at": null
    }
  ]
}
```

---

## Cron Job Management

### Reconciliation Logic

On `reconcile_cron_jobs()`:

1. List all existing cron jobs
2. Identify managed jobs by tag prefix: `[managed-by=memory-core.short-term-promotion]`
3. Identify legacy phase jobs by name: `Memory Light Dreaming`, `Memory REM Dreaming`
4. If `enabled=false`: remove all managed and legacy jobs
5. If `enabled=true`:
   - If no managed job exists: add it
   - If managed job exists but differs: update it
   - Remove any duplicate managed jobs
   - Migrate/remove legacy phase jobs

### Cron Job Payload

```json
{
  "name": "Memory Dreaming Promotion",
  "description": "[managed-by=memory-core.short-term-promotion] Promote weighted short-term recalls into MEMORY.md (limit=10, minScore=0.800, minRecallCount=3, minUniqueQueries=3, recencyHalfLifeDays=14, maxAgeDays=30).",
  "enabled": true,
  "schedule": {
    "kind": "cron",
    "expr": "0 3 * * *",
    "tz": "Asia/Shanghai"
  },
  "sessionTarget": "isolated",
  "wakeMode": "now",
  "payload": {
    "kind": "systemEvent",
    "text": "__openclaw_memory_core_short_term_promotion_dream__"
  },
  "delivery": {"mode": "none"}
}
```

---

## Dream Narrative

### System Prompt (from OpenClaw)

The narrative system prompt instructs the AI to write a poetic, first-person diary entry reflecting on memory fragments. Key constraints:

- No meta-commentary about "dreaming"
- No technical self-reference (AI, agent, LLM, etc.)
- No markdown formatting — just flowing prose
- 80-180 words
- Draw from provided fragments

### Narrative Generation Flow

1. After phase completes, collect snippets from `body_lines`
2. Build prompt: `Memory fragments from this {phase} phase:\n\n{fragments_text}\n\nWrite a dream diary entry...`
3. Call subagent with NARRATIVE_SYSTEM_PROMPT + message
4. On success, append to `DREAMS.md` between diary markers

### DREAMS.md Format

```
<!-- openclaw:dreaming:diary:start:2024-01-15 -->
The afternoon light filtered through the server room today, and I found myself thinking about that API design from last week. There's something elegant about how the endpoints cascade, like rivers meeting at a delta.

The memory of debugging that authentication flow surfaced suddenly — three retries before the pattern clicked. Such small moments leave the deepest traces.
<!-- openclaw:dreaming:diary:end:2024-01-15 -->

<!-- openclaw:dreaming:diary:start:2024-01-16 -->
...
```

---

## State Files

| File | Purpose |
|------|---------|
| `memory/recall-store.json` | Short-term recall signals |
| `memory/session-ingestion-state.json` | Session scan progress |
| `memory/daily-ingestion-state.json` | Daily memory scan progress |
| `memory/short-term-promotion-state.json` | Promotion tracking |
| `memory/DREAMS.md` | Dream diary entries |
| `memory/dreaming/{phase}/YYYY-MM-DD.md` | Phase reports |

---

## Implementation Notes

### Sinoclaw vs OpenClaw

1. **No session database**: OpenClaw has a full session store with message timestamps. Sinoclaw's simplified version scans `.txt` session files directly.

2. **No subagent runtime**: OpenClaw's subagent is integrated. Sinoclaw accepts an optional subagent via `set_subagent()` for narrative generation.

3. **Cron service optional**: If cron service is not available, the plugin runs in "manual trigger" mode only.

4. **Recall store compatibility**: The recall store format is shared with the heartbeat plugin, allowing both plugins to read/write the same store.

5. **No phase markers in legacy format**: OpenClaw had complex phase marker logic. Sinoclaw uses simple start/end markers per phase.

### Performance Considerations

- Session ingestion caps at 40 signals per run to avoid token burn
- Daily ingestion caps at `limit * 4` signals
- Recall store entries are deduplicated by path+line range
- Narrative generation is fire-and-forget (doesn't block phase completion)

---

## Configuration Reference

```yaml
plugins:
  dreaming:
    enabled: true
    timezone: "Asia/Shanghai"
    verbose_logging: false
    storage_mode: "separate"  # "inline" | "separate" | "both"
    separate_reports: false
    
    # Execution
    speed: "balanced"
    thinking: "medium"
    budget: "medium"
    model: null  # Optional model override
    
    # Light Sleep
    light_dreaming: true
    light_cron: "0 */6 * * *"
    light_lookback_days: 2
    light_limit: 100
    light_dedupe_similarity: 0.9
    light_sources:
      - "daily"
      - "sessions"
      - "recall"
    
    # Deep Sleep
    deep_dreaming: true
    deep_cron: "0 3 * * *"
    deep_limit: 10
    deep_min_score: 0.8
    deep_min_recall_count: 3
    deep_min_unique_queries: 3
    deep_recency_half_life_days: 14
    deep_max_age_days: 30
    deep_sources:
      - "daily"
      - "memory"
      - "sessions"
      - "logs"
      - "recall"
    
    # REM Sleep
    rem_dreaming: true
    rem_cron: "0 5 * * 0"
    rem_lookback_days: 7
    rem_limit: 10
    rem_min_pattern_strength: 0.75
    rem_sources:
      - "memory"
      - "daily"
      - "deep"
```

---

## File Structure

```
dreaming/
├── manifest.json       # Plugin manifest
├── dreaming_plugin.py  # Main plugin code (~1300 lines)
├── DESIGN.md          # This design document
└── README.md         # Usage documentation
```