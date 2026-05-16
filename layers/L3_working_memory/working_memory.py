"""
L3 Working Memory — anan 的短时记忆缓冲
========================================

人脑里 working memory 是 7±2 chunks 的活跃缓冲：
  - 最近发生的事还"温热"
  - 旧的会 decay 出去
  - 显著的（高 salience）会留更久

这一层订阅 EventBus，把"值得短期记住"的事件存到滑动窗口。
比 raw bus.history() 强在哪里？
  1. **有界**：buffer 满了自动淘汰最不重要的（不是单纯 FIFO）
  2. **打分**：每个事件按 topic + source + payload 算 salience
  3. **decay**: 时间越久权重越低（配合 salience 决定留谁）
  4. **可查询**：recall_recent(n=K) 返回最值得回想的 K 个

为什么不直接放 EventBus.history()?
  bus history 是 raw firehose，所有事件平等。working memory 是已经过滤+打分的
  "意识焦点"。L1 sleep 反思时优先从这里取，能省 reflect 算力，结果质量更高。

事件:
  L3.working_memory.captured  — 一个事件被纳入
  L3.working_memory.evicted   — 一个事件被挤出
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.L3.working_memory")


# Salience scorer: takes an event, returns a float [0.0 ... 1.0+]
SalienceScorer = Callable[[Event], float]


def default_salience(event: Event) -> float:
    """Default heuristic. Override via WorkingMemory(salience_fn=...).

    Rules of thumb:
      - L0.circadian.tick is low (it's just background heartbeat)
      - L0.circadian.bedtime / wake / asleep are high (lifecycle moments)
      - L1.sleep.* events are high (cognitive moments)
      - L2.memory.persisted is high (long-term commit moments)
      - L9.self.* are top (identity-touching moments)
      - Anything else: 0.5
    """
    t = event.topic
    if t == "L0.circadian.tick":
        return 0.1
    if t.startswith("L0.circadian."):
        return 0.7
    if t.startswith("L1.sleep."):
        return 0.8
    if t.startswith("L2.memory."):
        return 0.85
    if t.startswith("L9."):
        return 0.95
    return 0.5


@dataclass
class WorkingMemoryEntry:
    event: Event
    captured_at: float
    salience: float

    def weight(self, *, now: float, half_life_s: float) -> float:
        """Combined retention weight = salience * decay(age).

        Half-life decay: weight halves every half_life_s seconds.
        """
        age = max(0.0, now - self.captured_at)
        decay = 0.5 ** (age / half_life_s) if half_life_s > 0 else 1.0
        return self.salience * decay


class WorkingMemory:
    """Bounded short-term memory with salience-aware eviction.

    Usage:
        wm = WorkingMemory(capacity=64)
        await wm.attach(bus)             # subscribes to **
        recent = wm.recall_recent(5)     # top-5 by current weight
        await wm.detach()
    """

    def __init__(
        self,
        *,
        capacity: int = 64,
        half_life_s: float = 60.0,
        salience_fn: Optional[SalienceScorer] = None,
        min_salience: float = 0.05,
    ):
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self.capacity = capacity
        self.half_life_s = half_life_s
        self.salience_fn = salience_fn or default_salience
        self.min_salience = min_salience
        self._entries: list[WorkingMemoryEntry] = []
        self._bus: Optional[EventBus] = None
        self._unsub: Optional[Callable[[], None]] = None
        self._lock: asyncio.Lock = asyncio.Lock()  # created here (not lazy) so it's bound to the event loop that constructs this object
        self.captured_total = 0
        self.evicted_total = 0

    async def attach(self, bus: Optional[EventBus] = None) -> None:
        # Lock is created once in __init__ (sync context, bound to gateway main event loop).
        # Do NOT recreate it here — creating it in async context would bind it to whatever
        # loop is current at attach() time, which may differ from the loop that will use it.
        self._bus = bus or get_bus()
        # Subscribe to everything; we filter out our own L3.* events in _on_event
        # to avoid a feedback loop. Bare "*" in this bus matches all topics.
        self._unsub = self._bus.subscribe("*", self._on_event)

    async def detach(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        self._lock = None

    async def _on_event(self, event: Event) -> None:
        # Don't capture our own events — would create a feedback loop
        if event.topic.startswith("L3.working_memory."):
            return
        if self._lock is None:
            return  # not attached yet
        salience = self.salience_fn(event)
        if salience < self.min_salience:
            return
        async with self._lock:
            entry = WorkingMemoryEntry(
                event=event, captured_at=time.time(), salience=salience,
            )
            self._entries.append(entry)
            self.captured_total += 1
            await self._publish_meta("captured", entry)
            await self._evict_if_needed()

    async def _evict_if_needed(self) -> None:
        if len(self._entries) <= self.capacity:
            return
        # Find lowest-weight entry and remove it
        now = time.time()
        weighted = [(e.weight(now=now, half_life_s=self.half_life_s), i, e)
                    for i, e in enumerate(self._entries)]
        weighted.sort(key=lambda x: x[0])
        _, idx, victim = weighted[0]
        self._entries.pop(idx)
        self.evicted_total += 1
        await self._publish_meta("evicted", victim)

    async def _publish_meta(self, kind: str, entry: WorkingMemoryEntry) -> None:
        if not self._bus:
            return
        try:
            await self._bus.publish(Event(
                topic=f"L3.working_memory.{kind}",
                source="L3.working_memory",
                payload={
                    "topic": entry.event.topic,
                    "salience": entry.salience,
                    "size": len(self._entries),
                },
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("L3 meta-publish failed (non-fatal): %s", exc)

    # ---- public read API ----

    def recall_recent(self, n: int = 5) -> list[WorkingMemoryEntry]:
        """Top-N entries by current weight (salience * decay)."""
        now = time.time()
        ranked = sorted(
            self._entries,
            key=lambda e: e.weight(now=now, half_life_s=self.half_life_s),
            reverse=True,
        )
        return ranked[:n]

    def snapshot(self) -> list[WorkingMemoryEntry]:
        """All current entries in capture order (oldest first)."""
        return list(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def stats(self) -> dict:
        return {
            "size": len(self._entries),
            "capacity": self.capacity,
            "captured_total": self.captured_total,
            "evicted_total": self.evicted_total,
            "half_life_s": self.half_life_s,
        }
