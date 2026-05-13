"""
anan adapter — Sleep Awareness
================================

Wraps L1 sleep phases with event_bus signals so the rest of the
mind stack can react.

Why a separate adapter (not editing sleep_plugin.py directly)?
- sleep_plugin.py is grafted from OpenClaw and we want to track upstream
- The adapter pattern keeps "raw sleep mechanics" separate from "cognitive awareness"
- Other layers (L2 memory, L9 self) listen to bus events; they don't need
  to know how sleep actually works internally

Event topics emitted:
    L1.sleep.<phase>.start       — phase begins (payload: {phase, day, config})
    L1.sleep.<phase>.consolidated — phase succeeded (payload: {phase, day, recall_count})
    L1.sleep.<phase>.failed      — phase raised (payload: {phase, day, error})

These are the first cognitive signals anan emits. When you see these in
bus.history(), the brain is awake.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.adapters.sleep_awareness")


# Type alias: a sleep phase function (light/rem/deep) that takes config + day
# and returns whatever sleep_plugin returns
SleepPhaseFn = Callable[..., Awaitable[Any]]


async def run_with_awareness(
    phase: str,
    fn: SleepPhaseFn,
    *args,
    _anan_day: Optional[str] = None,
    _anan_bus: Optional[EventBus] = None,
    **kwargs,
) -> Any:
    """Run a sleep phase and broadcast its lifecycle on the event bus.

    Args:
        phase: Phase name — "light", "rem", or "deep"
        fn: The actual sleep function from sleep_plugin
            (run_light_sleep_phase / run_rem_sleep_phase / run_deep_sleep_phase)
        _anan_day: Optional day identifier (YYYY-MM-DD) for the dream entry.
            Underscore-prefixed so it never collides with fn's own kwargs.
        _anan_bus: Override the global bus (mostly for testing). Same.
        *args, **kwargs: Forwarded to fn untouched

    Returns:
        Whatever fn returned, untouched.

    Emits three lifecycle events on the bus:
        L1.sleep.<phase>.start         — before fn runs
        L1.sleep.<phase>.consolidated  — after fn returns successfully
        L1.sleep.<phase>.failed        — after fn raises (then re-raises)
    """
    bus = _anan_bus or get_bus()
    day = _anan_day
    started_at = time.time()

    await bus.publish(Event(
        topic=f"L1.sleep.{phase}.start",
        source="L1.sleep_awareness",
        payload={"phase": phase, "day": day, "started_at": started_at},
    ))

    try:
        result = await fn(*args, **kwargs)
    except Exception as exc:
        await bus.publish(Event(
            topic=f"L1.sleep.{phase}.failed",
            source="L1.sleep_awareness",
            payload={
                "phase": phase,
                "day": day,
                "error": repr(exc),
                "duration_s": time.time() - started_at,
            },
        ))
        raise

    # Try to extract a "recall_count" hint if the result looks like a dict
    # with metadata; otherwise fall back to None
    recall_count = None
    if isinstance(result, dict):
        recall_count = result.get("recall_count") or result.get("count")

    await bus.publish(Event(
        topic=f"L1.sleep.{phase}.consolidated",
        source="L1.sleep_awareness",
        payload={
            "phase": phase,
            "day": day,
            "duration_s": time.time() - started_at,
            "recall_count": recall_count,
        },
    ))

    return result


def make_aware(
    phase: str,
    fn: SleepPhaseFn,
    *,
    bus: Optional[EventBus] = None,
) -> SleepPhaseFn:
    """Decorate a sleep phase function so every call publishes lifecycle events.

    Usage:
        from layers.L1_sleep.sleep_plugin import run_light_sleep_phase
        aware_light = make_aware("light", run_light_sleep_phase)
        await aware_light(config, day="2026-05-14")
    """
    async def wrapped(*args, **kwargs):
        # Read `day` for the awareness signal but DO NOT consume it
        # — the inner fn may also need it
        day = kwargs.get("day")
        return await run_with_awareness(
            phase, fn, *args, _anan_day=day, _anan_bus=bus, **kwargs
        )

    wrapped.__name__ = f"aware_{phase}_{fn.__name__}"
    wrapped.__doc__ = (
        f"Sleep phase '{phase}' wrapped with anan awareness signals.\n\n"
        f"Original docstring:\n{fn.__doc__ or '(none)'}"
    )
    return wrapped
