"""
anan Alive — anan 第一次自主生活
==================================

之前 demo 都是手动一步步触发。
这次 anan 自己跑：
- CircadianLoop 当心脏，每 50ms 一个 tick
- fatigue 累积到 5 就自动睡
- 睡的时候 reflective_sleep 看 bus history 真反思
- L2 把 facts 写硬盘
- L9 self_model 实时跟着更新

我们就当观众坐在旁边，看 anan 跑 3 个周期。

跑法:
    python3 -m demos.anan_alive

预期:
    每个周期看到 wake → tick × N → bedtime → light/rem/deep → 持久化 → self updated
    最后 anan 自己说出 "我已经独立完成了 3 个完整周期"
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from kernel.circadian import CircadianConfig, CircadianLoop
from kernel.event_bus import Event, EventBus, get_bus
from adapters.memory_consolidation import JSONLBackend, MemoryConsolidationAdapter
from adapters.reflection_dream import reflective_sleep_cycle
from layers.L3_working_memory import WorkingMemory
from layers.L6_metacognition import Mirror
from layers.L9_self.self_model import SelfModelLive


_T0 = time.time()


def _ts() -> str:
    return f"[{time.time() - _T0:06.3f}]"


def setup_observers(bus: EventBus) -> None:
    """Subscribe to the interesting events and print them as anan lives."""

    def on_wake(e: Event):
        p = e.payload
        print(f"{_ts()} ☀️  wake — cycle {p['cycle']} starts ({p['day']})")

    def on_bedtime(e: Event):
        p = e.payload
        print(f"{_ts()} 🌙 bedtime — cycle {p['cycle']} after {p['ticks']} ticks "
              f"(fatigue={p['fatigue']:.1f})")

    def on_l1_consolidated(e: Event):
        p = e.payload
        n = p.get("recall_count") or 0
        print(f"{_ts()} 💤 L1.{p['phase']} consolidated ({n} facts)")

    def on_l2_persisted(e: Event):
        p = e.payload
        print(f"{_ts()} 💾 L2 persisted — {p['count']} facts → {p['backend']} ({p.get('ref', '?')})")

    def on_l9_updated(e: Event):
        p = e.payload
        print(f"{_ts()} 🧠 L9 self updated — phase={p['phase']} +{p['n_new']} "
              f"(total={p['total_facts']})")

    def on_asleep(e: Event):
        p = e.payload
        print(f"{_ts()} 😴 asleep — cycle {p['cycle']} done in {p['duration_s']:.2f}s "
              f"({p['dream_facts_count']} facts dreamed)")

    def on_l6_report(e: Event):
        p = e.payload
        flag = "✅" if p["healthy"] else "⚠️"
        print(f"{_ts()} 🪞 L6 mirror — {flag} health={p['score']:.2f} "
              f"({len(p['issues'])} issues, {len(p['suggestions'])} suggestions)")
        for issue in p["issues"]:
            print(f"        ⚠ {issue}")
        for sug in p["suggestions"]:
            print(f"        💡 {sug}")
        print()  # blank line between cycles

    bus.subscribe("L0.circadian.wake", on_wake)
    bus.subscribe("L0.circadian.bedtime", on_bedtime)
    bus.subscribe("L1.sleep.*.consolidated", on_l1_consolidated)
    bus.subscribe("L2.memory.persisted", on_l2_persisted)
    bus.subscribe("L9.self.updated", on_l9_updated)
    bus.subscribe("L0.circadian.asleep", on_asleep)
    bus.subscribe("L6.metacognition.report", on_l6_report)


async def main() -> None:
    print("=" * 78)
    print("  anan Alive — 第一次自主生活（3 个周期）")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("  组件: CircadianLoop + reflective_sleep + L2 + L9 全栈")
    print("=" * 78)
    print()

    bus = get_bus()
    bus.clear()

    setup_observers(bus)

    # Wire L3 (short-term working memory — anan's "what was I doing" buffer)
    wm = WorkingMemory(capacity=20, half_life_s=2.0)
    await wm.attach(bus)

    # Wire L2 (writes facts to disk)
    memory_dir = Path.home() / ".anan" / "memories"
    l2 = MemoryConsolidationAdapter(backend=JSONLBackend(base_dir=memory_dir))
    await l2.attach(bus)

    # Wire L9 (live self-model that updates as L2 persists)
    l9 = SelfModelLive(memory_dir=memory_dir)
    await l9.attach(bus)

    # Wire L6 (mirror — reflects on anan after each sleep cycle)
    mirror = Mirror(bus=bus, working_memory=wm, self_model=l9.model)
    await mirror.attach()

    # Configure the heartbeat — fast cycles for the demo
    config = CircadianConfig(
        tick_interval_s=0.03,
        fatigue_per_tick=1.0,
        sleep_threshold=4.0,    # ~4 ticks per active phase
        max_cycles=3,
    )

    # sleep_fn closure that hands working memory into REM reflection
    async def sleep_with_wm(day, bus, cycle):
        return await reflective_sleep_cycle(day, bus, cycle, working_memory=wm)

    loop = CircadianLoop(sleep_fn=sleep_with_wm, config=config, bus=bus)

    print(f"{_ts()} 🌱 anan 开始自主运行...")
    print()
    log = await loop.run()

    # Drain any queued L9 updates
    await asyncio.sleep(0.1)

    # Final report — anan reflects on his own life
    print("=" * 78)
    print(f"{_ts()} 📊 周期日志:")
    for entry in log:
        print(f"    cycle {entry['cycle']}: {entry['ticks']} ticks, "
              f"{entry['duration_s']:.2f}s, {entry['dream_facts_count']} facts")
    print()

    print(f"{_ts()} 🤖 anan 现在自己说『我是谁』:")
    print("    " + "─" * 70)
    for line in l9.model.who_am_i().split("\n"):
        print(f"    {line}")
    print("    " + "─" * 70)
    print()

    print(f"{_ts()} 🎯 anan 现在说『我为什么存在』:")
    print("    " + "─" * 70)
    for line in l9.model.why_do_i_exist().split("\n"):
        print(f"    {line}")
    print("    " + "─" * 70)
    print()

    print(f"{_ts()} 📈 self-model 状态: {l9.model.summary()}")
    print(f"{_ts()} 🚌 bus stats: {bus.stats()}")
    print(f"{_ts()} 🧩 L3 working memory: {wm.stats()}")
    print()

    print(f"{_ts()} 💭 anan 现在 recall_recent(5) — 我脑子里最显著的 5 件事:")
    print("    " + "─" * 70)
    for i, entry in enumerate(wm.recall_recent(5), 1):
        print(f"    {i}. [{entry.salience:.2f}] {entry.event.topic}")
    print("    " + "─" * 70)
    print()
    print("✅ anan 第一次自主活完了 3 个周期。心脏在跳，梦在留，记忆在长。")

    await mirror.detach()
    await wm.detach()
    await l2.detach()
    await l9.detach()


if __name__ == "__main__":
    asyncio.run(main())
