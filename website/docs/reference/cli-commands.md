---
sidebar_position: 1
title: "CLI Commands Reference"
description: "Authoritative reference for anan Agent terminal commands and command families"
---

# CLI Commands Reference

This page covers the **terminal commands** you run from your shell.

For in-chat slash commands, see [Slash Commands Reference](./slash-commands.md).

## Global entrypoint

```bash
anan [global-options] <command> [subcommand/options]
```

### Global options

| Option | Description |
|--------|-------------|
| `--version`, `-V` | Show version and exit. |
| `--profile <name>`, `-p <name>` | Select which anan Agent profile to use for this invocation. Overrides the sticky default set by `anan profile use`. |
| `--resume <session>`, `-r <session>` | Resume a previous session by ID or title. |
| `--continue [name]`, `-c [name]` | Resume the most recent session, or the most recent session matching a title. |
| `--worktree`, `-w` | Start in an isolated git worktree for parallel-agent workflows. |
| `--yolo` | Bypass dangerous-command approval prompts. |
| `--pass-session-id` | Include the session ID in the agent's system prompt. |
| `--ignore-user-config` | Ignore `~/.anan/config.yaml` and fall back to built-in defaults. Credentials in `.env` are still loaded. |
| `--ignore-rules` | Skip auto-injection of `AGENTS.md`, `SOUL.md`, `.cursorrules`, memory, and preloaded skills. |
| `--tui` | Launch the [TUI](../user-guide/tui.md) instead of the classic CLI. Equivalent to `SINOCLAW_TUI=1`. |
| `--dev` | With `--tui`: run the TypeScript sources directly via `tsx` instead of the prebuilt bundle (for TUI contributors). |

## Top-level commands

| Command | Purpose |
|---------|---------|
| `anan chat` | Interactive or one-shot chat with the agent. |
| `anan model` | Interactively choose the default provider and model. |
| `anan fallback` | Manage fallback providers tried when the primary model errors. |
| `anan gateway` | Run or manage the messaging gateway service. |
| `anan setup` | Interactive setup wizard for all or part of the configuration. |
| `anan whatsapp` | Configure and pair the WhatsApp bridge. |
| `anan slack` | Slack helpers (currently: generate the app manifest with every command as a native slash). |
| `anan auth` | Manage credentials — add, list, remove, reset, set strategy. Handles OAuth flows for Codex/Nous/Anthropic. |
| `anan login` / `logout` | **Deprecated** — use `anan auth` instead. |
| `anan status` | Show agent, auth, and platform status. |
| `anan cron` | Inspect and tick the cron scheduler. |
| `anan kanban` | Multi-profile collaboration board (tasks, links, dispatcher). |
| `anan webhook` | Manage dynamic webhook subscriptions for event-driven activation. |
| `anan hooks` | Inspect, approve, or remove shell-script hooks declared in `config.yaml`. |
| `anan doctor` | Diagnose config and dependency issues. |
| `anan dump` | Copy-pasteable setup summary for support/debugging. |
| `anan debug` | Debug tools — upload logs and system info for support. |
| `anan backup` | Back up anan home directory to a zip file. |
| `anan checkpoints` | Inspect / prune / clear `~/.anan/checkpoints/` (the shadow store used by `/rollback`). Run with no args for a status overview. |
| `anan import` | Restore a anan Agent backup from a zip file. |
| `anan logs` | View, tail, and filter agent/gateway/error log files. |
| `anan config` | Show, edit, migrate, and query configuration files. |
| `anan pairing` | Approve or revoke messaging pairing codes. |
| `anan skills` | Browse, install, publish, audit, and configure skills. |
| `anan curator` | Background skill maintenance — status, run, pause, pin. See [Curator](../user-guide/features/curator.md). |
| `anan memory` | Configure external memory provider. Plugin-specific subcommands (e.g. `anan honcho`) register automatically when their provider is active. |
| `anan acp` | Run anan Agent as an ACP server for editor integration. |
| `anan mcp` | Manage MCP server configurations and run anan Agent as an MCP server. |
| `anan plugins` | Manage anan Agent plugins (install, enable, disable, remove). |
| `anan tools` | Configure enabled tools per platform. |
| `anan sessions` | Browse, export, prune, rename, and delete sessions. |
| `anan insights` | Show token/cost/activity analytics. |
| `anan fallback` | Interactive manager for the fallback provider chain. |
| `anan claw` | OpenClaw migration helpers. |
| `anan dashboard` | Launch the web dashboard for managing config, API keys, and sessions. |
| `anan profile` | Manage profiles — multiple isolated anan Agent instances. |
| `anan completion` | Print shell completion scripts (bash/zsh/fish). |
| `anan version` | Show version information. |
| `anan update` | Pull latest code and reinstall dependencies. `--check` prints commit diff without pulling; `--backup` takes a pre-pull `ANAN_HOME` snapshot. |
| `anan uninstall` | Remove anan Agent from the system. |

## `anan chat`

```bash
anan chat [options]
```

Common options:

| Option | Description |
|--------|-------------|
| `-q`, `--query "..."` | One-shot, non-interactive prompt. |
| `-m`, `--model <model>` | Override the model for this run. |
| `-t`, `--toolsets <csv>` | Enable a comma-separated set of toolsets. |
| `--provider <provider>` | Force a provider: `auto`, `openrouter`, `nous`, `openai-codex`, `copilot-acp`, `copilot`, `anthropic`, `gemini`, `google-gemini-cli`, `huggingface`, `zai`, `kimi-coding`, `kimi-coding-cn`, `minimax`, `minimax-cn`, `minimax-oauth`, `kilocode`, `xiaomi`, `arcee`, `gmi`, `alibaba`, `alibaba-coding-plan` (alias `alibaba_coding`), `deepseek`, `nvidia`, `ollama-cloud`, `xai` (alias `grok`), `qwen-oauth`, `bedrock`, `opencode-zen`, `opencode-go`, `ai-gateway`, `azure-foundry`, `tencent-tokenhub` (alias `tencent`, `tokenhub`). |
| `-s`, `--skills <name>` | Preload one or more skills for the session (can be repeated or comma-separated). |
| `-v`, `--verbose` | Verbose output. |
| `-Q`, `--quiet` | Programmatic mode: suppress banner/spinner/tool previews. |
| `--image <path>` | Attach a local image to a single query. |
| `--resume <session>` / `--continue [name]` | Resume a session directly from `chat`. |
| `--worktree` | Create an isolated git worktree for this run. |
| `--checkpoints` | Enable filesystem checkpoints before destructive file changes. |
| `--yolo` | Skip approval prompts. |
| `--pass-session-id` | Pass the session ID into the system prompt. |
| `--ignore-user-config` | Ignore `~/.anan/config.yaml` and use built-in defaults. Credentials in `.env` are still loaded. Useful for isolated CI runs, reproducible bug reports, and third-party integrations. |
| `--ignore-rules` | Skip auto-injection of `AGENTS.md`, `SOUL.md`, `.cursorrules`, persistent memory, and preloaded skills. Combine with `--ignore-user-config` for a fully isolated run. |
| `--source <tag>` | Session source tag for filtering (default: `cli`). Use `tool` for third-party integrations that should not appear in user session lists. |
| `--max-turns <N>` | Maximum tool-calling iterations per conversation turn (default: 90, or `agent.max_turns` in config). |

Examples:

```bash
anan
anan chat -q "Summarize the latest PRs"
anan chat --provider openrouter --model anthropic/claude-sonnet-4.6
anan chat --toolsets web,terminal,skills
anan chat --quiet -q "Return only JSON"
anan chat --worktree -q "Review this repo and open a PR"
anan chat --ignore-user-config --ignore-rules -q "Repro without my personal setup"
```

### `anan -z <prompt>` — scripted one-shot

For programmatic callers (shell scripts, CI, cron, parent processes piping in a prompt), `anan -z` is the purest one-shot entry point: **single prompt in, final response text out, nothing else on stdout or stderr.** No banner, no spinner, no tool previews, no `Session:` line — just the agent's final reply as plain text.

```bash
anan -z "What's the capital of France?"
# → Paris.

# Parent scripts can cleanly capture the response:
answer=$(anan -z "summarize this" < /path/to/file.txt)
```

Per-run overrides (no mutation to `~/.anan/config.yaml`):

| Flag | Equivalent env var | Purpose |
|---|---|---|
| `-m` / `--model <model>` | `SINOCLAW_INFERENCE_MODEL` | Override the model for this run |
| `--provider <provider>` | `SINOCLAW_INFERENCE_PROVIDER` | Override the provider for this run |

```bash
anan -z "…" --provider openrouter --model openai/gpt-5.5
# or:
SINOCLAW_INFERENCE_MODEL=anthropic/claude-sonnet-4.6 anan -z "…"
```

Same agent, same tools, same skills — just strips every interactive / cosmetic layer. If you need tool output in the transcript too, use `anan chat -q` instead; `-z` is explicitly for "I only want the final answer".

## `anan model`

Interactive provider + model selector. **This is the command for adding new providers, setting up API keys, and running OAuth flows.** Run it from your terminal — not from inside an active anan Agent chat session.

```bash
anan model
```

Use this when you want to:
- **add a new provider** (OpenRouter, Anthropic, Copilot, DeepSeek, custom, etc.)
- log into OAuth-backed providers (Anthropic, Copilot, Codex, Nous Portal)
- enter or update API keys
- pick from provider-specific model lists
- configure a custom/self-hosted endpoint
- save the new default into config

:::warning anan model vs /model — know the difference
**`anan model`** (run from your terminal, outside any anan session) is the **full provider setup wizard**. It can add new providers, run OAuth flows, prompt for API keys, and configure endpoints.

**`/model`** (typed inside an active anan Agent chat session) can only **switch between providers and models you've already set up**. It cannot add new providers, run OAuth, or prompt for API keys.

**If you need to add a new provider:** Exit your anan session first (`Ctrl+C` or `/quit`), then run `anan model` from your terminal prompt.
:::

### `/model` slash command (mid-session)

Switch between already-configured models without leaving a session:

```
/model                              # Show current model and available options
/model claude-sonnet-4              # Switch model (auto-detects provider)
/model zai:glm-5                    # Switch provider and model
/model custom:qwen-2.5              # Use model on your custom endpoint
/model custom                       # Auto-detect model from custom endpoint
/model custom:local:qwen-2.5        # Use a named custom provider
/model openrouter:anthropic/claude-sonnet-4  # Switch back to cloud
```

By default, `/model` changes apply **to the current session only**. Add `--global` to persist the change to `config.yaml`:

```
/model claude-sonnet-4 --global     # Switch and save as new default
```

:::info What if I only see OpenRouter models?
If you've only configured OpenRouter, `/model` will only show OpenRouter models. To add another provider (Anthropic, DeepSeek, Copilot, etc.), exit your session and run `anan model` from the terminal.
:::

Provider and base URL changes are persisted to `config.yaml` automatically. When switching away from a custom endpoint, the stale base URL is cleared to prevent it leaking into other providers.

## `anan gateway`

```bash
anan gateway <subcommand>
```

Subcommands:

| Subcommand | Description |
|------------|-------------|
| `run` | Run the gateway in the foreground. Recommended for WSL, Docker, and Termux. |
| `start` | Start the installed systemd/launchd background service. |
| `stop` | Stop the service (or foreground process). |
| `restart` | Restart the service. |
| `status` | Show service status. |
| `install` | Install as a systemd (Linux) or launchd (macOS) background service. |
| `uninstall` | Remove the installed service. |
| `setup` | Interactive messaging-platform setup. |

Options:

| Option | Description |
|--------|-------------|
| `--all` | On `start` / `restart` / `stop`: act on **every profile's** gateway, not just the active `ANAN_HOME`. Useful if you run multiple profiles side-by-side and want to restart them all after `anan update`. |

:::tip WSL users
Use `anan gateway run` instead of `anan gateway start` — WSL's systemd support is unreliable. Wrap it in tmux for persistence: `tmux new -s anan 'anan gateway run'`. See [WSL FAQ](/docs/reference/faq#wsl-gateway-keeps-disconnecting-or-anan-gateway-start-fails) for details.
:::

## `anan setup`

```bash
anan setup [model|tts|terminal|gateway|tools|agent] [--non-interactive] [--reset] [--quick] [--reconfigure]
```

**First run:** launches the first-time wizard.

**Returning user (already configured):** drops straight into the full reconfigure wizard — every prompt shows your current value as its default, press Enter to keep or type a new value. No menu.

Jump into one section instead of the full wizard:

| Section | Description |
|---------|-------------|
| `model` | Provider and model setup. |
| `terminal` | Terminal backend and sandbox setup. |
| `gateway` | Messaging platform setup. |
| `tools` | Enable/disable tools per platform. |
| `agent` | Agent behavior settings. |

Options:

| Option | Description |
|--------|-------------|
| `--quick` | On returning-user runs: only prompt for items that are missing or unset. Skip items you already have configured. |
| `--non-interactive` | Use defaults / environment values without prompts. |
| `--reset` | Reset configuration to defaults before setup. |
| `--reconfigure` | Backwards-compat alias — bare `anan setup` on an existing install now does this by default. |

## `anan whatsapp`

```bash
anan whatsapp
```

Runs the WhatsApp pairing/setup flow, including mode selection and QR-code pairing.

## `anan slack`

```bash
anan slack manifest              # print manifest to stdout
anan slack manifest --write      # write to ~/.anan/slack-manifest.json
anan slack manifest --slashes-only  # just the features.slash_commands array
```

Generates a Slack app manifest that registers every gateway command in
`COMMAND_REGISTRY` (`/btw`, `/stop`, `/model`, …) as a first-class
Slack slash command — matching Discord and Telegram parity. Paste the
output into your Slack app config at
[https://api.slack.com/apps](https://api.slack.com/apps) → your app →
**Features → App Manifest → Edit**, then **Save**. Slack prompts for
reinstall if scopes or slash commands changed.

| Flag | Default | Purpose |
|------|---------|---------|
| `--write [PATH]` | stdout | Write to a file instead of stdout. Bare `--write` writes `$ANAN_HOME/slack-manifest.json`. |
| `--name NAME` | `anan Agent` | Bot display name in Slack. |
| `--description DESC` | default blurb | Bot description shown in the Slack app directory. |
| `--slashes-only` | off | Emit only `features.slash_commands` for merging into a manually-maintained manifest. |

Run `anan slack manifest --write` again after `anan update` to pick
up any new commands.


## `anan login` / `anan logout` *(Deprecated)*

:::caution
`anan login` has been removed. Use `anan auth` to manage OAuth credentials, `anan model` to select a provider, or `anan setup` for full interactive setup.
:::

## `anan auth`

Manage credential pools for same-provider key rotation. See [Credential Pools](/docs/user-guide/features/credential-pools) for full documentation.

```bash
anan auth                                              # Interactive wizard
anan auth list                                         # Show all pools
anan auth list openrouter                              # Show specific provider
anan auth add openrouter --api-key sk-or-v1-xxx        # Add API key
anan auth add anthropic --type oauth                   # Add OAuth credential
anan auth remove openrouter 2                          # Remove by index
anan auth reset openrouter                             # Clear cooldowns
```

Subcommands: `add`, `list`, `remove`, `reset`. When called with no subcommand, launches the interactive management wizard.

## `anan status`

```bash
anan status [--all] [--deep]
```

| Option | Description |
|--------|-------------|
| `--all` | Show all details in a shareable redacted format. |
| `--deep` | Run deeper checks that may take longer. |

## `anan cron`

```bash
anan cron <list|create|edit|pause|resume|run|remove|status|tick>
```

| Subcommand | Description |
|------------|-------------|
| `list` | Show scheduled jobs. |
| `create` / `add` | Create a scheduled job from a prompt, optionally attaching one or more skills via repeated `--skill`. |
| `edit` | Update a job's schedule, prompt, name, delivery, repeat count, or attached skills. Supports `--clear-skills`, `--add-skill`, and `--remove-skill`. |
| `pause` | Pause a job without deleting it. |
| `resume` | Resume a paused job and compute its next future run. |
| `run` | Trigger a job on the next scheduler tick. |
| `remove` | Delete a scheduled job. |
| `status` | Check whether the cron scheduler is running. |
| `tick` | Run due jobs once and exit. |

## `anan kanban`

```bash
anan kanban [--board <slug>] <action> [options]
```

Multi-profile, multi-project collaboration board. Each install can host many boards (one per project, repo, or domain); each board is a standalone queue with its own SQLite DB and dispatcher scope. New installs start with one board called `default`, whose DB is `~/.anan/kanban.db` for back-compat; additional boards live at `~/.anan/kanban/boards/<slug>/kanban.db`. The gateway-embedded dispatcher sweeps every board per tick.

**Global flags (apply to every action below):**

| Flag | Purpose |
|------|---------|
| `--board <slug>` | Operate on a specific board. Defaults to the current board (set via `anan kanban boards switch`, the `SINOCLAW_KANBAN_BOARD` env var, or `default`). |

**This is the human / scripting surface.** Agent workers spawned by the dispatcher drive the board through a dedicated `kanban_*` [toolset](/docs/user-guide/features/kanban#how-workers-interact-with-the-board) (`kanban_show`, `kanban_complete`, `kanban_block`, `kanban_create`, `kanban_link`, `kanban_comment`, `kanban_heartbeat`) instead of shelling to `anan kanban`. Workers have `SINOCLAW_KANBAN_BOARD` pinned in their env so they physically cannot see other boards.

| Action | Purpose |
|--------|---------|
| `init` | Create `kanban.db` if missing. Idempotent. |
| `boards list` / `boards ls` | List all boards with task counts. `--json`, `--all` (include archived). |
| `boards create <slug>` | Create a new board. Flags: `--name`, `--description`, `--icon`, `--color`, `--switch` (make active). Slug is kebab-case, auto-downcased. |
| `boards switch <slug>` / `boards use` | Persist `<slug>` as the active board (writes `~/.anan/kanban/current`). |
| `boards show` / `boards current` | Print the currently-active board's name, DB path, and task counts. |
| `boards rename <slug> "<name>"` | Change a board's display name. Slug is immutable. |
| `boards rm <slug>` | Archive (default) or hard-delete a board. `--delete` skips the archive step. Archived boards move to `boards/_archived/<slug>-<ts>/`. Refused for `default`. |
| `create "<title>"` | Create a new task on the active board. Flags: `--body`, `--assignee`, `--parent` (repeatable), `--workspace scratch\|worktree\|dir:<path>`, `--tenant`, `--priority`, `--triage`, `--idempotency-key`, `--max-runtime`, `--skill` (repeatable). |
| `list` / `ls` | List tasks on the active board. Filter with `--mine`, `--assignee`, `--status`, `--tenant`, `--archived`, `--json`. |
| `show <id>` | Show a task with comments and events. `--json` for machine output. |
| `assign <id> <profile>` | Assign or reassign. Use `none` to unassign. Refused while task is running. |
| `link <parent> <child>` | Add a dependency. Cycle-detected. Both tasks must be on the same board. |
| `unlink <parent> <child>` | Remove a dependency. |
| `claim <id>` | Atomically claim a ready task. Prints resolved workspace path. |
| `comment <id> "<text>"` | Append a comment. The next worker that claims the task reads it as part of its `kanban_show()` response. |
| `complete <id>` | Mark task done. Flags: `--result`, `--summary`, `--metadata`. |
| `block <id> "<reason>"` | Mark task blocked. Also appends the reason as a comment. |
| `unblock <id>` | Return a blocked task to ready. |
| `archive <id>` | Hide from default list. `gc` will remove scratch workspaces. |
| `tail <id>` | Follow a task's event stream. |
| `dispatch` | One dispatcher pass on the active board. Flags: `--dry-run`, `--max N`, `--json`. |
| `context <id>` | Print the full context a worker would see (title + body + parent results + comments). |
| `specify <id>` / `specify --all` | Flesh out a triage-column task into a concrete spec (title + body with goal, approach, acceptance criteria) via the auxiliary LLM, then promote it to `todo`. Flags: `--tenant` (scope `--all` to one tenant), `--author`, `--json`. Configure the model under `auxiliary.triage_specifier` in `config.yaml`. |
| `gc` | Remove scratch workspaces for archived tasks. |

Examples:

```bash
# Create a second board and put a task on it without switching away.
anan kanban boards create atm10-server --name "ATM10 Server" --icon 🎮
anan kanban --board atm10-server create "Restart server" --assignee ops

# Switch the active board for subsequent calls.
anan kanban boards switch atm10-server
anan kanban list                  # shows atm10-server tasks

# Archive a board (recoverable) or hard-delete it.
anan kanban boards rm atm10-server
anan kanban boards rm atm10-server --delete
```

Board resolution order (highest precedence first): `--board <slug>` flag → `SINOCLAW_KANBAN_BOARD` env var → `~/.anan/kanban/current` file → `default`.

All actions are also available as a slash command in the gateway (`/kanban …`), with the same argument surface — including `boards` subcommands and the `--board` flag.

For the full design — comparison with Cline Kanban / Paperclip / NanoClaw / Gemini Enterprise, eight collaboration patterns, four user stories, concurrency correctness proof — see `docs/anan-kanban-v1-spec.pdf` in the repository or the [Kanban user guide](/docs/user-guide/features/kanban).

## `anan webhook`

```bash
anan webhook <subscribe|list|remove|test>
```

Manage dynamic webhook subscriptions for event-driven agent activation. Requires the webhook platform to be enabled in config — if not configured, prints setup instructions.

| Subcommand | Description |
|------------|-------------|
| `subscribe` / `add` | Create a webhook route. Returns the URL and HMAC secret to configure on your service. |
| `list` / `ls` | Show all agent-created subscriptions. |
| `remove` / `rm` | Delete a dynamic subscription. Static routes from config.yaml are not affected. |
| `test` | Send a test POST to verify a subscription is working. |

### `anan webhook subscribe`

```bash
anan webhook subscribe <name> [options]
```

| Option | Description |
|--------|-------------|
| `--prompt` | Prompt template with `{dot.notation}` payload references. |
| `--events` | Comma-separated event types to accept (e.g. `issues,pull_request`). Empty = all. |
| `--description` | Human-readable description. |
| `--skills` | Comma-separated skill names to load for the agent run. |
| `--deliver` | Delivery target: `log` (default), `telegram`, `discord`, `slack`, `github_comment`. |
| `--deliver-chat-id` | Target chat/channel ID for cross-platform delivery. |
| `--secret` | Custom HMAC secret. Auto-generated if omitted. |
| `--deliver-only` | Skip the agent — deliver the rendered `--prompt` as the literal message. Zero LLM cost, sub-second delivery. Requires `--deliver` to be a real target (not `log`). |

Subscriptions persist to `~/.anan/webhook_subscriptions.json` and are hot-reloaded by the webhook adapter without a gateway restart.

## `anan doctor`

```bash
anan doctor [--fix]
```

| Option | Description |
|--------|-------------|
| `--fix` | Attempt automatic repairs where possible. |

## `anan dump`

```bash
anan dump [--show-keys]
```

Outputs a compact, plain-text summary of your entire anan Agent setup. Designed to be copy-pasted into Discord, GitHub issues, or Telegram when asking for support — no ANSI colors, no special formatting, just data.

| Option | Description |
|--------|-------------|
| `--show-keys` | Show redacted API key prefixes (first and last 4 characters) instead of just `set`/`not set`. |

### What it includes

| Section | Details |
|---------|---------|
| **Header** | anan Agent version, release date, git commit hash |
| **Environment** | OS, Python version, OpenAI SDK version |
| **Identity** | Active profile name, ANAN_HOME path |
| **Model** | Configured default model and provider |
| **Terminal** | Backend type (local, docker, ssh, etc.) |
| **API keys** | Presence check for all 22 provider/tool API keys |
| **Features** | Enabled toolsets, MCP server count, memory provider |
| **Services** | Gateway status, configured messaging platforms |
| **Workload** | Cron job counts, installed skill count |
| **Config overrides** | Any config values that differ from defaults |

### Example output

```
--- anan dump ---
version:          0.8.0 (2026.4.8) [af4abd2f]
os:               Linux 6.14.0-37-generic x86_64
python:           3.11.14
openai_sdk:       2.24.0
profile:          default
anan_home:      ~/.anan
model:            anthropic/claude-opus-4.6
provider:         openrouter
terminal:         local

api_keys:
  openrouter           set
  openai               not set
  anthropic            set
  nous                 not set
  firecrawl            set
  ...

features:
  toolsets:           all
  mcp_servers:        0
  memory_provider:    built-in
  gateway:            running (systemd)
  platforms:          telegram, discord
  cron_jobs:          3 active / 5 total
  skills:             42

config_overrides:
  agent.max_turns: 250
  compression.threshold: 0.85
  display.streaming: True
--- end dump ---
```

### When to use

- Reporting a bug on GitHub — paste the dump into your issue
- Asking for help in Discord — share it in a code block
- Comparing your setup to someone else's
- Quick sanity check when something isn't working

:::tip
`anan dump` is specifically designed for sharing. For interactive diagnostics, use `anan doctor`. For a visual overview, use `anan status`.
:::

## `anan debug`

```bash
anan debug share [options]
```

Upload a debug report (system info + recent logs) to a paste service and get a shareable URL. Useful for quick support requests — includes everything a helper needs to diagnose your issue.

| Option | Description |
|--------|-------------|
| `--lines <N>` | Number of log lines to include per log file (default: 200). |
| `--expire <days>` | Paste expiry in days (default: 7). |
| `--local` | Print the report locally instead of uploading. |

The report includes system info (OS, Python version, anan Agent version), recent agent and gateway logs (512 KB limit per file), and redacted API key status. Keys are always redacted — no secrets are uploaded.

Paste services tried in order: paste.rs, dpaste.com.

### Examples

```bash
anan debug share              # Upload debug report, print URL
anan debug share --lines 500  # Include more log lines
anan debug share --expire 30  # Keep paste for 30 days
anan debug share --local      # Print report to terminal (no upload)
```

## `anan backup`

```bash
anan backup [options]
```

Create a zip archive of your anan Agent configuration, skills, sessions, and data. The backup excludes the anan codebase itself.

| Option | Description |
|--------|-------------|
| `-o`, `--output <path>` | Output path for the zip file (default: `~/anan-backup-<timestamp>.zip`). |
| `-q`, `--quick` | Quick snapshot: only critical state files (config.yaml, state.db, .env, auth, cron jobs). Much faster than a full backup. |
| `-l`, `--label <name>` | Label for the snapshot (only used with `--quick`). |

The backup uses SQLite's `backup()` API for safe copying, so it works correctly even when anan Agent is running (WAL-mode safe).

**What's excluded from the zip:**

- `*.db-wal`, `*.db-shm`, `*.db-journal` — SQLite's WAL / shared-memory / journal sidecars. The `*.db` file already got a consistent snapshot via `sqlite3.backup()`; shipping the live sidecars alongside it would let a restore see a half-committed state.
- `checkpoints/` — per-session trajectory caches. Hash-keyed and regenerated per session; wouldn't port cleanly to another install anyway.
- The `anan` code itself (this is a user-data backup, not a repo snapshot).

### Examples

```bash
anan backup                           # Full backup to ~/anan-backup-*.zip
anan backup -o /tmp/anan.zip        # Full backup to specific path
anan backup --quick                   # Quick state-only snapshot
anan backup --quick --label "pre-upgrade"  # Quick snapshot with label
```

## `anan checkpoints`

```bash
anan checkpoints [COMMAND]
```

Inspect and manage the shadow git store at `~/.anan/checkpoints/` — the storage layer behind the in-session `/rollback` command. Safe to run any time; does not require the agent to be running.

| Subcommand | Description |
|------------|-------------|
| `status` (default) | Show total size, project count, and per-project breakdown. Bare `anan checkpoints` is equivalent. |
| `list` | Alias for `status`. |
| `prune` | Force a cleanup sweep — delete orphan and stale projects, GC the store, enforce the size cap. Ignores the 24h idempotency marker. |
| `clear` | Delete the entire checkpoint base. Irreversible; asks for confirmation unless `-f`. |
| `clear-legacy` | Delete only the `legacy-<timestamp>/` archives produced by the v1→v2 migration. |

### Options

| Option | Subcommand | Description |
|--------|------------|-------------|
| `--limit N` | `status`, `list` | Max projects to list (default 20). |
| `--retention-days N` | `prune` | Drop projects whose `last_touch` is older than N days (default 7). |
| `--max-size-mb N` | `prune` | After the orphan/stale pass, drop the oldest commit per project until total store size ≤ N MB (default 500). |
| `--keep-orphans` | `prune` | Skip deleting projects whose working directory no longer exists. |
| `-f`, `--force` | `clear`, `clear-legacy` | Skip the confirmation prompt. |

### Examples

```bash
anan checkpoints                                  # status overview
anan checkpoints prune --retention-days 3         # aggressive cleanup
anan checkpoints prune --max-size-mb 200          # tighten size cap once
anan checkpoints clear-legacy -f                  # drop v1 archive dirs
anan checkpoints clear -f                         # wipe everything
```

See [Checkpoints and `/rollback`](../user-guide/checkpoints-and-rollback.md) for the full architecture and the in-session commands.

## `anan import`

```bash
anan import <zipfile> [options]
```

Restore a previously created anan Agent backup into your anan home directory. All files in the archive overwrite existing files in your anan home; `--force` only skips the confirmation prompt that fires when the target already has a anan Agent installation.

| Option | Description |
|--------|-------------|
| `-f`, `--force` | Skip the existing-installation confirmation prompt. |

:::warning
Stop the gateway before importing to avoid conflicts with running processes.
:::

### Examples
```bash
anan import ~/anan-backup-20260423.zip           # Prompts before overwriting existing config
anan import ~/anan-backup-20260423.zip --force   # Overwrite without prompting
```

## `anan logs`

```bash
anan logs [log_name] [options]
```

View, tail, and filter anan Agent log files. All logs are stored in `~/.anan/logs/` (or `<profile>/logs/` for non-default profiles).

### Log files

| Name | File | What it captures |
|------|------|-----------------|
| `agent` (default) | `agent.log` | All agent activity — API calls, tool dispatch, session lifecycle (INFO and above) |
| `errors` | `errors.log` | Warnings and errors only — a filtered subset of agent.log |
| `gateway` | `gateway.log` | Messaging gateway activity — platform connections, message dispatch, webhook events |

### Options

| Option | Description |
|--------|-------------|
| `log_name` | Which log to view: `agent` (default), `errors`, `gateway`, or `list` to show available files with sizes. |
| `-n`, `--lines <N>` | Number of lines to show (default: 50). |
| `-f`, `--follow` | Follow the log in real time, like `tail -f`. Press Ctrl+C to stop. |
| `--level <LEVEL>` | Minimum log level to show: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `--session <ID>` | Filter lines containing a session ID substring. |
| `--since <TIME>` | Show lines from a relative time ago: `30m`, `1h`, `2d`, etc. Supports `s` (seconds), `m` (minutes), `h` (hours), `d` (days). |
| `--component <NAME>` | Filter by component: `gateway`, `agent`, `tools`, `cli`, `cron`. |

### Examples

```bash
# View the last 50 lines of agent.log (default)
anan logs

# Follow agent.log in real time
anan logs -f

# View the last 100 lines of gateway.log
anan logs gateway -n 100

# Show only warnings and errors from the last hour
anan logs --level WARNING --since 1h

# Filter by a specific session
anan logs --session abc123

# Follow errors.log, starting from 30 minutes ago
anan logs errors --since 30m -f

# List all log files with their sizes
anan logs list
```

### Filtering

Filters can be combined. When multiple filters are active, a log line must pass **all** of them to be shown:

```bash
# WARNING+ lines from the last 2 hours containing session "tg-12345"
anan logs --level WARNING --since 2h --session tg-12345
```

Lines without a parseable timestamp are included when `--since` is active (they may be continuation lines from a multi-line log entry). Lines without a detectable level are included when `--level` is active.

### Log rotation

anan Agent uses Python's `RotatingFileHandler`. Old logs are rotated automatically — look for `agent.log.1`, `agent.log.2`, etc. The `anan logs list` subcommand shows all log files including rotated ones.

## `anan config`

```bash
anan config <subcommand>
```

Subcommands:

| Subcommand | Description |
|------------|-------------|
| `show` | Show current config values. |
| `edit` | Open `config.yaml` in your editor. |
| `set <key> <value>` | Set a config value. |
| `path` | Print the config file path. |
| `env-path` | Print the `.env` file path. |
| `check` | Check for missing or stale config. |
| `migrate` | Add newly introduced options interactively. |

## `anan pairing`

```bash
anan pairing <list|approve|revoke|clear-pending>
```

| Subcommand | Description |
|------------|-------------|
| `list` | Show pending and approved users. |
| `approve <platform> <code>` | Approve a pairing code. |
| `revoke <platform> <user-id>` | Revoke a user's access. |
| `clear-pending` | Clear pending pairing codes. |

## `anan skills`

```bash
anan skills <subcommand>
```

Subcommands:

| Subcommand | Description |
|------------|-------------|
| `browse` | Paginated browser for skill registries. |
| `search` | Search skill registries. |
| `install` | Install a skill. |
| `inspect` | Preview a skill without installing it. |
| `list` | List installed skills. |
| `check` | Check installed hub skills for upstream updates. |
| `update` | Reinstall hub skills with upstream changes when available. |
| `audit` | Re-scan installed hub skills. |
| `uninstall` | Remove a hub-installed skill. |
| `reset` | Un-stick a bundled skill flagged as `user_modified` by clearing its manifest entry. With `--restore`, also replaces the user copy with the bundled version. |
| `publish` | Publish a skill to a registry. |
| `snapshot` | Export/import skill configurations. |
| `tap` | Manage custom skill sources. |
| `config` | Interactive enable/disable configuration for skills by platform. |

Common examples:

```bash
anan skills browse
anan skills browse --source official
anan skills search react --source skills-sh
anan skills search https://mintlify.com/docs --source well-known
anan skills inspect official/security/1password
anan skills inspect skills-sh/vercel-labs/json-render/json-render-react
anan skills install official/migration/openclaw-migration
anan skills install skills-sh/anthropics/skills/pdf --force
anan skills install https://sharethis.chat/SKILL.md                     # Direct URL (single-file SKILL.md)
anan skills install https://example.com/SKILL.md --name my-skill        # Override name when frontmatter has none
anan skills check
anan skills update
anan skills config
anan skills reset google-workspace
anan skills reset google-workspace --restore --yes
```

Notes:
- `--force` can override non-dangerous policy blocks for third-party/community skills.
- `--force` does not override a `dangerous` scan verdict.
- `--source skills-sh` searches the public `skills.sh` directory.
- `--source well-known` lets you point anan Agent at a site exposing `/.well-known/skills/index.json`.
- Passing an `http(s)://…/*.md` URL installs a single-file SKILL.md directly. When frontmatter has no `name:` and the URL slug isn't a valid identifier, an interactive terminal prompts for a name; non-interactive surfaces (`/skills install` inside the TUI, gateway platforms) require `--name <x>` instead.

## `anan curator`

```bash
anan curator <subcommand>
```

The curator is an auxiliary-model background task that periodically reviews agent-created skills, prunes stale ones, consolidates overlaps, and archives obsolete skills. Bundled and hub-installed skills are never touched. Archives are recoverable; auto-deletion never happens.

| Subcommand | Description |
|------------|-------------|
| `status` | Show curator status and skill stats |
| `run` | Trigger a curator review now (blocks until the LLM pass finishes) |
| `run --background` | Start the LLM pass in a background thread and return immediately |
| `run --dry-run` | Preview only — produce the review report with no mutations |
| `backup` | Take a manual tar.gz snapshot of `~/.anan/skills/` (curator also snapshots automatically before every real run) |
| `rollback` | Restore `~/.anan/skills/` from a snapshot (defaults to newest) |
| `rollback --list` | List available snapshots |
| `rollback --id <ts>` | Restore a specific snapshot by id |
| `rollback -y` | Skip the confirmation prompt |
| `pause` | Pause the curator until resumed |
| `resume` | Resume a paused curator |
| `pin <skill>` | Pin a skill so the curator never auto-transitions it |
| `unpin <skill>` | Unpin a skill |
| `restore <skill>` | Restore an archived skill |

On a fresh install the first scheduled pass is deferred by one full `interval_hours` (7 days by default) — the gateway will not curate immediately on the first tick after `anan update`. Use `anan curator run --dry-run` to preview before that happens.

See [Curator](../user-guide/features/curator.md) for behavior and config.

## `anan fallback`

```bash
anan fallback <subcommand>
```

Manage the fallback provider chain. Fallback providers are tried in order when the primary model fails with rate-limit, overload, or connection errors.

| Subcommand | Description |
|------------|-------------|
| `list` (alias: `ls`) | Show the current fallback chain (default when no subcommand) |
| `add` | Pick a provider + model (same picker as `anan model`) and append to the chain |
| `remove` (alias: `rm`) | Pick an entry to delete from the chain |
| `clear` | Remove all fallback entries |

See [Fallback Providers](../user-guide/features/fallback-providers.md).

## `anan hooks`

```bash
anan hooks <subcommand>
```

Inspect shell-script hooks declared in `~/.anan/config.yaml`, test them against synthetic payloads, and manage the first-use consent allowlist at `~/.anan/shell-hooks-allowlist.json`.

| Subcommand | Description |
|------------|-------------|
| `list` (alias: `ls`) | List configured hooks with matcher, timeout, and consent status |
| `test <event>` | Fire every hook matching `<event>` against a synthetic payload |
| `revoke` (aliases: `remove`, `rm`) | Remove a command's allowlist entries (takes effect on next restart) |
| `doctor` | Check each configured hook: exec bit, allowlist, mtime drift, JSON validity, and synthetic run timing |

See [Hooks](../user-guide/features/hooks.md) for event signatures and payload shapes.

## `anan memory`

```bash
anan memory <subcommand>
```

Set up and manage external memory provider plugins. Available providers: honcho, openviking, mem0, hindsight, holographic, retaindb, byterover, supermemory. Only one external provider can be active at a time. Built-in memory (MEMORY.md/USER.md) is always active.

Subcommands:

| Subcommand | Description |
|------------|-------------|
| `setup` | Interactive provider selection and configuration. |
| `status` | Show current memory provider config. |
| `off` | Disable external provider (built-in only). |

:::info Provider-specific subcommands
When an external memory provider is active, it may register its own top-level `anan <provider>` command for provider-specific management (e.g. `anan honcho` when Honcho is active). Inactive providers do not expose their subcommands. Run `anan --help` to see what's currently wired in.
:::

## `anan acp`

```bash
anan acp
```

Starts anan Agent as an ACP (Agent Client Protocol) stdio server for editor integration.

Related entrypoints:

```bash
anan-acp
python -m acp_adapter
```

Install support first:

```bash
pip install -e '.[acp]'
```

See [ACP Editor Integration](../user-guide/features/acp.md) and [ACP Internals](../developer-guide/acp-internals.md).

## `anan mcp`

```bash
anan mcp <subcommand>
```

Manage MCP (Model Context Protocol) server configurations and run anan Agent as an MCP server.

| Subcommand | Description |
|------------|-------------|
| `serve [-v\|--verbose]` | Run anan Agent as an MCP server — expose conversations to other agents. |
| `add <name> [--url URL] [--command CMD] [--args ...] [--auth oauth\|header]` | Add an MCP server with automatic tool discovery. |
| `remove <name>` (alias: `rm`) | Remove an MCP server from config. |
| `list` (alias: `ls`) | List configured MCP servers. |
| `test <name>` | Test connection to an MCP server. |
| `configure <name>` (alias: `config`) | Toggle tool selection for a server. |

See [MCP Config Reference](./mcp-config-reference.md), [Use MCP with anan Agent](../guides/use-mcp-with-anan.md), and [MCP Server Mode](../user-guide/features/mcp.md#running-anan-as-an-mcp-server).

## `anan plugins`

```bash
anan plugins [subcommand]
```

Unified plugin management — general plugins, memory providers, and context engines in one place. Running `anan plugins` with no subcommand opens a composite interactive screen with two sections:

- **General Plugins** — multi-select checkboxes to enable/disable installed plugins
- **Provider Plugins** — single-select configuration for Memory Provider and Context Engine. Press ENTER on a category to open a radio picker.

| Subcommand | Description |
|------------|-------------|
| *(none)* | Composite interactive UI — general plugin toggles + provider plugin configuration. |
| `install <identifier> [--force]` | Install a plugin from a Git URL or `owner/repo`. |
| `update <name>` | Pull latest changes for an installed plugin. |
| `remove <name>` (aliases: `rm`, `uninstall`) | Remove an installed plugin. |
| `enable <name>` | Enable a disabled plugin. |
| `disable <name>` | Disable a plugin without removing it. |
| `list` (alias: `ls`) | List installed plugins with enabled/disabled status. |

Provider plugin selections are saved to `config.yaml`:
- `memory.provider` — active memory provider (empty = built-in only)
- `context.engine` — active context engine (`"compressor"` = built-in default)

General plugin disabled list is stored in `config.yaml` under `plugins.disabled`.

See [Plugins](../user-guide/features/plugins.md) and [Build a anan Agent Plugin](../guides/build-a-anan-plugin.md).

## `anan tools`

```bash
anan tools [--summary]
```

| Option | Description |
|--------|-------------|
| `--summary` | Print the current enabled-tools summary and exit. |

Without `--summary`, this launches the interactive per-platform tool configuration UI.

## `anan sessions`

```bash
anan sessions <subcommand>
```

Subcommands:

| Subcommand | Description |
|------------|-------------|
| `list` | List recent sessions. |
| `browse` | Interactive session picker with search and resume. |
| `export <output> [--session-id ID]` | Export sessions to JSONL. |
| `delete <session-id>` | Delete one session. |
| `prune` | Delete old sessions. |
| `stats` | Show session-store statistics. |
| `rename <session-id> <title>` | Set or change a session title. |

## `anan insights`

```bash
anan insights [--days N] [--source platform]
```

| Option | Description |
|--------|-------------|
| `--days <n>` | Analyze the last `n` days (default: 30). |
| `--source <platform>` | Filter by source such as `cli`, `telegram`, or `discord`. |

## `anan claw`

```bash
anan claw migrate [options]
```

Migrate your OpenClaw setup to anan Agent. Reads from `~/.anan` (or a custom path) and writes to `~/.anan`. Automatically detects legacy directory names (`~/.clawdbot`, `~/.moltbot`) and config filenames (`clawdbot.json`, `moltbot.json`).

| Option | Description |
|--------|-------------|
| `--dry-run` | Preview what would be migrated without writing anything. |
| `--preset <name>` | Migration preset: `full` (all compatible settings) or `user-data` (excludes infrastructure config). Neither preset imports secrets — pass `--migrate-secrets` explicitly. |
| `--overwrite` | Overwrite existing anan Agent files on conflicts (default: refuse to apply when the plan has conflicts). |
| `--migrate-secrets` | Include API keys in migration. Required even under `--preset full`. |
| `--no-backup` | Skip the pre-migration zip snapshot of `~/.anan/` (by default a single restore-point archive is written to `~/.anan/backups/pre-migration-*.zip` before apply; restorable with `anan import`). |
| `--source <path>` | Custom OpenClaw directory (default: `~/.anan`). |
| `--workspace-target <path>` | Target directory for workspace instructions (AGENTS.md). |
| `--skill-conflict <mode>` | Handle skill name collisions: `skip` (default), `overwrite`, or `rename`. |
| `--yes` | Skip the confirmation prompt. |

### What gets migrated

The migration covers 30+ categories across persona, memory, skills, model providers, messaging platforms, agent behavior, session policies, MCP servers, TTS, and more. Items are either **directly imported** into anan Agent equivalents or **archived** for manual review.

**Directly imported:** SOUL.md, MEMORY.md, USER.md, AGENTS.md, skills (4 source directories), default model, custom providers, MCP servers, messaging platform tokens and allowlists (Telegram, Discord, Slack, WhatsApp, Signal, Matrix, Mattermost), agent defaults (reasoning effort, compression, human delay, timezone, sandbox), session reset policies, approval rules, TTS config, browser settings, tool settings, exec timeout, command allowlist, gateway config, and API keys from 3 sources.

**Archived for manual review:** Cron jobs, plugins, hooks/webhooks, memory backend (QMD), skills registry config, UI/identity, logging, multi-agent setup, channel bindings, IDENTITY.md, TOOLS.md, HEARTBEAT.md, BOOTSTRAP.md.

**API key resolution** checks three sources in priority order: config values → `~/.anan/.env` → `auth-profiles.json`. All token fields handle plain strings, env templates (`${VAR}`), and SecretRef objects.

For the complete config key mapping, SecretRef handling details, and post-migration checklist, see the **[full migration guide](../guides/migrate-from-openclaw.md)**.

### Examples

```bash
# Preview what would be migrated
anan claw migrate --dry-run

# Full migration (all compatible settings, no secrets)
anan claw migrate --preset full

# Full migration including API keys
anan claw migrate --preset full --migrate-secrets

# Migrate user data only (no secrets), overwrite conflicts
anan claw migrate --preset user-data --overwrite

# Migrate from a custom OpenClaw path
anan claw migrate --source /home/user/old-openclaw
```

## `anan dashboard`

```bash
anan dashboard [options]
```

Launch the web dashboard — a browser-based UI for managing configuration, API keys, and monitoring sessions. Requires `pip install anan[web]` (FastAPI + Uvicorn). See [Web Dashboard](/docs/user-guide/features/web-dashboard) for full documentation.

| Option | Default | Description |
|--------|---------|-------------|
| `--port` | `9119` | Port to run the web server on |
| `--host` | `127.0.0.1` | Bind address |
| `--no-open` | — | Don't auto-open the browser |

```bash
# Default — opens browser to http://127.0.0.1:9119
anan dashboard

# Custom port, no browser
anan dashboard --port 8080 --no-open
```

## `anan profile`

```bash
anan profile <subcommand>
```

Manage profiles — multiple isolated anan Agent instances, each with its own config, sessions, skills, and home directory.

| Subcommand | Description |
|------------|-------------|
| `list` | List all profiles. |
| `use <name>` | Set a sticky default profile. |
| `create <name> [--clone] [--clone-all] [--clone-from <source>] [--no-alias]` | Create a new profile. `--clone` copies config, `.env`, and `SOUL.md` from the active profile. `--clone-all` copies all state. `--clone-from` specifies a source profile. |
| `delete <name> [-y]` | Delete a profile. |
| `show <name>` | Show profile details (home directory, config, etc.). |
| `alias <name> [--remove] [--name NAME]` | Manage wrapper scripts for quick profile access. |
| `rename <old> <new>` | Rename a profile. |
| `export <name> [-o FILE]` | Export a profile to a `.tar.gz` archive (local backup). |
| `import <archive> [--name NAME]` | Import a profile from a `.tar.gz` archive (local restore). |
| `install <source> [--name N] [--alias] [--force] [-y]` | Install a profile distribution from a git URL or local directory. |
| `update <name> [--force-config] [-y]` | Re-pull a distribution; preserves user data (memories, sessions, auth). |
| `info <name>` | Show a profile's distribution manifest (version, requirements, source). |

Examples:

```bash
anan profile list
anan profile create work --clone
anan profile use work
anan profile alias work --name h-work
anan profile export work -o work-backup.tar.gz
anan profile import work-backup.tar.gz --name restored
anan profile install github.com/user/my-distro --alias
anan profile update work
anan -p work chat -q "Hello from work profile"
```

## `anan completion`

```bash
anan completion [bash|zsh|fish]
```

Print a shell completion script to stdout. Source the output in your shell profile for tab-completion of anan Agent commands, subcommands, and profile names.

Examples:

```bash
# Bash
anan completion bash >> ~/.bashrc

# Zsh
anan completion zsh >> ~/.zshrc

# Fish
anan completion fish > ~/.config/fish/completions/anan.fish
```

## `anan update`

```bash
anan update [--check] [--backup] [--restart-gateway]
```

Pulls the latest `anan` code and reinstalls dependencies in your venv, then re-runs the post-install hooks (MCP servers, skills sync, completion install). Safe to run on a live install.

| Option | Description |
|--------|-------------|
| `--check` | Print the current commit and the latest `origin/main` commit side by side, and exit 0 if in sync or 1 if behind. Does not pull, install, or restart anything. |
| `--backup` | Create a labeled pre-update snapshot of `ANAN_HOME` (config, auth, sessions, skills, pairing data) before pulling. Default is **off** — the previous always-backup behavior was adding minutes to every update on large homes. Flip it on permanently via `update.backup: true` in `config.yaml`. |
| `--restart-gateway` | After a successful update, restart the running gateway service. Implies `--all` semantics if multiple profiles are installed. |

Additional behavior:

- **Pairing data snapshot.** Even when `--backup` is off, `anan update` takes a lightweight snapshot of `~/.anan/pairing/` and the Feishu comment rules before `git pull`. You can roll it back with `anan backup restore --state pre-update` if a pull rewrites a file you were editing.
- **Legacy `anan.service` warning.** If anan Agent detects a pre-rename `anan.service` systemd unit (instead of the current `anan-gateway.service`), it prints a one-time migration hint so you can avoid flap-loop issues.
- **Exit codes.** `0` on success, `1` on pull/install/post-install errors, `2` on unexpected working-tree changes that block `git pull`.

## `anan fallback`

```bash
anan fallback           # interactive manager
```

Manage the fallback provider chain (used when your primary provider hits a rate limit or returns a fatal error) without hand-editing `config.yaml`. Reuses the provider picker from `anan model` — same provider list, same credential prompts, same validation.

Typical session:

1. Press `a` to add a fallback → pick a provider (OAuth-based providers open a browser; API-key providers prompt for the key), then pick the specific model.
2. Use `↑`/`↓` to reorder fallbacks (first-in-list is tried first).
3. Press `d` to remove one.

All changes persist to the top-level `fallback_providers:` list in `config.yaml`. Interacts with [Credential Pools](/docs/user-guide/features/credential-pools): pools rotate keys *within* a provider, fallbacks switch to a *different* provider entirely.

See [Fallback Providers](/docs/user-guide/features/fallback-providers) for behavior details and interaction with `fallback_model` (legacy single-fallback key).

## Maintenance commands

| Command | Description |
|---------|-------------|
| `anan version` | Print version information. |
| `anan update` | Pull latest changes and reinstall dependencies. |
| `anan uninstall [--full] [--yes]` | Remove anan Agent, optionally deleting all config/data. |

## See also

- [Slash Commands Reference](./slash-commands.md)
- [CLI Interface](../user-guide/cli.md)
- [Sessions](../user-guide/sessions.md)
- [Skills System](../user-guide/features/skills.md)
- [Skins & Themes](../user-guide/features/skins.md)
