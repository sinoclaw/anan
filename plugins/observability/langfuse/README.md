# Langfuse Observability Plugin

This plugin ships bundled with Anan but is **opt-in** — it only loads when
you explicitly enable it.

## Enable

Pick one:

```bash
# Interactive: walks you through credentials + SDK install + enable
anan tools  # → Langfuse Observability

# Manual
pip install langfuse
anan plugins enable observability/langfuse
```

## Required credentials

Set these in `~/.anan/.env` (or via `anan tools`):

```bash
SINOCLAW_LANGFUSE_PUBLIC_KEY=pk-lf-...
SINOCLAW_LANGFUSE_SECRET_KEY=sk-lf-...
SINOCLAW_LANGFUSE_BASE_URL=https://cloud.langfuse.com   # or your self-hosted URL
```

Without the SDK or credentials the hooks no-op silently — the plugin fails
open.

## Verify

```bash
anan plugins list                 # observability/langfuse should show "enabled"
anan chat -q "hello"              # then check Langfuse for an "Anan turn" trace
```

## Optional tuning

```bash
SINOCLAW_LANGFUSE_ENV=production       # environment tag
SINOCLAW_LANGFUSE_RELEASE=v1.0.0       # release tag
SINOCLAW_LANGFUSE_SAMPLE_RATE=0.5      # sample 50% of traces
SINOCLAW_LANGFUSE_MAX_CHARS=12000      # max chars per field (default: 12000)
SINOCLAW_LANGFUSE_DEBUG=true           # verbose plugin logging
```

## Disable

```bash
anan plugins disable observability/langfuse
```
