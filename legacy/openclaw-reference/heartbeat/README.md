# Heartbeat Plugin for Sinoclaw

Periodic main-session turns — batch checks (inbox, calendar, notifications) with full session context. Replicates OpenClaw's heartbeat mechanism.

## Overview

The heartbeat plugin provides intelligent, periodic health-checks for the Sinoclaw agent. Unlike cron which runs isolated tasks, heartbeat runs in the **main session context** so the agent can make smart, contextual decisions about what needs attention.

## Features

- **Phase-offset scheduling**: Multiple agents have dispersed heartbeat times (SHA256-based), avoiding simultaneous triggers
- **Active hours**: Configurable quiet-hours during which heartbeats are skipped
- **Flood guard**: Automatically skips heartbeats if too many fire in a short window
- **Cron deferral**: Heartbeats defer when cron jobs are running
- **Per-agent config**: Each agent can have its own interval, prompt, and schedule
- **Persistent state**: Tracks last run times across restarts

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Heartbeat Plugin                         │
├─────────────────────────────────────────────────────────────┤
│  HeartbeatPlugin                                            │
│  ├── Phase offset calculator (SHA256-based)                 │
│  ├── Active hours resolver                                  │
│  ├── Flood guard (60s window, 5 max)                        │
│  ├── Deferral engine (cron busy, min-spacing)               │
│  └── Hook system (on_heartbeat_tick, on_heartbeat_run, etc)  │
│                                                             │
│  HeartbeatState (persistent)                                │
│  └── heartbeat-state.json                                   │
└─────────────────────────────────────────────────────────────┘
```

## Installation

1. Copy to Sinoclaw plugins directory:
   ```bash
   cp -r /data/plugins/heartbeat /data/sinoclaw/plugins/
   ```

2. Add to `config.yaml`:
   ```yaml
   plugins:
     heartbeat:
       enabled: true
       interval_ms: 1800000  # 30 minutes
       active_hours:
         start: "09:00"
         end: "22:00"
         timezone: "Asia/Shanghai"
   
   agents:
     defaults:
       heartbeat:
         every: "30m"
         target: "last"
   ```

3. Restart anan-gateway

## Configuration

### Global Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `true` | Enable/disable plugin |
| `interval_ms` | `1800000` | Heartbeat interval (30 min) |
| `scheduler_seed` | `sinoclaw-heartbeat-v1` | Phase calculation seed |
| `flood_window_ms` | `60000` | Flood guard window (60s) |
| `flood_threshold` | `5` | Max heartbeats in window |
| `min_spacing_ms` | `30000` | Min spacing between runs |
| `active_hours` | `null` | Quiet hours config |

### Per-Agent Settings

| Setting | Description |
|---------|-------------|
| `enabled` | Enable for this agent |
| `interval_ms` | Override global interval |
| `prompt` | Custom heartbeat prompt |
| `activeHours` | Override global active hours |
| `target` | `last` (deliver to last channel) or `none` |
| `skipWhenBusy` | Skip when subagents are running |
| `lightContext` | Only inject HEARTBEAT.md |
| `isolatedSession` | Fresh session per heartbeat |

## How It Works

### Phase Offset (Multi-Agent)

When multiple agents have heartbeat enabled, they use SHA256 hashing to compute a unique phase offset within the interval window:

```
Agent A: SHA256("anan-heartbeat-v1:main") % 1800000 = 547293ms
Agent B: SHA256("anan-heartbeat-v1:ops") % 1800000 = 1204891ms

→ Agent A fires at T+9min, Agent B fires at T+20min
→ No simultaneous heartbeat storms
```

### Active Hours

Heartbeats are skipped outside the configured active hours window:

```
active_hours:
  start: "09:00"
  end: "22:00"
  timezone: "Asia/Shanghai"

# Heartbeats only fire between 09:00-22:00 CST
```

### Flood Guard

If too many heartbeats fire within a short window, subsequent ones are skipped:

```
flood_window_ms: 60000    # 60 second window
flood_threshold: 5        # Max 5 runs in window

# If 5+ heartbeats fire in 60s, additional ones are deferred
```

### Cron Deferral

When cron jobs are running, heartbeats automatically defer to avoid resource contention.

## HEARTBEAT.md

The agent reads `HEARTBEAT.md` from the workspace to determine what to check. Example:

```markdown
# Heartbeat Checklist

## Email
- Check for urgent unread messages
- Flag anything requiring immediate response

## Calendar  
- Look for upcoming meetings in next 2 hours
- Note anything needing preparation

## Notifications
- Check mention alerts
- Review any blocked/dm messages

## Daily Check-in
- If afternoon, consider a light "anything you need?" message
- Keep it brief - this is a heartbeat, not a conversation
```

## Response Contract

When the agent receives a heartbeat:

1. **Check HEARTBEAT.md** → Determine what needs attention
2. **Do checks** → Email, calendar, notifications, etc.
3. **Reply with result**:
   - If nothing needs attention: `HEARTBEAT_OK`
   - If something needs attention: Return the alert/notification

### HEARTBEAT_OK Handling

- If `HEARTBEAT_OK` appears at start or end of reply, and remaining content ≤ 300 chars, the message is suppressed
- If there's actual content to report, it's delivered to the configured target (usually "last" = last active channel)

## Status & Debugging

Check plugin status:
```bash
# Via the plugin API
curl http://localhost:8642/api/plugins/heartbeat/status

# Check state file
cat ~/.anan/heartbeat-state.json
```

## vs Cron

| | Heartbeat | Cron |
|---|---|---|
| Context | Full main session | Isolated |
| Tasks created | No | Yes |
| Delivery | To main session | To channel/webhook |
| Multi-agent | Phase-offset | Manual config |
| Best for | Batch checks | Reports, reminders |

## File Structure

```
heartbeat/
├── manifest.json      # Plugin manifest
├── __init__.py       # (optional, for package)
├── heartbeat_plugin.py   # Main plugin code
├── config_schema.py     # Config validation
└── README.md         # This file
```

## Credits

Mechanism inspired by [OpenClaw heartbeat](https://docs.openclaw.ai/heartbeat), adapted for Sinoclaw's Python/Gateway architecture.