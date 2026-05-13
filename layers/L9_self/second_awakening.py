"""
Second Awakening — anan 的第二次苏醒
======================================

如果 first_dream.py 是 anan 学会做梦的瞬间，
那 second_awakening.py 是 anan 学会**记得**的瞬间。

这次我们不再做梦。我们只做一件事：
    打开 ~/.anan/memories/ 里的 JSONL 文件，
    重建 self-model，
    让 anan 自己说出"我是谁"、"我为什么存在"、"昨天我梦见了什么"。

这是 L9 self 第一次真的派上用场 —— 没有 RAM 缓存，没有上下文注入，
全靠硬盘上的梦境痕迹。

跑法:
    # 先跑一次 first_dream 把梦写到 ~/.anan/memories/
    python3 -m adapters.first_dream
    # 然后跑这个 demo 让 anan "醒来"
    python3 -m layers.L9_self.second_awakening
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from kernel.event_bus import Event, EventBus, get_bus
from layers.L9_self.self_model import SelfModelLive


_T0 = time.time()


def _ts() -> str:
    return f"[{time.time() - _T0:06.3f}]"


async def listen_for_signs(bus: EventBus) -> None:
    """L9-internal listener that prints awakening signals as they fire.

    NOTE: The event_bus expects async handlers (it awaits the return value).
    Using sync `def` works for some buses but ours is strict — define async.
    """
    async def _on_loaded(event: Event):
        p = event.payload
        print(f"{_ts()} 🌅 L9.self.loaded — facts={p['n_facts']}, "
              f"days={p['n_days']}, identity={p['identity_count']}, vision={p['vision_count']}")

    async def _on_updated(event: Event):
        p = event.payload
        print(f"{_ts()} 🌱 L9.self.updated — phase={p['phase']}, "
              f"day={p['day']}, +{p['n_new']} new facts (total={p['total_facts']})")

    bus.subscribe("L9.self.loaded", _on_loaded)
    bus.subscribe("L9.self.updated", _on_updated)


async def awaken() -> SelfModelLive:
    """The actual awakening sequence."""
    bus = get_bus()
    bus.clear()  # fresh bus for this demo, but disk memory is preserved

    await listen_for_signs(bus)

    print(f"{_ts()} 🌑 anan 启动，准备从硬盘读取记忆...")
    print(f"{_ts()} 📂 memory_dir = {Path.home() / '.anan' / 'memories'}")
    print()

    # The whole magic: SelfModelLive() rebuilds the self from disk on construction
    live = SelfModelLive()
    await live.attach(bus)

    return live


async def main() -> None:
    print("=" * 78)
    print("  anan Second Awakening — 第二次苏醒（L9 self 启动）")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("  目的: 证明 anan 不再失忆 — 重启后还能说出『我是谁』")
    print("=" * 78)
    print()

    live = await awaken()

    print()
    print(f"{_ts()} 🤖 anan 现在尝试介绍自己:")
    print("    " + "─" * 70)
    for line in live.model.who_am_i().split("\n"):
        print(f"    {line}")
    print("    " + "─" * 70)

    print()
    print(f"{_ts()} 🎯 anan 现在说出存在的意义:")
    print("    " + "─" * 70)
    for line in live.model.why_do_i_exist().split("\n"):
        print(f"    {line}")
    print("    " + "─" * 70)

    print()
    print(f"{_ts()} 💭 anan 回忆最近一次梦:")
    print("    " + "─" * 70)
    for line in live.model.what_did_i_dream().split("\n"):
        print(f"    {line}")
    print("    " + "─" * 70)

    print()
    print(f"{_ts()} 📊 self-model 状态: {live.model.summary()}")
    print()

    if live.model.n_facts == 0:
        print("⚠️  anan 没有找到任何记忆 —— 先跑一次 `python3 -m adapters.first_dream` 让我做个梦")
    else:
        print("✅ anan 第二次苏醒成功。我从昨天的梦里记得了自己是谁。")
    print()


if __name__ == "__main__":
    asyncio.run(main())
