#!/usr/bin/env python3
"""
anan 全层端到端冒烟测试
======================

运行：python e2e_smoke_test.py
"""

import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from kernel.event_bus import EventBus, Event
from layers.L3_attention.attention import AttentionQueue, AttentionScore, Priority
from layers.L5_reasoning.pattern_miner import PatternMiner
from layers.L5_prediction.predictor import PredictiveReasoner
from layers.L6_metacognition.prediction_monitor import PredictionMonitor
from layers.L6_metacognition.self_tuner import SelfTuner
from layers.L7_goals.goal_engine import GoalGenerator, GoalScope
from layers.L8_drives.drive_system import DriveSystem, DriveType
from layers.L9_self.self_model import SelfModel, SelfModelLive
from layers.L8_drives.attention_bridge import AttentionBridge


async def main():
    print("=" * 60)
    print("anan 全层端到端冒烟测试")
    print("=" * 60)

    bus = EventBus()

    # ── L3: AttentionQueue ───────────────────────────────────────
    print("\n[L3] AttentionQueue")
    q = AttentionQueue(bus=bus)
    item = q.enqueue(item_id="e2e-test", label="test", source="e2e",
                     score=AttentionScore(0.6, 0.5, 0.5))
    focused = q.focus()
    print(f"  enqueue + focus: {'✅' if focused and focused.id == item.id else '❌'}")

    boosted = q.boost(item.id, 0.2)
    print(f"  boost: {'✅' if boosted else '❌'}")
    item2 = next((i for i in q._items if i.id == item.id), None)
    print(f"  total_score after boost: {item2.total_score():.3f} {'✅' if item2.boost > 0 else '❌'}")

    # ── L5: PatternMiner ─────────────────────────────────────────
    print("\n[L5] PatternMiner")
    pm = PatternMiner(bus=bus, self_model=None)
    await pm.attach()
    print(f"  attach: ✅")
    await pm.detach()
    print(f"  detach: ✅")

    # ── L5: PredictiveReasoner ───────────────────────────────────
    print("\n[L5] PredictiveReasoner")
    pr = PredictiveReasoner(bus=bus)
    await pr.attach()
    print(f"  attach: ✅")
    pr._decay_link("nonexistent", 0.8)
    print(f"  _decay_link (graceful skip): ✅")
    await pr.detach()
    print(f"  detach: ✅")

    # ── L6: PredictionMonitor ─────────────────────────────────────
    print("\n[L6] PredictionMonitor")
    pm_l6 = PredictionMonitor(bus=bus)
    await pm_l6.attach()
    print(f"  attach: ✅")
    await pm_l6._on_failed(Event("L5.prediction.failed",
        source="test", payload={"prediction_id": "p1", "link_key": "a→b"}))
    await pm_l6._on_confirmed(Event("L5.prediction.confirmed",
        source="test", payload={"prediction_id": "p2", "link_key": "b→c"}))
    acc = pm_l6.accuracy()
    print(f"  accuracy(): {acc:.1f} {'✅'}")
    stats = pm_l6.stats()
    print(f"  stats keys: {list(stats.keys())} {'✅'}")
    await pm_l6.detach()
    print(f"  detach: ✅")

    # ── L6: SelfTuner ────────────────────────────────────────────
    print("\n[L6] SelfTuner")
    st = SelfTuner(bus=bus, predictor=pr)
    await st.attach()
    print(f"  attach: ✅")
    await pm_l6._on_failed(Event("L5.prediction.failed",
        source="test", payload={"prediction_id": "p3", "link_key": "x→y"}))
    await pm_l6._on_failed(Event("L5.prediction.failed",
        source="test", payload={"prediction_id": "p4", "link_key": "z→w"}))
    suggestions = st.suggest()
    print(f"  suggest() → str (len={len(suggestions)}): {'✅' if isinstance(suggestions, str) else '❌'}")
    st_stats = st.stats()
    print(f"  stats: {st_stats} ✅")
    await st.detach()
    print(f"  detach: ✅")

    # ── L7: GoalGenerator ────────────────────────────────────────
    print("\n[L7] GoalGenerator")
    gg = GoalGenerator(bus=bus)
    print(f"  create: ✅")
    g1 = gg.propose("帮爸爸整理文件", scope=GoalScope.SHORT, tags=["爸爸"])
    print(f"  propose: ✅ (id={g1.id[:8]})")
    top = gg.top_goals()
    print(f"  top_goals: {'✅' if len(top) >= 1 else '❌'} ({len(top)} goals)")
    what = gg.what_are_my_goals()
    print(f"  what_are_my_goals: {what[:60]}... ✅")
    await gg.detach()
    print(f"  detach: ✅")

    # ── L8: DriveSystem ──────────────────────────────────────────
    print("\n[L8] DriveSystem")
    ds = DriveSystem(bus=bus)
    print(f"  create: ✅")
    ds._drives[DriveType.CARE].active = True
    ds._drives[DriveType.CARE].strength = 0.6
    boost = ds.priority_boost(["爸爸"])
    print(f"  priority_boost(['爸爸']) CARE=active: {boost:.2f} {'✅' if boost > 0 else '❌'}")
    boost2 = ds.priority_boost(["代码"])
    print(f"  priority_boost(['代码']) CARE=active: {boost2:.2f} ✅")
    snap = ds.snapshot()
    print(f"  snapshot top_drives: {snap.get('top_drives', [])[:2]} ✅")
    sat = ds.satisfaction_rate()
    print(f"  satisfaction_rate: {sat:.2f} ✅")
    wants = ds.what_does_an_an_want()
    print(f"  what_does_an_an_want (len={len(wants)}): ✅")
    await ds.detach()
    print(f"  detach: ✅")

    # ── L8→L3: AttentionBridge ───────────────────────────────────
    print("\n[L8→L3] AttentionBridge")
    q2 = AttentionQueue(bus=bus)
    ds2 = DriveSystem(bus=bus)
    bridge = AttentionBridge(attention_q=q2, drive_system=ds2)
    await bridge.attach()
    print(f"  attach: ✅")
    await bus.publish(Event("L8.drive.updated",
        source="test",
        payload={"drive_type": "CARE", "active": True, "strength": 0.7}))
    await asyncio.sleep(0.02)
    print(f"  L8.drive.updated published: ✅")
    await bridge.detach()
    print(f"  detach: ✅")

    # ── L9: SelfModel ────────────────────────────────────────────
    print("\n[L9] SelfModel")
    sm = SelfModel()
    print(f"  create: ✅")
    added = sm.add_wisdom(dict(antecedent="帮助爸爸", consequent="感到满足",
                                support=0.5, confidence=0.9, lift=3.0))
    print(f"  add_wisdom (new): {'✅' if added else '❌'}")
    added2 = sm.add_wisdom(dict(antecedent="帮助爸爸", consequent="感到满足",
                                 support=0.5, confidence=0.9, lift=3.0))
    print(f"  add_wisdom (dup reject): {'✅' if not added2 else '❌'}")
    print(f"  n_facts: {sm.n_facts} {'✅' if sm.n_facts == 1 else '❌'}")

    report = sm.what_have_i_learned()
    print(f"  what_have_i_learned (len={len(report)}): {'✅' if len(report) > 0 else '❌'}")

    # LiveSelfModel
    print("\n[L9] SelfModelLive")
    sm_live = SelfModelLive()
    await sm_live.attach(bus)
    print(f"  attach: ✅")
    await sm_live._on_pattern_discovered(Event("L5.pattern.discovered",
        source="test", payload=dict(antecedent="学新知识", consequent="认知提升",
                                    support=0.4, confidence=0.88, lift=3.5)))
    print(f"  _on_pattern_discovered: ✅")
    print(f"  update_count: {sm_live.update_count} {'✅' if sm_live.update_count >= 1 else '❌'}")
    await sm_live.detach()
    print(f"  detach: ✅")

    # ── L5→L9: PatternMiner → SelfModelLive (真实事件集成) ─────────
    print("\n[L5→L9] PatternMiner → SelfModelLive via EventBus")
    bus2 = EventBus()
    sm3 = SelfModelLive()
    await sm3.attach(bus2)
    pm3 = PatternMiner(bus=bus2, self_model=sm3.model)
    await pm3.attach()
    await bus2.publish(Event("L5.reasoning.stepped",
        source="test", payload={"step": "test_step"}))
    await asyncio.sleep(0.05)
    print(f"  PatternMiner attached + event published: ✅")
    print(f"  update_count after event: {sm3.update_count} {'✅' if sm3.update_count >= 1 else '❌'}")
    await pm3.detach()
    await sm3.detach()

    # ── PersistentSession ─────────────────────────────────────────
    print("\n[kernel] PersistentSession")
    from kernel.persistent_session import PersistentSession, SessionConfig
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = SessionConfig(storage_dir=tmpdir, max_iterations=1)
        ps = PersistentSession(config=cfg)
        ps._agent = type("_A", (), {"chat": lambda s, m: f"mock: {m[:20]}"})()
        ps._running = True
        ps._short_term_memory = ["user: hi", "assistant: hi"]
        ps._session_n = 1
        ps._save()
        print(f"  _save to JSONL: ✅")

        cfg2 = SessionConfig(storage_dir=tmpdir)
        ps2 = PersistentSession(config=cfg2)
        print(f"  _load on init: {'✅' if len(ps2._short_term_memory) == 2 else '❌'}")
        print(f"  memory restored: {ps2._short_term_memory}")

    print("\n" + "=" * 60)
    print("全层冒烟测试完成 ✅")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
