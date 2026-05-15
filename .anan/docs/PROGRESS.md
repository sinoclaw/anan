# anan 九层进度报告

> 更新时间：2026-05-15 16:45
> 调研范围：`layers/` 全部源文件 + `kernel/mind_stack_runner.py`
> 最新提交：L5→L6→L7→L8 预测验证闭环 + Daydreaming/Lucid Dream 触发链全接通

---

## 总览

| 层 | 组件 | 状态 | 启动 | attach | 核心功能 |
|---|---|---|---|---|---|
| L0 | CircadianLoop | ✅ | ✅ | — | 30s/次 tick |
| L1 | DreamingPlugin | ✅ | ✅ | ✅ | Daydreaming（L4.idle.started 触发）+ Lucid Dream 框架完整 |
| L2 | MemoryTier | ✅ | ✅ | ✅ | 三层存储在，promote 链路已联动（publishes L2.memory.persisted） |
| L3 | VigilanceMonitor | 🟡 | ✅ | ✅ | 走神检测框架在，未与 L4 联动 |
| L3 | AttentionQueue | 🟡 | ✅ | ✅ | 三维评分在，未被其他层调用 |
| L4 | ConsciousnessEngine | ✅ | ✅ | ✅ | idle 检测 + 对话上下文注入 + L8 驱动消费 |
| L5 | PatternMiner | ✅ | ✅ | ✅ | 因果挖掘完成 |
| L5 | PredictiveReasoner | ✅ | ✅ | ✅ | 预测完成 |
| L6 | PredictionMonitor | ✅ | ✅ | ✅ | 追踪准确率完成 |
| L6 | SelfTuner | ✅ | ✅ | ✅ | 调参闭环接通 PatternMiner + PredictiveReasoner |
| L6 | Mirror | ✅ | ✅ | ✅ | 发 HealthReport 事件，L7 Goals 消费 |
| L7 | GoalGenerator | ✅ | ✅ | ✅ | L8驱动触发 + L0.tick周期生成目标 |
| L7 | SelfRegulator | 🟡 | ✅ | ✅ | 订阅 L6.warn + L7.goal.achieved/abandoned，自动调节 |
| L8 | DriveSystem | ✅ | ✅ | ✅ | L0.tick 周期性触发 CURIOSITY |
| L8 | IntentStack | 🟡 | ✅ | ✅ | 订阅 L6.report/L7.goal.*，未被主动触发 |
| L8 | AttentionBridge | ✅ | ✅ | ✅ | DriveSystem → AttentionQueue boost 桥接 |
| L9 | SelfModel | ✅ | ✅ | ✅ | 自我意识完成 |

---

## 各层详细状态

### L0 — Circadian Loop ✅
**文件**：`kernel/circadian.py`

- 30s/次 tick，发 `L0.circadian.tick/wake/bedtime/asleep`
- fatigue_threshold=5.0（可配置）
- **问题**：tick_interval_s=30s 是给 idle 场景的，生产环境要调

---

### L1 — Sleep 🟡 部分实现
**文件**：`layers/L1_sleep/sleep_plugin.py`（1698行）

**已有**：
- Light/REM/Deep Sleep 三阶段框架
- `AnanSessionDB` 读 anan state.db 的 session 数据
- Narrative dream 生成（NARRATIVE_SYSTEM_PROMPT）
- Recall signal system（短时记忆信号）
- `attach()` 订阅 L4.idle.started，`start()` 调用 `attach()` 使 Daydreaming 触发链生效

**缺失**：
- `DreamingPlugin` 的 `run_dreaming_sweep()` 需要 `workspace_dir` + `phase` 参数，MindStackRunner 传了空 `{}`
- Daydreaming（idle 触发）未实现
- Lucid Dream（自主调度未来行动）未实现
- `_extract_concept_tags` 的大写字母 split bug 未修

**与 L2 联动**：promote 链路存在但未连接 CircadianLoop 的 sleep 事件

---

### L2 — Memory Tier 🟡 部分实现
**文件**：`layers/L2_memory/memory_tier.py`

**已有**：
- MemoryStore（JSON 文件存储）
- 三层：short-term(recall-store.json) / mid-term(周月记) / long-term(MEMORY.md)
- promote 链路：`promote_short_to_mid()` / `promote_all_short_to_mid()` / `promote_mid_to_long()`
- `promote_all_short_to_mid()` / `promote_mid_to_long()` 发布 `L2.memory.persisted` 事件供 L9 消费

**缺失**：
- `promote_all_short_to_mid()` 被 L1 Deep Sleep 调用，但 L1 的 sleep_fn 传空参数导致调用失败
- Mid-term 周记/月记的摘要压缩未实现（直接拼接原文）
- Working Memory（L3_working_memory）未接入

---

### L3 — Attention 🟡 部分实现
**文件**：`layers/L3_attention/attention.py`

**已有**：
- VigilanceMonitor：走神检测（focus_duration < threshold 的频率）
- AttentionQueue：三维评分(urgency/importance/interest) + 抢占机制
- `boost()` 接口：外部加成分数，供 L8 DriveSystem 调用

**缺失**：
- AttentionQueue 未被 MindStackRunner 传给其他层（各层独立创建 instance）
- L4 ConsciousnessEngine 创建了自己的 `IdleDetector`，未使用 AttentionQueue 的监测结果
- L8 DriveSystem 有 `attention_bridge.py` 但未激活

---

### L4 — Consciousness ✅ 完成
**文件**：`layers/L4_consciousness/consciousness.py`

**已有**：
- IdleDetector：120s 无输入 → idle，开始发 `L4.idle.started/ended`
- ConsciousnessEngine：idle 时按 45s 周期生成思考
- OutputGate：评估推送还是内部笔记（只有 HIGH+CRITICAL 才推送）
- 5 种思考模板：DIALOGUE_REFLECTION / QUESTION_EXTENSION / TODO_CHECK / SITUATION_ASSOCIATION / SPONTANEOUS

**2026-05-15 修复**：
- ✅ `gateway.message.sent` 事件 → `_on_gateway_message()` 注入对话上下文
- ✅ `_on_gateway_message()` 调用 `note_user_input()` 取消 idle 状态
- ✅ 订阅 `L8.drive.suggestion` 接收驱动力建议
- ✅ `stop()` 方法加给 MindStackRunner 调用

**缺失**：
- `_generate_one_thought()` 的模板填充了提示词，但内容本身是"回想有没有更好回答"的开式问句，不是真正的反思性思考

---

### L5 — Predictive Mind ✅ 完成
**文件**：`layers/L5_reasoning/pattern_miner.py` + `layers/L5_prediction/predictor.py`

**已有**：
- PatternMiner：订阅 `L0.circadian.bedtime`，扫描 session 历史挖掘因果规则
- CausalReasoner：discovered_links 字典存储「X→Y」规则，含 lift/confidence/support
- PredictiveReasoner：订阅所有事件，pending prediction 匹配 effect → confirmed/failed
- `what_have_i_learned()`：输出中文洞察报告
- `wisdom_facts`：去重存储

**2026-05-15 修复**：
- ✅ `PatternMiner.set_min_lift()` 方法 + `import asyncio` 补全
- ✅ `SelfTuner._apply()` 同时写回 PatternMiner + PredictiveReasoner

**问题**：依赖 session 历史数据，刚启动时为空

---

### L6 — Metacognition 🟡 部分实现
**文件**：`layers/L6_metacognition/`

**已有**：
- PredictionMonitor：订阅 `L5.prediction.confirmed/failed`，衰减链路 lift，发出 `L6.metacognition.warn`
- SelfTuner：订阅 `L6.metacognition.warn` + `L6.metacognition.report`，调整 min_lift / horizon_s，写回 PatternMiner + PredictiveReasoner
- Mirror（`mirror.py`）：订阅 `L0.circadian.asleep`，发出 `L6.metacognition.report` + `L6.metacognition.warn`，L7 Goals 消费 report

**2026-05-15 修复**：
- ✅ SelfTuner 调参闭环接通：`_apply()` 同时写回 `PatternMiner.set_min_lift()` + `PredictiveReasoner._min_lift`
- ✅ SelfTuner 接收 `pattern_miner` 参数（由 MindStackRunner 注入）

**缺失**：
- Mirror 未加入 MindStackRunner（没注册到 `_layers`）
- Mirror 的 HealthReport 没有消费者（L7 Goals 未订阅）

---

### L7 — Goals ✅ 已触发
**文件**：`layers/L7_goals/goal_engine.py`

**已有**：
- GoalGenerator：propose / decompose / achieve / abandon / detect_conflicts
- `what_are_my_goals()` 输出当前目标列表

**2026-05-15 修复**：
- ✅ 订阅 `L8.drive.suggestion` → `_on_drive_suggestion()` 生成驱动力目标
- ✅ 订阅 `L0.circadian.tick` → `_on_circadian_tick()` 周期性生成探索目标（active_order < 2 时）

**缺失**：
- SelfRegulator 未与 GoalGenerator 连接

---

### L7 Will — SelfRegulator 🟡 框架在
**文件**：`layers/L7_will/regulator.py`

- 基于 GoalGenerator 的目标做自我调节
- 未与 GoalGenerator 连接

---

### L8 — Drive System ✅ 已触发
**文件**：`layers/L8_drives/drive_system.py`

**已有**：
- 5 种驱动力：CURIOSITY / COMPLETION / CARE / AESTHETICS / BOREDOM
- `trigger()` 触发驱动，`satisfy()` 满足驱动
- `active_drives()` 返回当前最活跃的驱动力

**2026-05-15 修复**：
- ✅ 订阅 `L0.circadian.tick` → `_on_circadian_tick()` 周期性触发 CURIOSITY
- ✅ 无活跃驱动时发送 `L8.drive.suggestion` 通知 L4/L7

**缺失**：
- `attention_bridge.py` 未接入（连接 DriveSystem 和 AttentionQueue 的桥）

---

### L8 Intent — IntentStack 🟡 未启动
**文件**：`layers/L8_intent/intent_stack.py`

- 订阅 `L8.intent.snapshot` 和 `L7.goal.*`
- 未加入 MindStackRunner 的 `_layers`（需要确认）

---

### L9 — Self Model ✅ 完成
**文件**：`layers/L9_self/self_model.py`

- 启动时扫描 `~/.anan/memories/*.jsonl` 重建身份事实
- `who_am_i()` / `what_did_i_dream()` / `why_do_i_exist()` 接口
- 订阅 `L2.memory.persisted` 增量更新（但 L2 未发此事件）

---

## 层间关键断点（2026-05-15 更新）

```
CircadianLoop.tick (L0.circadian.tick)
    ├─→ PatternMiner.mine_now()     ✅ (bedtime 触发)
    ├─→ DriveSystem._on_circadian_tick() ✅ 触发 CURIOSITY
    │       └─→ L8.drive.suggestion ✅
    │               ├─→ GoalGenerator._on_drive_suggestion() ✅ 生成目标
    │               └─→ ConsciousnessEngine._on_drive_suggestion() ✅ 生成思考
    └─→ GoalGenerator._on_circadian_tick() ✅ 周期性生成探索目标

PatternMiner.discovered_links
    └─→ PredictiveReasoner ✅

PredictiveReasoner
    ├─→ L5.prediction.upcoming ✅
    │       └─→ DriveSystem._on_prediction() ✅ 触发 CURIOSITY
    ├─→ L5.prediction.confirmed/failed ✅
    │       └─→ PredictionMonitor ✅
    │               └─→ L6.metacognition.warn ✅
    │                       └─→ SelfTuner ✅
    │                               ├─→ PredictiveReasoner._min_lift ✅
    │                               └─→ PatternMiner.set_min_lift() ✅

gateway.message.sent
    └─→ ConsciousnessEngine._on_gateway_message() ✅
            ├─→ set_dialogue_context() ✅
            └─→ note_user_input() ✅ 取消 idle

L2.memory.persisted (2026-05-15 新增)
    └─→ L9 SelfModel 增量更新 ✅

L6.metacognition.report (2026-05-15 新增)
    └─→ GoalGenerator._on_metacognition_report() ✅ 生成目标
```

---

## 未加入 MindStackRunner 的组件

（2026-05-15 更新：以下问题已全部修复）

| 组件 | 文件 | 原问题 | 状态 |
|---|---|---|---|
| Mirror | `layers/L6_metacognition/mirror.py` | `_start_layers()` 未实例化 | ✅ 已加入 |
| IntentStack | `layers/L8_intent/intent_stack.py` | stop() 方法存在 | ✅ 已确认 |
| AttentionBridge | `layers/L8_drives/attention_bridge.py` | 未激活 | ✅ 已接入（传入 AttentionQueue 实例） |

---

## 待办清单（按优先级）

### P0 — 必须修 ✅ 全部完成
1. ✅ **L6 SelfTuner ↔ PatternMiner 调参闭环**：SelfTuner 改了参数要写回 PatternMiner
2. ✅ **L4 ConsciousnessEngine 上下文注入**：让 `set_dialogue_context()` 被调用
3. ✅ **L4 note_user_input()**：外部事件触发 idle 检测
4. ✅ **DriveSystem 从未被触发**：L0.tick 周期性触发
5. ✅ **GoalGenerator 从未被触发**：L8.drive.suggestion + L0.tick 触发

### P1 — 重要 ✅ 全部完成
6. ✅ **L1 DreamingPlugin sleep_fn 参数**：传真实 workspace_dir + phase
7. ✅ **L2 MemoryTier promote 链路**：MemoryTier(bus=self._bus) 已传入 bus，promote 后发 L2.memory.persisted 事件
8. ✅ **Mirror 加入 MindStackRunner**：启动 L6 元认知报告 → L7 Goals
9. ✅ **IntentStack 确认启动**：stop() 方法已加

### P2 — 完善 ✅ 全部完成
10. ✅ **L7 Goals → SelfRegulator 连接**：SelfRegulator 已订阅 L7.goal.achieved/abandoned，GoalGenerator 发事件后自动消费
11. ✅ **AttentionQueue boost() 被 L8 调用**：DriveSystem 发 L8.drive.updated + AttentionBridge 已接入，MindStackRunner 传入同一个 AttentionQueue 实例
12. ✅ **L4 思考质量提升**：`_THOUGHT_TEMPLATES` 从问句改为反思性思考模板

### P3 — 新增 ✅ 全部完成
13. ✅ **L1 Daydreaming**：新增 `run_daydreaming_sweep()` 方法，idle 触发
14. ✅ **L1 Lucid Dream**：新增 `run_lucid_dream_sweep()` 方法，周末触发
15. ✅ **L7 Goals 消费 Mirror HealthReport**：GoalGenerator 订阅 `L6.metacognition.report`

---

## 下一步

P0 全部完成，九层事件流已全接通。下一批重点：
1. **Mirror 加入 MindStackRunner** — 让 L6 元认知报告产生消费者（L7 Goals）
2. **L1/L2 promote 链路** — 让睡眠真正触发记忆升级
3. **IntentStack 确认** — 验证是否正常启动
