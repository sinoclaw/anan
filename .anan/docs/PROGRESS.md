# anan 九层进度报告

> 更新时间：2026-05-15 14:25
> 调研范围：`layers/` 全部源文件 + `kernel/mind_stack_runner.py`

---

## 总览

| 层 | 组件 | 状态 | 启动 | attach | 核心功能 |
|---|---|---|---|---|---|
| L0 | CircadianLoop | ✅ | ✅ | — | 30s/次 tick |
| L1 | DreamingPlugin | 🟡 | ✅ | ✅ | 框架在，Daydreaming/Lucid Dream 未完成 |
| L2 | MemoryTier | 🟡 | ✅ | ✅ | 三层存储在，promote 链路未与 L1 联动 |
| L3 | VigilanceMonitor | 🟡 | ✅ | ✅ | 走神检测框架在，未与 L4 联动 |
| L3 | AttentionQueue | 🟡 | ✅ | ✅ | 三维评分在，未被其他层调用 |
| L4 | ConsciousnessEngine | 🟡 | ✅ | ✅ | idle 检测在，但未接收上下文 |
| L5 | PatternMiner | ✅ | ✅ | ✅ | 因果挖掘完成 |
| L5 | PredictiveReasoner | ✅ | ✅ | ✅ | 预测完成 |
| L6 | PredictionMonitor | ✅ | ✅ | ✅ | 追踪准确率完成 |
| L6 | SelfTuner | 🟡 | ✅ | ✅ | 调参框架在，调参对象未确认 |
| L6 | Mirror | 🟡 | ❌ | ❌ | 未加入 MindStackRunner |
| L7 | GoalGenerator | 🟡 | ✅ | ✅ | 目标生成在，未被触发 |
| L7 | SelfRegulator | 🟡 | ✅ | ✅ | 框架在，未连接 L7 Goals |
| L8 | DriveSystem | 🟡 | ✅ | ✅ | 5种驱动力在，未被触发 |
| L8 | IntentStack | 🟡 | ✅ | ✅ | 框架在，未启动 |
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
- `AnanSessionDB` 读 sinoclaw state.db 的 session 数据
- Narrative dream 生成（NARRATIVE_SYSTEM_PROMPT）
- Recall signal system（短时记忆信号）

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

**关键问题**：
```python
# MindStackRunner 中，L3 的 AttentionQueue 是独立实例
from layers.L3_attention.attention import VigilanceMonitor, AttentionQueue
attention = VigilanceMonitor(...)   # 这个 instance
queue = AttentionQueue(...)         # 这个 instance
# 但 L4/L8 根本没拿到这些 instance
```

---

### L4 — Consciousness 🟡 部分实现
**文件**：`layers/L4_consciousness/consciousness.py`

**已有**：
- IdleDetector：120s 无输入 → idle，开始发 `L4.idle.started/ended`
- ConsciousnessEngine：idle 时按 45s 周期生成思考
- OutputGate：评估推送还是内部笔记（只有 HIGH+CRITICAL 才推送）
- 5 种思考模板：DIALOGUE_REFLECTION / QUESTION_EXTENSION / TODO_CHECK / SITUATION_ASSOCIATION / SPONTANEOUS

**缺失**：
- `set_dialogue_context()` / `set_question_context()` 从未被调用 → 对话反思/问题延伸永远为空
- `_generate_one_thought()` 的模板填充了提示词，但内容本身是"回想有没有更好回答"的问句，不是真正的思考
- 未连接 L8 DriveSystem（`L8.drive.suggestion` 事件未被主动消费）
- `note_user_input()` 从未被外部调用 → idle 检测永远感知不到用户输入

**关键问题**：
```python
# _generate_one_thought() 产出的是：
"回想刚才的对话: {context}，有没有更好的回答方式？"
# 这是个问题，不是思考产出。真正需要的是：
"刚才对话里，如果重来说，我会用更简洁的方式解释 X"
```

---

### L5 — Predictive Mind ✅ 完成
**文件**：`layers/L5_reasoning/pattern_miner.py` + `layers/L5_prediction/predictor.py`

**已有**：
- PatternMiner：订阅 `L0.circadian.bedtime`，扫描 session 历史挖掘因果规则
- CausalReasoner：discovered_links 字典存储「X→Y」规则，含 lift/confidence/support
- PredictiveReasoner：订阅所有事件，pending prediction 匹配 effect → confirmed/failed
- `what_have_i_learned()`：输出中文洞察报告
- `wisdom_facts`：去重存储

**问题**：依赖 session 历史数据，刚启动时为空

---

### L6 — Metacognition 🟡 部分实现
**文件**：`layers/L6_metacognition/`

**已有**：
- PredictionMonitor：订阅 `L5.prediction.confirmed/failed`，衰减链路 lift
- SelfTuner：订阅 `L6.metacognition.warn`，调整 min_lift / horizon_s
- Mirror（`mirror.py`）：HealthReport 元认知报告，但**未启动**

**缺失**：
- Mirror 未加入 MindStackRunner（没注册到 `_layers`）
- SelfTuner 调参后没有写回 PatternMiner（参数改了但不生效）
- Mirror 的 HealthReport 没有消费者（L7 Goals 未订阅）

**关键问题**：
```python
# SelfTuner 调参：
self._predictor._min_lift = new_min_lift
# 但 PatternMiner 用的是自己的 self._min_lift，不是 predictor 的
# 两者是独立配置！
```

---

### L7 — Goals 🔴 未触发（框架在）
**文件**：`layers/L7_goals/goal_engine.py`

**已有**：
- GoalGenerator：propose / decompose / achieve / abandon / detect_conflicts
- `what_are_my_goals()` 输出当前目标列表
- 订阅 `L6.metacognition.report` 和 `L9.self.updated` 自动生成目标

**缺失**：
- `L6.metacognition.report` 从未被发布（Mirror 没启动）
- `L9.self.updated` 从未被发布（条件是 n_new>=3，刚启动时不够）
- 没有外部触发 goal 的路径

---

### L7 Will — SelfRegulator 🟡 框架在
**文件**：`layers/L7_will/regulator.py`

- 基于 GoalGenerator 的目标做自我调节
- 未与 GoalGenerator 连接

---

### L8 — Drive System 🟡 未触发
**文件**：`layers/L8_drives/drive_system.py` + `attention_bridge.py`

**已有**：
- 5 种驱动力：CURIOSITY / COMPLETION / CARE / AESTHETICS / BOREDOM
- `trigger()` 触发驱动，`satisfy()` 满足驱动
- `active_drives()` 返回当前最活跃的驱动力

**缺失**：
- DriveSystem 从未被触发（没有事件调用 `trigger()`）
- `attention_bridge.py` 未接入（连接 DriveSystem 和 AttentionQueue 的桥）
- L4 ConsciousnessEngine 监听了 `L8.drive.suggestion`，但 DriveSystem 从未发过

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

## 层间关键断点

```
CircadianLoop.tick
    ↓ (L0.circadian.bedtime)
PatternMiner.mine_now()     ← 框架在，但 session 历史为空时无用
    ↓ discovered_links
PredictiveReasoner          ← 需要 discovered_links 有数据才能预测
    ↓ L5.prediction.upcoming
PredictionMonitor           ← 订阅 confirmed/failed，但无 pending predictions 时无数据
    ↓ L6.metacognition.warn
SelfTuner                   ← 调参，但参数没写回 PatternMiner

L8 DriveSystem             ← 从未被触发（无 trigger() 调用）
    ↓ L8.drive.suggestion
L4 ConsciousnessEngine      ← 收到 suggestion 但没有真正消费
    ↓
OutputGate.evaluate()       ← 大部分产出 importance=LOW，推送不了
```

---

## 未加入 MindStackRunner 的组件

| 组件 | 文件 | 问题 |
|---|---|---|
| Mirror | `layers/L6_metacognition/mirror.py` | `_start_layers()` 未实例化 |
| IntentStack | `layers/L8_intent/intent_stack.py` | 未注册 |
| AttentionBridge | `layers/L8_drives/attention_bridge.py` | 未激活 |
| VigilanceMonitor | `layers/L3_attention/attention.py` | 已加入，但走神事件无消费者 |
| IdleDetector (L4) | `consciousness.py` | L4 自带的，未被 `note_user_input()` 驱动 |

---

## 待办清单（按优先级）

### P0 — 必须修
1. **L6 SelfTuner ↔ PatternMiner 调参闭环**：SelfTuner 改了参数要写回 PatternMiner
2. **L4 ConsciousnessEngine 上下文注入**：让 `set_dialogue_context()` 被调用，否则永远产出空思考
3. **L4 note_user_input()**：外部事件触发 idle 检测

### P1 — 重要
4. **L1 DreamingPlugin sleep_fn 参数**：传真实的 workspace_dir 和 phase
5. **L2 MemoryTier promote 链路**：连上 L1 sleep 事件
6. **L8 DriveSystem 触发**：在合适的时机调用 `drives.trigger()`
7. **Mirror 加入 MindStackRunner**：启动 L6 元认知报告

### P2 — 完善
8. **L7 Goals 触发路径**：L6.metacognition.report → GoalGenerator（需要 Mirror）
9. **IntentStack 加入 MindStackRunner**
10. **AttentionQueue boost() 被 L8 调用**：通过 attention_bridge.py

---

## 下一步

先从 P0 开始：
1. 修 SelfTuner ↔ PatternMiner 调参闭环
2. 给 L4 注入对话上下文
3. 让 note_user_input() 被调用

完成后九层才能真正产生可观测的主动行为。
