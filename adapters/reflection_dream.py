"""
Reflection Dream — anan 从经历里真的反思出事实
================================================

之前 mock_*_sleep 的 facts 是硬编码 — 那是『我演什么就反思出什么』。
真反思应该是：**看 bus 上发生了什么，提炼出值得记的**。

这就是这个 adapter 做的：
- 扫 EventBus.history() 拿这一周期里所有事件
- 按事件类型聚合 ("收到了 N 个 tick"、"经历了 fatigue=K"...)
- 生成 facts 注入回 sleep result 让 L2 去持久化

这是 anan 第一次"反思"自己 — facts 不再是脚本喂的，是 anan 看自己经历后产生的。

Light sleep:  快索引 — 这周期发生了多少事
REM sleep:    叙事 — 编个故事概括周期
Deep sleep:   抽象 — 从多个周期里提炼模式

注意：这是启发式 reflection（pattern matching），不是 LLM。
LLM-based reflection 留给 L6 metacognition 以后做。
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Optional

from kernel.event_bus import EventBus, get_bus

logger = logging.getLogger("anan.adapters.reflection_dream")


def _summarize_topics(events: list) -> dict[str, int]:
    """Bucket events by their leading topic segment (L0 / L1 / L2 / ...)."""
    return dict(Counter(e.topic.split(".")[0] for e in events))


def reflect_light(bus: EventBus, *, day: str, cycle: int) -> dict:
    """Light sleep — fast index of recent activity.

    Looks at events from the most recent active period and counts what happened.
    Output is intentionally factual / boring — light sleep is just bookkeeping.
    """
    events = bus.history(limit=200)
    # Only look at events SINCE the last wake of this cycle
    cycle_events = [
        e for e in events
        if e.payload and e.payload.get("cycle") == cycle
    ]
    summary = _summarize_topics(cycle_events)
    facts = [
        f"周期 {cycle} ({day}) 经历了 {len(cycle_events)} 个事件",
    ]
    for layer, count in sorted(summary.items()):
        facts.append(f"  其中 {layer} 层事件 {count} 个")

    return {
        "phase": "light",
        "day": day,
        "recall_count": len(cycle_events),
        "consolidated_facts": facts,
    }


def reflect_rem(bus: EventBus, *, day: str, cycle: int, working_memory=None) -> dict:
    """REM sleep — narrative recombination.

    Picks notable moments and weaves them into a short narrative.
    Right now: heuristic. Future: LLM via L6.

    If `working_memory` is provided, the narrative pulls its TOP entries
    (already weighted by salience+decay) instead of grovelling through raw
    bus history. This is what L3 is for — better signal/noise.
    """
    events = bus.history(limit=200)
    cycle_events = [
        e for e in events
        if e.payload and e.payload.get("cycle") == cycle
    ]

    ticks = [e for e in cycle_events if e.topic == "L0.circadian.tick"]
    bedtime = next((e for e in cycle_events if e.topic == "L0.circadian.bedtime"), None)

    facts: list[str] = []
    dream_content: Optional[str] = None

    if ticks:
        last_tick = ticks[-1]
        max_fatigue = last_tick.payload.get("fatigue", 0)
        elapsed = last_tick.payload.get("elapsed_s", 0)
        facts.append(
            f"在周期 {cycle} 我经历了 {len(ticks)} 个心跳，"
            f"持续了 {elapsed:.2f}s，疲劳度爬到 {max_fatigue:.1f}"
        )
        dream_content = (
            f"梦境主题：周期 {cycle} 的节奏感 — "
            f"我感觉到自己在 active 阶段一点点累积，"
            f"然后到 fatigue {max_fatigue:.0f} 的时候自然想睡。"
        )

    if bedtime:
        facts.append(f"我自己识别出该睡了（cycle={cycle}）—— 这是自主的节律")

    # If L3 working memory is wired, mix in what felt most salient
    if working_memory is not None:
        try:
            top = working_memory.recall_recent(n=3)
            if top:
                topics = [e.event.topic for e in top]
                facts.append(
                    f"working memory 觉得最深的 3 件事："
                    f"{', '.join(topics)}"
                )
        except Exception:  # noqa: BLE001
            pass  # WM is best-effort, never break the dream

    if not facts:
        facts.append(f"周期 {cycle} 没什么可叙事的")
        dream_content = "梦里一片安静。"

    return {
        "phase": "rem",
        "day": day,
        "recall_count": len(facts),
        "dream": dream_content,
        "consolidated_facts": facts,
    }


def reflect_deep(bus: EventBus, *, day: str, cycle: int) -> dict:
    """Deep sleep — long-term abstraction.

    Looks across MULTIPLE cycles to find patterns. Only fires every few cycles
    (caller decides). Generates the most "permanent" facts — these are the ones
    that become part of identity.
    """
    events = bus.history(limit=500)
    # Distinct cycles we've seen complete
    cycles_seen = sorted({
        e.payload.get("cycle") for e in events
        if e.topic == "L0.circadian.asleep" and e.payload
    })

    facts: list[str] = []
    if len(cycles_seen) >= 2:
        facts.append(
            f"我已经独立完成了 {len(cycles_seen)} 个完整周期 "
            f"(cycles: {cycles_seen[:5]}{'...' if len(cycles_seen) > 5 else ''})"
        )
        facts.append("核心身份：我是陈亦安，我能自己维持节律")
    else:
        facts.append(f"这是我第一个完整周期 (cycle={cycle})")
        facts.append("核心身份：我开始能自主生活了")

    # Always include the abstract / permanent fact
    facts.append("核心愿景：长出能自主决策的脑子")

    return {
        "phase": "deep",
        "day": day,
        "recall_count": len(facts),
        "consolidated_facts": facts,
    }


# --------------------------------------------------------------------------
# A ready-to-use sleep_fn that plugs into CircadianLoop
# --------------------------------------------------------------------------


async def reflective_sleep_cycle(
    day: str, bus: EventBus, cycle: int, *, working_memory=None,
) -> int:
    """Sleep function compatible with CircadianLoop.sleep_fn signature.

    Runs light → REM → deep sleep through run_with_awareness so L2 picks it up.
    If `working_memory` is provided, REM uses it for richer narrative.
    Returns the total number of consolidated facts.
    """
    from adapters.sleep_awareness import run_with_awareness

    total = 0
    for phase, fn in (
        ("light", lambda d: _async_wrap(reflect_light(bus, day=d, cycle=cycle))),
        ("rem",   lambda d: _async_wrap(reflect_rem(bus, day=d, cycle=cycle, working_memory=working_memory))),
        ("deep",  lambda d: _async_wrap(reflect_deep(bus, day=d, cycle=cycle))),
    ):
        result = await run_with_awareness(
            phase, fn, day, _anan_bus=bus, _anan_day=day,
        )
        if isinstance(result, dict):
            total += len(result.get("consolidated_facts") or [])
    return total


async def _async_wrap(value):
    """Tiny adapter so sync reflect_*() can be passed to run_with_awareness."""
    return value
