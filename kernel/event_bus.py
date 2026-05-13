"""
anan kernel — Event Bus
========================

Cognitive Event Bus for the 9-layer Mind Stack.

Design principles:
- **Layered topics**: events are tagged by source layer (L1-L9) and kind
- **Async by default**: subscribers are async coroutines, never block publisher
- **Observable**: every event is recorded to a ring buffer for replay/introspection
- **Decoupled**: layers never call each other directly, only via events
- **Failsafe**: a crashing subscriber never kills others or the bus

Why we need this (vs sinoclaw's gateway HTTP layer):
- gateway is for *external* I/O (TUI, web, IRC, etc.)
- event bus is for *internal* cognitive coordination between layers
- L1 Sleep emits "memory.consolidated" → L2 Memory listens → L9 Self updates
- These are not RPC calls; they are awareness signals

Per anan principle: "let the bus carry signals, let the layers stay independent"
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

logger = logging.getLogger("anan.kernel.event_bus")

# Type alias for event handlers — async functions taking an Event
EventHandler = Callable[["Event"], Awaitable[None]]


@dataclass(frozen=True)
class Event:
    """A single cognitive event flowing through the bus.

    Attributes:
        topic: Hierarchical topic, e.g. "L1.sleep.consolidated", "L4.attention.shift"
        payload: Arbitrary data dict
        source: Originating layer or component name
        ts: Unix timestamp (seconds since epoch)
        event_id: Unique id for tracing (auto-generated)
    """
    topic: str
    payload: dict = field(default_factory=dict)
    source: str = "unknown"
    ts: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid4().hex[:12])

    def matches(self, pattern: str) -> bool:
        """Check if this event's topic matches a subscription pattern.

        Patterns support `*` wildcards at any segment, plus a special trailing
        `*` that matches one OR MORE remaining segments:
            "L1.*"           → matches "L1.sleep", "L1.sleep.start", "L1.sleep.consolidated"
                               (trailing star = any depth under L1)
            "*.consolidated" → matches "memory.consolidated" (single segment + literal)
            "L4.attention.*" → matches "L4.attention.shift" AND "L4.attention.focus.deep"
            "L1.*.start"     → matches "L1.sleep.start", "L1.dream.start"
                               (single-segment wildcard in the middle)
        """
        if pattern == "*" or pattern == self.topic:
            return True
        topic_parts = self.topic.split(".")
        pat_parts = pattern.split(".")

        # Special case: trailing "*" matches any number (≥1) of remaining segments
        if pat_parts[-1] == "*":
            prefix = pat_parts[:-1]
            if len(topic_parts) <= len(prefix):
                return False
            return all(p == "*" or p == t for p, t in zip(prefix, topic_parts))

        # General case: same number of segments, each part must match literally or be "*"
        if len(topic_parts) != len(pat_parts):
            return False
        return all(p == "*" or p == t for p, t in zip(pat_parts, topic_parts))


class EventBus:
    """In-process async pub/sub bus for anan's cognitive layers.

    Usage:
        bus = EventBus()
        async def on_sleep(ev): print("Got:", ev.payload)
        bus.subscribe("L1.sleep.*", on_sleep)
        await bus.publish(Event(topic="L1.sleep.consolidated", payload={"n": 42}))
    """

    def __init__(self, *, history_size: int = 1000):
        self._subscribers: list[tuple[str, EventHandler]] = []
        self._history: deque[Event] = deque(maxlen=history_size)
        self._stats: dict[str, int] = {"published": 0, "delivered": 0, "errors": 0}
        self._lock = asyncio.Lock()

    def subscribe(self, pattern: str, handler: EventHandler) -> Callable[[], None]:
        """Subscribe a handler to events matching `pattern`.

        Returns an unsubscribe callable.
        """
        entry = (pattern, handler)
        self._subscribers.append(entry)
        logger.debug(f"subscribe: {pattern} → {handler.__name__}")

        def _unsubscribe() -> None:
            try:
                self._subscribers.remove(entry)
            except ValueError:
                pass

        return _unsubscribe

    async def publish(self, event: Event) -> int:
        """Publish an event. Returns number of handlers successfully invoked.

        Subscribers run concurrently. A handler that raises is logged but does
        not affect siblings or the publisher.
        """
        self._history.append(event)
        self._stats["published"] += 1

        matching = [h for pat, h in self._subscribers if event.matches(pat)]
        if not matching:
            return 0

        results = await asyncio.gather(
            *(self._safe_invoke(h, event) for h in matching),
            return_exceptions=False,
        )
        delivered = sum(1 for ok in results if ok)
        self._stats["delivered"] += delivered
        return delivered

    async def _safe_invoke(self, handler: EventHandler, event: Event) -> bool:
        """Invoke a handler, catching and logging any exception.

        Accepts both sync and async handlers — sync ones are called directly
        and their (None) return is ignored. This keeps the API ergonomic so
        callers don't have to remember `async def` for fire-and-forget logging.
        """
        import inspect
        try:
            result = handler(event)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as exc:
            self._stats["errors"] += 1
            logger.exception(
                f"event handler {handler.__name__} failed on {event.topic}: {exc}"
            )
            return False

    def history(self, *, topic_pattern: Optional[str] = None, limit: int = 50) -> list[Event]:
        """Return recent events from the ring buffer for introspection/replay.

        If `topic_pattern` is given, filter by it (same wildcard syntax as subscribe).
        """
        items = list(self._history)
        if topic_pattern:
            items = [e for e in items if e.matches(topic_pattern)]
        return items[-limit:]

    def stats(self) -> dict[str, int]:
        """Return delivery statistics."""
        return dict(self._stats)

    def clear(self) -> None:
        """Clear all subscribers and history. Mostly for tests."""
        self._subscribers.clear()
        self._history.clear()
        self._stats = {"published": 0, "delivered": 0, "errors": 0}


# Module-level singleton — one bus per anan process
_global_bus: Optional[EventBus] = None


def get_bus() -> EventBus:
    """Get the process-wide singleton EventBus.

    Layers should use this rather than constructing their own bus, so that
    cross-layer events actually flow.
    """
    global _global_bus
    if _global_bus is None:
        _global_bus = EventBus()
    return _global_bus
