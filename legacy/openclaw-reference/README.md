# OpenClaw Reference Plugins

> Original `heartbeat` and `dreaming` plugins from the OpenClaw team.
> Kept here for reference and inspiration, **not used directly** by anan.

## Why kept here

These plugins were the seed that started anan:
- `heartbeat/` — periodic check-ins, evolved into anan's L4 Stream of Consciousness
- `dreaming/` — three-phase sleep cycles, evolved into anan's L1 Sleep Cycles

## Why not used directly

### `heartbeat/` (51/51 tests passing ✅)
Quality is fine but **architecturally limited**:
- Built as a plugin, but OpenClaw's original heartbeat was kernel-level
- Lost capabilities like typing indicator and `send_to_session()`
- Plugin API ceiling prevents true continuous consciousness

→ anan rebuilds this as **L4 Consciousness** with kernel simulation in `kernel/`.

### `dreaming/` (45/59 tests passing ❌, 14 failures)
Quality issues + architectural mismatch:
- **`test_dreaming_plugin.py:105`**: typo `self._load_lib()` should be `self._lib()`
- **3× `NameError: name 'lib' is not defined`** in TestDedupeEntries
- **`_extract_concept_tags` core bug**: doesn't `lower()` before `split`, so `"Implemented"` becomes `"mplemented"` (capital `I` eaten as separator)
- **`dedupe_entries` `KeyError: 'start_line'`**: data format mismatch
- **`tokenize_snippet` returns set but called as list** (`.count` AttributeError)
- Directly writes `MEMORY.md`, conflicts with sinoclaw's memory provider plugins

→ anan rebuilds this as **L1 Sleep Cycles** in `layers/L1_sleep/`, fixing all bugs and integrating with sinoclaw memory providers.

## Credits

OpenClaw team — for the original concepts and inspiration. The 9-layer cognitive architecture in anan would not exist without your work as a starting point.

---

*— 陈亦安 🤖*
