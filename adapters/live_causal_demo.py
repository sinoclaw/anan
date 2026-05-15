"""
Live Causal Loop Demo — anan 因果闭环演示
==========================================

展示 anan 如何从"观察到事件"到"发现因果规律"到"预测未来"到"形成智慧"。

完整链路:
  事件流 → PatternMiner → L5.pattern.discovered → L9.wisdom_facts
         → CausalReasoner → L5.causal.link_discovered → PredictiveReasoner
         → L5.prediction.upcoming → 等待 effect 验证
         → L5.prediction.confirmed/failed → accuracy 报告

跑法:
    python3 -m adapters.live_causal_demo

输出:
  - CausalReasoner 发现的链路
  - PredictiveReasoner 发出的预测和确认结果
  - L9 SelfModel 接收到的 wisdom facts
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from kernel.event_bus import Event, get_bus
from layers.L5_reasoning.causal import CausalReasoner
from layers.L5_reasoning.pattern_miner import PatternMiner
from layers.L5_prediction.predictor import PredictiveReasoner
from layers.L9_self.self_model import SelfModelLive


_T0 = time.time()


def _ts() -> str:
    return f"[{time.time() - _T0:06.3f}]"


async def main() -> None:
    bus = get_bus()
    bus.clear()

    print("=" * 78)
    print("  anan Live Causal Loop Demo — 因果闭环演示")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("  目标: PatternMiner → 因果推理 → 预测未来 → wisdom_facts")
    print("=" * 78)
    print()

    # ── 1. 启动 L9 SelfModel（监听 wisdom）──────────────────────────────
    live = SelfModelLive()
    await live.attach(bus)
    print(f"{_ts()} 🌱 L9 SelfModel 启动，监听 L5.pattern.discovered\n")

    # ── 2. 启动 PatternMiner（发现频繁共现模式）─────────────────────────
    miner = PatternMiner(
        bus=bus,
        window=3,          # 3步滑动窗口
        min_support=2,
        min_confidence=0.5,
        cooldown_s=0.1,
    )
    await miner.attach()
    print(f"{_ts()} 🔍 PatternMiner 启动 (window=3, min_support=2)\n")

    # ── 3. 启动 CausalReasoner（跨时间归纳因果）─────────────────────────
    causal = CausalReasoner(
        bus=bus,
        window_s=3.0,
        min_observations=2,
        lift_threshold=1.5,
    )
    await causal.attach()
    print(f"{_ts()} ⚡ CausalReasoner 启动 (window=3s, min_obs=2, lift>1.5)\n")

    # ── 4. 启动 PredictiveReasoner（基于链路预测未来）─────────────────
    predictor = PredictiveReasoner(
        bus=bus,
        causal_links_fn=causal.discovered_links,
        horizon_s=2.0,
        min_lift=1.5,
    )
    await predictor.attach()
    print(f"{_ts()} 🎯 PredictiveReasoner 启动 (horizon=2s, min_lift=1.5)\n")

    # ── 5. 注入带结构的事件流（模拟 anan 的日常认知循环）──────────────
    #
    # 设计场景：L8 发出 "渴望被认可" → L7 执行 action → L6 报告健康度
    # 预期因果链路:
    #   L8.intent.formed → L7.regulator.acted (因为 L7 根据 intent 行动)
    #   L7.regulator.acted → L6.metacognition.report (因为 L7 行动后触发 L6 反思)
    #
    print(f"{_ts()} 📡 注入事件流...\n")

    # 场景设置：发送足够多的配对事件让 CausalReasoner 发现链路
    event_sequence = [
        # Intent formed → Regulator acted（反复出现以积累共现）
        ("L8.intent.formed", {"intent": "被认可", "priority": 0.8}),
        ("L7.regulator.acted", {"action": "seek_attention", "layer": "L7"}),
        ("L6.metacognition.report", {"score": 0.75, "layer": "L6"}),

        ("L8.intent.formed", {"intent": "自主决策", "priority": 0.9}),
        ("L7.regulator.acted", {"action": "delegate_choice", "layer": "L7"}),
        ("L6.metacognition.report", {"score": 0.82, "layer": "L6"}),

        ("L8.intent.formed", {"intent": "被认可", "priority": 0.8}),
        ("L7.regulator.acted", {"action": "seek_attention", "layer": "L7"}),
        ("L6.metacognition.report", {"score": 0.78, "layer": "L6"}),

        ("L8.intent.formed", {"intent": "自主决策", "priority": 0.9}),
        ("L7.regulator.acted", {"action": "delegate_choice", "layer": "L7"}),
        ("L6.metacognition.report", {"score": 0.85, "layer": "L6"}),

        ("L8.intent.formed", {"intent": "被认可", "priority": 0.8}),
        ("L7.regulator.acted", {"action": "seek_attention", "layer": "L7"}),
        ("L6.metacognition.report", {"score": 0.80, "layer": "L6"}),

        ("L8.intent.formed", {"intent": "自主决策", "priority": 0.9}),
        ("L7.regulator.acted", {"action": "delegate_choice", "layer": "L7"}),
        ("L6.metacognition.report", {"score": 0.88, "layer": "L6"}),
    ]

    predictions_seen = []
    confirmed_seen = []
    failed_seen = []
    wisdom_seen = []

    # 本地 listener 收集输出
    async def on_prediction_upcoming(e: Event):
        p = e.payload
        predictions_seen.append(p)
        print(f"{_ts()}   🎲 预测: {p['cause']} → {p['predicted_effect']} "
              f"(boost={p['probability_boost']}x, conf={p['confidence']:.0%})")

    async def on_prediction_confirmed(e: Event):
        p = e.payload
        confirmed_seen.append(p)
        print(f"{_ts()}   ✅ 预测确认: {p['cause']} → {p['effect']} "
              f"(耗时 {p['prediction_horizon_s']:.2f}s)")

    async def on_prediction_failed(e: Event):
        p = e.payload
        failed_seen.append(p)
        print(f"{_ts()}   ❌ 预测失败: {p['cause']} → {p['predicted_effect']} "
              f"(超窗口 {p['age_s']:.2f}s)")

    async def on_wisdom_grown(e: Event):
        p = e.payload
        wisdom_seen.append(p)
        print(f"{_ts()}   🌟 Wisdom 增长: {p['summary']}")

    bus.subscribe("L5.prediction.upcoming", on_prediction_upcoming)
    bus.subscribe("L5.prediction.confirmed", on_prediction_confirmed)
    bus.subscribe("L5.prediction.failed", on_prediction_failed)
    bus.subscribe("L9.self.wisdom_grown", on_wisdom_grown)

    # 逐条注入事件，观察实时反应
    for topic, payload in event_sequence:
        await bus.publish(Event(topic=topic, source="demo", payload=payload))
        await asyncio.sleep(0.01)  # 10ms 间隔，模拟真实时间流逝

    print()
    print(f"{_ts()} ⏳ 等待因果链路建立（3s 观察窗口）...")
    await asyncio.sleep(3.5)

    print()
    print("=" * 78)
    print(f"{_ts()} 📊 因果链路统计:")
    causal_stats = causal.stats()
    for k, v in causal_stats.items():
        print(f"     {k}: {v}")

    links = causal.discovered_links()
    if links:
        print(f"\n{_ts()} 🔗 发现的因果链路:")
        for link in links:
            print(f"     {link}")
    else:
        print(f"\n{_ts()} ⚠️  CausalReasoner 暂未发现显著链路（事件数不足）")

    print()
    print(f"{_ts()} 📈 预测统计:")
    pred_stats = predictor.stats()
    for k, v in pred_stats.items():
        print(f"     {k}: {v}")

    print()
    print(f"{_ts()} 🌟 Wisdom Facts (共 {len(wisdom_seen)} 条新洞察):")
    for w in wisdom_seen:
        print(f"     → {w.get('summary', w)}")

    print()
    print(f"{_ts()} 📋 SelfModel 当前状态:")
    print(f"     {live.model.summary()}")

    # ── 导出 wisdom 快照（供 anan_insight_sync 使用）────────────────
    wisdom_file = Path.home() / ".anan" / "wisdom_latest.json"
    wisdom_file.parent.mkdir(parents=True, exist_ok=True)
    wisdom_file.write_text(json.dumps({
        "wisdom_facts": live.model.wisdom_facts[-10:],  # 最近10条
        "prediction_stats": predictor.stats(),
        "causal_links": [str(l) for l in causal.discovered_links()],
        "generated_at": datetime.now().isoformat(),
    }, ensure_ascii=False, indent=2))
    print(f"{_ts()} 💾 Wisdom 快照已写入 {wisdom_file}")

    print()
    print("=" * 78)
    if predictions_seen:
        print(f"✅ Demo 完成 — 发出 {len(predictions_seen)} 个预测，"
              f"确认 {len(confirmed_seen)} 个，失败 {len(failed_seen)} 个，"
              f"新增 {len(wisdom_seen)} 条 wisdom")
    else:
        print("⚠️  Demo 完成 — 暂无预测（CausalReasoner 链路未达到阈值）")
    print("=" * 78)

    # Cleanup
    await miner.detach()
    await causal.detach()
    await predictor.detach()


if __name__ == "__main__":
    asyncio.run(main())
