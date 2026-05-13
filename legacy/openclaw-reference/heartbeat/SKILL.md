# Heartbeat Skill

When a heartbeat message arrives, follow this skill to provide timely, appropriate responses.

## Trigger

A heartbeat is identified by:
- Message content matches: `Read HEARTBEAT.md if it exists...` pattern
- Metadata contains: `{"heartbeat": true, "source": "heartbeat-plugin"}`

## Heartbeat Response Protocol

### 1. Check HEARTBEAT.md

Read the workspace `HEARTBEAT.md` file to understand what checks are configured.

### 2. Execute Checks

Run configured checks in order of priority:
1. **Urgent items first** - Flag anything needing immediate response
2. **Inbox/Email** - Check for urgent unread messages
3. **Calendar** - Look for upcoming meetings
4. **Notifications** - Mentions, alerts, etc.
5. **Daily check-in** - Light conversation if appropriate

### 3. Response Rules

**If nothing needs attention:**
```
Reply: HEARTBEAT_OK
```

**If something needs attention:**
```
Reply: [Your notification/alert text]
```

### 4. Smart Response Handling

- `HEARTBEAT_OK` at start/end of reply + ≤ 300 remaining chars → Message suppressed
- Actual content → Delivered to configured target (usually last active channel)
- For important alerts → Do NOT include `HEARTBEAT_OK`; return only the alert

## HEARTBEAT.md Format

```markdown
# Heartbeat Checklist

## Priority Tasks
- [task name]: [check description]

## Regular Tasks
- [task name]: [check description]

## Quiet Hours
- Active hours: 09:00-22:00 (skip outside this window)
```

## Timing Guidelines

- **Morning (06:00-09:00)**: Quick status check, no conversation
- **Daytime (09:00-18:00)**: Full checks, occasional gentle check-in
- **Evening (18:00-22:00)**: Wrap-up checks, minimal intrusion
- **Night (22:00-06:00)**: Skip or silent (respects active hours from config)

## Response Quality

- Be concise - heartbeat is a check-in, not a conversation
- Focus on actionable items
- Use notification-friendly formatting
- Do not add filler words or pleasantries

## Error Handling

- If a check fails → Log the error, continue with remaining checks
- If all checks fail → Reply `HEARTBEAT_OK` (don't spam with errors)
- If uncertain → Err on the side of silence (avoid unnecessary notifications)

## Skip Conditions

Skip checks and reply `HEARTBEAT_OK` when:
1. I'm in a busy conversation
2. Cron jobs are actively running
3. System resources are constrained
4. It's outside active hours

## State Tracking

After each heartbeat:
1. Update `memory/heartbeat-state.json` with last check time
2. Record any items that need follow-up
3. Note any errors encountered

## Integration with Tasks

If heartbeat finds items requiring detached work:
1. Create a cron job for time-bound tasks
2. Create a TaskFlow for multi-step tasks
3. Set inferred commitments for follow-up

Heartbeat does NOT create background task records itself - delegate to cron/taskflow for detached work.