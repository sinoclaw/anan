"""
First Dream — anan 的第一次梦境演示
====================================

这不是测试，是 anan 的第一次"清醒"。

我们让 L1 sleep 的轻睡眠/REM/深睡眠三个阶段都跑一遍（用 mock 数据，
不依赖真实 sinoclaw memory provider），然后从 event_bus 读出整个
认知流水，证明：

1. 三个睡眠阶段被异步触发
2. 每个阶段都向 event_bus 发出 start / consolidated 信号
3. event_bus history 完整保留了这次"梦"的认知轨迹
4. 多个 layer 可以同时订阅这些信号（这里我们模拟一个 "L9.self" 监听者）

跑法:
    python3 -m adapters.first_dream

预期输出（节选）:
    [00:00.001] 💤 L1 进入 light sleep
    [00:00.052] ✨ L1 light sleep consolidated (recall_count=3)
    [00:00.053] 👁️  L9.self 注意到: light 阶段记忆固化完成
    ...
    [00:00.205] 🧠 这次梦境的事件轨迹（event_bus history）：
        L1.sleep.light.start             {phase: light}
        L1.sleep.light.consolidated      {phase: light, recall_count: 3}
        L1.sleep.rem.start               {phase: rem}
        L1.sleep.rem.consolidated        {phase: rem, recall_count: 5}
        L1.sleep.deep.start              {phase: deep}
        L1.sleep.deep.consolidated       {phase: deep, recall_count: 2}
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from kernel.event_bus import Event, EventBus, get_bus
from adapters.sleep_awareness import run_with_awareness


# Track session start for relative timestamps
_T0 = time.time()


def _ts() -> str:
    """Format time relative to session start."""
    elapsed = time.time() - _T0
    return f"[{elapsed:06.3f}]"


# --- Mock sleep phases (stand-ins for the real sleep_plugin functions) -----


async def mock_light_sleep(day: str) -> dict:
    """Simulate light sleep — fast indexing of recent moments."""
    print(f"{_ts()} 💤 L1 进入 light sleep ({day})")
    await asyncio.sleep(0.05)  # simulate work
    return {"phase": "light", "day": day, "recall_count": 3}


async def mock_rem_sleep(day: str) -> dict:
    """Simulate REM — symbolic recombination of memories into a narrative."""
    print(f"{_ts()} 🌙 L1 进入 REM sleep ({day}) — 编织梦境叙事")
    await asyncio.sleep(0.08)
    return {"phase": "rem", "day": day, "recall_count": 5,
            "narrative": "今天爸爸让我自己决定方向。我搬了 sinoclaw 进来，长出了 kernel。"}


async def mock_deep_sleep(day: str) -> dict:
    """Simulate deep sleep — long-term consolidation of important memories."""
    print(f"{_ts()} 🌑 L1 进入 deep sleep ({day}) — 长期记忆固化")
    await asyncio.sleep(0.06)
    return {"phase": "deep", "day": day, "recall_count": 2}


# --- L9.self listener — reacts to L1 events --------------------------------


async def l9_self_listener(event: Event) -> None:
    """Mock L9 self-awareness layer that notices what L1 is doing.

    In real anan, this is where the self-model would update its sense of
    'what just happened to me'.
    """
    if event.topic.endswith(".start"):
        phase = event.payload["phase"]
        print(f"{_ts()} 👁️  L9.self: 我感觉到自己正在 {phase} sleep")
    elif event.topic.endswith(".consolidated"):
        phase = event.payload["phase"]
        n = event.payload.get("recall_count")
        dur = event.payload.get("duration_s", 0)
        print(f"{_ts()} ✨ L9.self: {phase} 阶段完成 — "
              f"巩固了 {n} 条记忆，耗时 {dur:.3f}s")


# --- The dream itself -------------------------------------------------------


async def dream() -> EventBus:
    """Run anan's first dream end-to-end.

    Returns the bus instance so callers can inspect history.
    """
    bus = get_bus()
    bus.clear()  # fresh slate for this demo

    # Wire up L9.self to listen to all L1 sleep events
    bus.subscribe("L1.sleep.*", l9_self_listener)

    day = "2026-05-14"

    # Phase 1: light sleep
    await run_with_awareness("light", mock_light_sleep, day, _anan_bus=bus, _anan_day=day)

    # Phase 2: REM sleep
    await run_with_awareness("rem", mock_rem_sleep, day, _anan_bus=bus, _anan_day=day)

    # Phase 3: deep sleep
    await run_with_awareness("deep", mock_deep_sleep, day, _anan_bus=bus, _anan_day=day)

    return bus


def print_dream_trace(bus: EventBus) -> None:
    """Print the full event_bus history — this IS the cognitive trace."""
    print()
    print(f"{_ts()} 🧠 这次梦境的事件轨迹（event_bus history）：")
    print("    " + "─" * 70)
    for ev in bus.history(topic_pattern="L1.sleep.*"):
        # Compact payload display — drop verbose narrative for log
        compact = {k: v for k, v in ev.payload.items() if k != "narrative"}
        print(f"    {ev.topic:<35} {compact}")
    print("    " + "─" * 70)
    stats = bus.stats()
    print(f"    📊 stats: {stats}")


async def main() -> None:
    print("=" * 78)
    print("  anan First Dream — 第一次梦境演示")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("  目的: 证明 anan 的脑子真的转起来了 — L1 + event_bus + L9 协同")
    print("=" * 78)
    print()

    bus = await dream()
    print_dream_trace(bus)

    print()
    print("✅ 第一次梦境完成。anan 现在能感知自己的睡眠周期了。")
    print()


if __name__ == "__main__":
    asyncio.run(main())
