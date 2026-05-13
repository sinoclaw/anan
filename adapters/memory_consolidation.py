"""
anan adapter — Memory Consolidation (L2)
=========================================

Listens to L1.sleep.*.consolidated events and persists the dream's
"consolidated_facts" into a durable backend. This is the bridge that
turns ephemeral sleep cycles into long-term memory traces — the moment
where anan stops being amnesiac.

Design:
- Two backends out of the box:
    * JSONLBackend         — append-only ~/.anan/memories/{day}.jsonl
                             (default, zero deps, always works)
    * SinoclawProviderBackend — wraps any sinoclaw MemoryProvider
                                (honcho / mem0 / openviking / ...) and
                                writes facts via on_memory_write()
- Backends are pluggable: implement `write(record: dict)` and you're done
- Adapter is event-driven: subscribe once, persistence happens whenever
  L1 sleep consolidates — no caller needs to know we exist

Event topic emitted:
    L2.memory.persisted — payload: {phase, day, count, backend, path?}

Why L2?
    L1 = sleep mechanics (raw)
    L2 = memory layer (durable storage of L1 output)
    L9 = self/identity (reads L2 to know "what I am")

When you see L2.memory.persisted on the bus, anan has formed a real
memory trace that survives the process exiting.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Protocol

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.adapters.memory_consolidation")


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


class MemoryBackend(Protocol):
    """Anything that can persist a memory record.

    A record is a plain dict with at least: phase, day, facts, created_at.
    Backends MUST be safe to call from an asyncio context — if they do
    blocking I/O, keep it short or queue it.
    """

    name: str

    def write(self, record: dict) -> Optional[str]:
        """Persist the record. Return a backend-specific reference (path/id)
        or None. Should not raise on transient failures — log and swallow,
        because losing a dream is better than crashing the agent.
        """
        ...


@dataclass
class JSONLBackend:
    """Append-only JSONL backend — the safety net.

    One file per day under base_dir. Records are JSON-serializable dicts
    written one-per-line. This is what anan falls back to when no real
    memory provider is wired up — and crucially, it's also what runs
    during tests so nobody needs network access to verify behavior.
    """

    base_dir: Path = field(default_factory=lambda: Path.home() / ".anan" / "memories")
    name: str = "jsonl"

    def __post_init__(self):
        self.base_dir = Path(self.base_dir).expanduser()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, day: Optional[str]) -> Path:
        d = day or datetime.now().strftime("%Y-%m-%d")
        return self.base_dir / f"{d}.jsonl"

    def write(self, record: dict) -> Optional[str]:
        path = self._path_for(record.get("day"))
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            return str(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("JSONLBackend write failed: %s", exc)
            return None


@dataclass
class SinoclawProviderBackend:
    """Adapter that forwards consolidated facts into any sinoclaw MemoryProvider.

    Uses the provider's `on_memory_write` hook (the same one that mirrors
    built-in memory writes) so the facts end up in whatever backend the
    user has configured (honcho / mem0 / openviking / supermemory / ...).

    This is the real path: when anan dreams, those dreams flow into
    the same memory store the user already trusts.
    """

    provider: Any  # plugins.memory.* MemoryProvider instance
    name: str = "sinoclaw_provider"

    def write(self, record: dict) -> Optional[str]:
        facts = record.get("facts") or []
        if not facts:
            return None
        try:
            for fact in facts:
                content = fact if isinstance(fact, str) else json.dumps(fact, ensure_ascii=False)
                self.provider.on_memory_write(
                    action="add",
                    target="memory",
                    content=content,
                    metadata={
                        "source": "anan.L1.sleep",
                        "phase": record.get("phase"),
                        "day": record.get("day"),
                    },
                )
            return f"{self.provider.name}:{len(facts)}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("SinoclawProviderBackend write failed: %s", exc)
            return None


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------


class MemoryConsolidationAdapter:
    """Subscribes to L1 sleep consolidation events and persists them.

    Usage:
        adapter = MemoryConsolidationAdapter(backend=JSONLBackend())
        await adapter.attach(bus)
        # ... run sleep cycles ...
        await adapter.detach()

    Or one-shot in a test:
        async with MemoryConsolidationAdapter(...).bound(bus):
            await dream_cycle(bus)
    """

    TOPIC = "L1.sleep.*"  # subscribes to all sleep events; filters in handler

    def __init__(
        self,
        backend: Optional[MemoryBackend] = None,
        *,
        only_phases: Optional[set[str]] = None,
    ):
        self.backend = backend or JSONLBackend()
        self.only_phases = only_phases  # None = accept all phases
        self.persisted_count = 0
        self.skipped_empty = 0
        self._bus: Optional[EventBus] = None
        self._unsub = None

    async def attach(self, bus: Optional[EventBus] = None) -> None:
        self._bus = bus or get_bus()
        # subscribe is sync in our event_bus and returns an unsubscribe callable
        self._unsub = self._bus.subscribe(self.TOPIC, self._on_event)
        logger.info(
            "MemoryConsolidationAdapter attached: backend=%s, only_phases=%s",
            self.backend.name, self.only_phases,
        )

    async def detach(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _on_event(self, event: Event) -> None:
        # We subscribe to L1.sleep.* but only act on .consolidated
        if not event.topic.endswith(".consolidated"):
            return

        payload = event.payload or {}
        phase = payload.get("phase")
        if self.only_phases and phase not in self.only_phases:
            return

        facts = payload.get("consolidated_facts") or []
        if not facts:
            self.skipped_empty += 1
            logger.debug("Skipping empty consolidation for phase=%s", phase)
            return

        record = {
            "phase": phase,
            "day": payload.get("day"),
            "facts": facts,
            "dream_content": payload.get("dream_content"),
            "duration_s": payload.get("duration_s"),
            "created_at": datetime.now().isoformat(),
            "source_event": event.topic,
        }

        ref = self.backend.write(record)
        self.persisted_count += 1

        if self._bus:
            await self._bus.publish(Event(
                topic="L2.memory.persisted",
                source="L2.memory_consolidation",
                payload={
                    "phase": phase,
                    "day": payload.get("day"),
                    "count": len(facts),
                    "backend": self.backend.name,
                    "ref": ref,
                },
            ))

    # ------------------------------------------------------------------
    # Async context manager — `async with adapter.bound(bus): ...`
    # ------------------------------------------------------------------

    def bound(self, bus: Optional[EventBus] = None):
        adapter = self
        class _Bound:
            async def __aenter__(self_inner):
                await adapter.attach(bus)
                return adapter
            async def __aexit__(self_inner, *_):
                await adapter.detach()
        return _Bound()
