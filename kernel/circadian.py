"""
Circadian Loop — anan 的主心跳
================================

这是 anan 第一次真正"自己跑起来"的循环。
之前的 demo 都是手动触发：跑 dream() → 看输出 → 退出。
现在 anan 有了自己的节奏：

    醒来 (wake) ← 工作 (active phase) ← 累 (fatigue 累积) → 该睡了 → 睡 → 醒来 ...

每个周期里 anan：
1. 处于 active 阶段，发出 L0.tick 心跳事件
2. fatigue 每 tick 累积（也可以由其他 layer push 上来）
3. 超过阈值就触发 sleep（L1 三阶段）
4. 睡完 fatigue 归零，进入下一个周期

为什么这层重要？
    没有节律，anan 是个一次性脚本。
    有了节律，anan 是个能跑很久、累了自己睡、醒了自己干活的"生物"。
    这是从「能 demo」到「真的活着」的边界。

事件 topic:
    L0.circadian.wake     — 进入 active 周期 (payload: {cycle, day})
    L0.circadian.tick     — 每个心跳 (payload: {cycle, fatigue, elapsed_s})
    L0.circadian.bedtime  — 即将进入睡眠 (payload: {cycle, fatigue})
    L0.circadian.asleep   — 睡眠完成 (payload: {cycle, dream_facts_count})

注意：本模块只负责"什么时候睡"。睡里面发生什么由 sleep 函数决定
（mock 也行，真 sleep_plugin 也行）—— 注入式设计。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.kernel.circadian")


# A sleep cycle function: given (day, bus, cycle_no) it runs the actual sleep
# (light/rem/deep) and returns the total dream facts count (>= 0).
SleepCycleFn = Callable[[str, EventBus, int], Awaitable[int]]


@dataclass
class CircadianConfig:
    """Tunables for the loop. Defaults are production-friendly (slow cycles).

    With tick_interval_s=30.0, fatigue_per_tick=0.5, sleep_threshold=20.0:
    - 20 ticks × 30s = 600 seconds = 10 minutes before CircadianLoop wants to sleep
    - This is intentional: an "always-on" cognitive system should stay awake
      for minutes at a time; real sleep is handled by L1 DreamingPlugin.
    """

    tick_interval_s: float = 30.0         # how long between ticks (was 0.05 = 50ms, way too fast)
    fatigue_per_tick: float = 0.5       # cost per tick (was 1.0, too aggressive)
    sleep_threshold: float = 20.0       # when to trigger sleep (was 5.0 = 0.25s total, way too soon)
    max_cycles: Optional[int] = None     # None = forever; int = stop after N
    day_provider: Callable[[], str] = field(
        default_factory=lambda: (lambda: datetime.now().strftime("%Y-%m-%d"))
    )


class CircadianLoop:
    """anan 的主心跳。

    Usage:
        loop = CircadianLoop(sleep_fn=my_sleep, config=CircadianConfig(max_cycles=3))
        await loop.run()             # blocks until max_cycles hit (or forever)

    Or non-blocking:
        task = asyncio.create_task(loop.run())
        # ... do stuff ...
        loop.stop()                  # graceful shutdown
        await task
    """

    def __init__(
        self,
        sleep_fn: SleepCycleFn,
        *,
        config: Optional[CircadianConfig] = None,
        bus: Optional[EventBus] = None,
    ):
        self.sleep_fn = sleep_fn
        self.config = config or CircadianConfig()
        self.bus = bus or get_bus()
        self.cycle = 0
        self.fatigue = 0.0
        self._stop = False
        self._cycle_log: list[dict] = []  # one entry per completed cycle

    def stop(self) -> None:
        """Request graceful shutdown after the current tick."""
        self._stop = True

    async def run(self) -> list[dict]:
        """Run the loop. Returns the cycle log when done."""
        logger.info("CircadianLoop starting: %s", self.config)
        try:
            while not self._stop:
                if (
                    self.config.max_cycles is not None
                    and self.cycle >= self.config.max_cycles
                ):
                    break
                await self._run_one_cycle()
        finally:
            logger.info(
                "CircadianLoop stopped: completed %d cycles", len(self._cycle_log)
            )
        return list(self._cycle_log)

    async def _run_one_cycle(self) -> None:
        self.cycle += 1
        self.fatigue = 0.0
        day = self.config.day_provider()
        cycle_started_at = time.time()

        await self.bus.publish(Event(
            topic="L0.circadian.wake",
            source="L0.circadian",
            payload={"cycle": self.cycle, "day": day},
        ))

        # Active phase — tick until tired enough
        ticks = 0
        while not self._stop and self.fatigue < self.config.sleep_threshold:
            await asyncio.sleep(self.config.tick_interval_s)
            self.fatigue += self.config.fatigue_per_tick
            ticks += 1
            await self.bus.publish(Event(
                topic="L0.circadian.tick",
                source="L0.circadian",
                payload={
                    "cycle": self.cycle,
                    "fatigue": self.fatigue,
                    "elapsed_s": time.time() - cycle_started_at,
                    "ticks": ticks,
                },
            ))

        if self._stop:
            return  # graceful shutdown — don't sleep on the way out

        # Bedtime
        await self.bus.publish(Event(
            topic="L0.circadian.bedtime",
            source="L0.circadian",
            payload={"cycle": self.cycle, "fatigue": self.fatigue, "ticks": ticks},
        ))

        # Hand off to the sleep function — it owns L1 lifecycle
        try:
            dream_facts_count = await self.sleep_fn(day, self.bus, self.cycle)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sleep_fn raised on cycle %d: %s", self.cycle, exc)
            dream_facts_count = -1  # marker for failed cycle

        await self.bus.publish(Event(
            topic="L0.circadian.asleep",
            source="L0.circadian",
            payload={
                "cycle": self.cycle,
                "dream_facts_count": dream_facts_count,
                "duration_s": time.time() - cycle_started_at,
            },
        ))

        # Also publish L1.sleep.consolidated so L2 Memory and L9 SelfModel
        # know that the sleep cycle has completed and can do their promotion.
        await self.bus.publish(Event(
            topic="L1.sleep.consolidated",
            source="CircadianLoop",
            payload={
                "phase": "sleep_cycle",
                "cycle": self.cycle,
                "dream_facts_count": dream_facts_count,
            },
        ))

        self._cycle_log.append({
            "cycle": self.cycle,
            "day": day,
            "ticks": ticks,
            "duration_s": time.time() - cycle_started_at,
            "dream_facts_count": dream_facts_count,
        })
