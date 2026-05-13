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
from layers.L4_proactive import ProactiveObserver
from layers.L6_metacognition import Mirror
from layers.L7_will import SelfRegulator
from layers.L8_intent import IntentStack
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

    def on_l7_acted(e: Event):
        p = e.payload
        d = p["detail"]
        if p["action"] == "attenuate_layer_salience":
            print(f"{_ts()} 🔧 L7 调节 — 把 {d['layer']} 层 salience 降到 ×{d['factor']}")
        elif p["action"] == "shorten_sleep_threshold":
            print(f"{_ts()} 🔧 L7 调节 — sleep_threshold {d['from']} → {d['to']}")
        elif p["action"] == "emit_heal_intent":
            print(f"{_ts()} 🔧 L7 调节 — 发出 heal_bus 意图")
        else:
            print(f"{_ts()} 🔧 L7 调节 — {p['action']}: {d}")
        print()

    def on_l8_proposed(e: Event):
        p = e.payload
        print(f"{_ts()} 💭 L8 新意图 — {p['description']} (强度={p['strength']:.2f})")

    def on_l8_reinforced(e: Event):
        p = e.payload
        print(f"{_ts()} 💪 L8 加固 — {p['description']} → 强度={p['strength']:.2f} (×{p['reinforce_count']})")

    def on_l8_abandoned(e: Event):
        p = e.payload
        print(f"{_ts()} 🍃 L8 放下 — {p['description']} (原因: {p.get('abandon_reason')})")

    def on_l4_verified(e: Event):
        p = e.payload
        print(f"{_ts()} 👁️  L4 求证 ✅ — {p['intent_description']}")
        print(f"        → {p['evidence']}")

    def on_l4_falsified(e: Event):
        p = e.payload
        print(f"{_ts()} 👁️  L4 求证 ❌ — {p['intent_description']}")
        print(f"        → {p['evidence']}")

    bus.subscribe("L0.circadian.wake", on_wake)
    bus.subscribe("L0.circadian.bedtime", on_bedtime)
    bus.subscribe("L1.sleep.*.consolidated", on_l1_consolidated)
    bus.subscribe("L2.memory.persisted", on_l2_persisted)
    bus.subscribe("L9.self.updated", on_l9_updated)
    bus.subscribe("L0.circadian.asleep", on_asleep)
    bus.subscribe("L6.metacognition.report", on_l6_report)
    bus.subscribe("L7.regulator.acted", on_l7_acted)
    bus.subscribe("L8.intent.proposed", on_l8_proposed)
    bus.subscribe("L8.intent.reinforced", on_l8_reinforced)
    bus.subscribe("L8.intent.abandoned", on_l8_abandoned)
    bus.subscribe("L4.observation.verified", on_l4_verified)
    bus.subscribe("L4.observation.falsified", on_l4_falsified)


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
    # Stricter thresholds so anan actually sees its own problems and L7 can act.
    mirror = Mirror(
        bus=bus, working_memory=wm, self_model=l9.model,
        attention_skew_threshold=0.45,    # any layer >45% of WM → flag
        healthy_score_threshold=0.85,     # high bar so warns actually fire
    )
    await mirror.attach()

    # Configure the heartbeat — fast cycles for the demo
    config = CircadianConfig(
        tick_interval_s=0.03,
        fatigue_per_tick=1.0,
        sleep_threshold=4.0,    # ~4 ticks per active phase
        max_cycles=5,           # bumped to 5 so L7 has time to act
    )

    # sleep_fn closure that hands working memory + intent stack into reflection
    async def sleep_with_wm(day, bus, cycle):
        return await reflective_sleep_cycle(
            day, bus, cycle, working_memory=wm, intent_stack=l8,
        )

    loop = CircadianLoop(sleep_fn=sleep_with_wm, config=config, bus=bus)

    # Wire L7 (self-regulator — listens to L6.warn, adjusts WM + circadian)
    l7 = SelfRegulator(
        bus=bus, working_memory=wm, circadian=loop,
        salience_attenuation=0.5, threshold_step=0.5,
    )
    await l7.attach()

    # Wire L8 (intent stack — turns reactions into persistent wants)
    l8 = IntentStack(bus=bus, capacity=7, initial_strength=0.4, reinforce_alpha=0.35)
    await l8.attach()

    # Wire L4 (proactive observer — runs probes when L8 snapshots, satisfies/reinforces intents)
    l4 = ProactiveObserver(
        bus=bus, intent_stack=l8, working_memory=wm, self_model=l9.model,
        auto_satisfy=True, reinforce_on_falsify=True,
    )
    await l4.attach()

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
    print(f"{_ts()} 🔧 L7 self-regulator: {l7.stats()}")
    print(f"{_ts()} 💭 L8 intent stack: {l8.stats()}")
    print(f"{_ts()} 👁️  L4 observer:    {l4.stats()}")
    print()

    print(f"{_ts()} 🎯 anan 现在说『我想要什么』:")
    print("    " + "─" * 70)
    for line in l8.what_do_i_want().split("\n"):
        print(f"    {line}")
    print("    " + "─" * 70)
    print()

    if l7.history():
        print(f"{_ts()} 📜 L7 调节历史 — anan 这 5 个周期里改了自己几次:")
        print("    " + "─" * 70)
        for i, a in enumerate(l7.history(), 1):
            print(f"    {i}. [{a.action}] {a.trigger}")
            print(f"       → {a.detail}")
        print("    " + "─" * 70)
        print()

    print(f"{_ts()} 💭 anan 现在 recall_recent(5) — 我脑子里最显著的 5 件事:")
    print("    " + "─" * 70)
    for i, entry in enumerate(wm.recall_recent(5), 1):
        print(f"    {i}. [{entry.salience:.2f}] {entry.event.topic}")
    print("    " + "─" * 70)
    print()
    print("✅ anan 活完了 5 个周期。L0→L1→L2→L3→L6→L7→L8→L9 闭环走通了。")
    print("   反射 → 持续渴望 → 梦里加固 → 长成身份。anan 现在会想事了.")

    await l4.detach()
    await l8.detach()
    await l7.detach()
    await mirror.detach()
    await wm.detach()
    await l2.detach()
    await l9.detach()


if __name__ == "__main__":
    asyncio.run(main())
