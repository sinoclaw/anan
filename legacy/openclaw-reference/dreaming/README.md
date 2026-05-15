# Dreaming Plugin for anan

Automated long-term memory management system — mimics human sleep cycles to organize and consolidate memory over time.

Based on OpenClaw's `memory-core` dreaming system.

## Overview

The dreaming plugin runs periodically in the background, processing and consolidating memory across three sleep-cycle phases:

| Phase | Schedule | Purpose |
|-------|----------|---------|
| **Light Sleep** | Every 6h | Ingest daily/sessions/recall signals into short-term memory |
| **REM Sleep** | Weekly | Find patterns/themes across memories |
| **Deep Sleep** | Daily 3am | Promote weighted short-term recalls into `MEMORY.md` |

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Dreaming Plugin                    │
├─────────────────────────────────────────────────────┤
│  DreamingPlugin                                      │
│  ├── Three phase runners:                           │
│  │   ├── run_light_sleep_phase()                   │
│  │   ├── run_rem_sleep_phase()                      │
│  │   └── run_deep_sleep_phase()                     │
│  │                                                  │
│  ├── Short-term recall store:                       │
│  │   ├── record_short_term_recalls()               │
│  │   ├── read_short_term_recall_entries()          │
│  │   └── rank_short_term_promotion_candidates()    │
│  │                                                  │
│  ├── Narrative generation:                          │
│  │   ├── generate_dream_narrative()                │
│  │   └── append_dream_narrative()                  │
│  │                                                  │
│  └── Cron reconciliation:                           │
│      ├── build_dreaming_cron_jobs()                 │
│      └── reconcile_cron_jobs()                      │
└─────────────────────────────────────────────────────┘
```

## Memory Flow

```
Sessions / Daily Memory / Recall Store
        │
        ▼
  ┌─────────────┐
  │  Light Sleep │  →  Ingest signals → recall store
  └─────────────┘
        │
        ▼
  ┌─────────────┐
  │   REM Sleep  │  →  Find patterns → theme reflections
  └─────────────┘
        │
        ▼
  ┌─────────────┐
  │  Deep Sleep  │  →  Rank candidates → promote to MEMORY.md
  └─────────────┘
        │
        ▼
    DREAMS.md  (diary narrative)
```

## Installation

```bash
cp -r /data/plugins/dreaming /data/anan/plugins/
```

Add to `config.yaml`:

```yaml
plugins:
  dreaming:
    enabled: true
    timezone: "Asia/Shanghai"
    storage_mode: "separate"  # "inline" | "separate" | "both"
    
    light_dreaming: true
    light_cron: "0 */6 * * *"  # Every 6 hours
    light_lookback_days: 2
    light_limit: 100
    
    deep_dreaming: true
    deep_cron: "0 3 * * *"    # Daily 3am
    deep_limit: 10
    deep_min_score: 0.8
    deep_min_recall_count: 3
    deep_min_unique_queries: 3
    
    rem_dreaming: true
    rem_cron: "0 5 * * 0"     # Weekly Sunday 5am
    rem_lookback_days: 7
    rem_limit: 10
    rem_min_pattern_strength: 0.75
```

## Phase Details

### Light Sleep

Ingests signals from three sources:
- **daily**: Daily memory files (`memory/YYYY-MM-DD.md`)
- **sessions**: Session transcripts (`sessions/`)
- **recall**: Existing recall store entries

Signals are stored in `memory/recall-store.json` with metadata:
- `recall_count`: How many times recalled
- `query_hashes`: Unique queries that referenced it
- `recall_days`: Days when it was recalled
- `concept_tags`: Extracted concept tags

### REM Sleep

Analyzes recall store to find recurring themes:
- Counts concept tag occurrences across entries
- Filters out blacklisted tags (agent, plugin, etc.)
- Generates reflection lines for strong patterns
- Example output:
  ```
  - Theme: `api_design` kept surfacing across 12 recalls.
    - confidence: 0.85
    - evidence: memory/2024-01-15.md:42-45, memory/2024-01-18.md:10-12
  ```

### Deep Sleep

Promotes short-term recalls to `MEMORY.md`:
- **Ranking score** = weighted combination of:
  - `relevance` (max score) × 0.25
  - `frequency` (log recall count) × 0.20
  - `recency` (exponential decay) × 0.20
  - `diversity` (unique queries / recalls) × 0.15
  - `consolidation` (unique days / 5) × 0.10
  - `conceptual` (tag count / 6) × 0.10

- **Promotion criteria**:
  - `score >= deep_min_score`
  - `recall_count >= deep_min_recall_count`
  - `unique_queries >= deep_min_unique_queries`
  - Not older than `deep_max_age_days`

## Dream Narrative

After each phase, if signals were processed, the plugin generates a dream diary entry using a dedicated AI prompt (NARRATIVE_SYSTEM_PROMPT):

```markdown
You are keeping a dream diary. Write a single entry in first person.
Voice: curious, gentle, slightly whimsical.
Rules:
- Draw from the memory fragments provided.
- Never say "I'm dreaming" or any meta-commentary.
- No markdown headers, bullet points — just flowing prose.
- 80-180 words.
```

Entries are appended to `memory/DREAMS.md` between diary markers.

## Output Locations

| Content | Location |
|---------|----------|
| Phase reports | `memory/dreaming/{phase}/YYYY-MM-DD.md` |
| Inline blocks | `memory/YYYY-MM-DD.md` (between markers) |
| Diary narrative | `memory/DREAMS.md` |
| Recall store | `memory/recall-store.json` |
| Promotion state | `memory/short-term-promotion-state.json` |

## State Files

- `recall-store.json`: Short-term recall signals
- `session-ingestion-state.json`: Session scan state
- `daily-ingestion-state.json`: Daily memory scan state
- `short-term-promotion-state.json`: Promotion tracking

## Cron Jobs

The plugin registers three cron jobs (if cron service is available):

1. **Memory Light Dreaming** — `0 */6 * * *` — triggers light sleep phase
2. **Memory Dreaming Promotion** — `0 3 * * *` — triggers deep sleep phase
3. **Memory REM Dreaming** — `0 5 * * 0` — triggers REM sleep phase

All use `sessionTarget=isolated` and `wakeMode=now` with system event payload.

## Compared to Heartbeat

| | Dreaming | Heartbeat |
|---|---|---|
| Purpose | Memory consolidation | Health check |
| Trigger | Cron (scheduled) | Cron (scheduled) |
| Context | Isolated session | Main session |
| Output | MEMORY.md + DREAMS.md | Notifications |
| Frequency | Varies by phase | Usually 30min |

## File Structure

```
dreaming/
├── manifest.json        # Plugin manifest
├── dreaming_plugin.py   # Main plugin code
├── DESIGN.md           # Design document
└── README.md           # This file
```