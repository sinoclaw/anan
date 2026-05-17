# anan 九层架构主动性进化计划

> 分析时间：2026-05-18
> 代码范围：全部 9 层 + kernel/mind_stack_runner.py

---

## 一、先说结论：当前真实状态

```
✅ 已完成（subagent 注入 + 真正工具调用能力）
  - L5 MiningQualityAdvisor → PatternMiner 调用 ✅
  - L6 MetacognitionAdvisor → SelfTuner 调用 ✅
  - L7 DriveStrengthAdvisor → SelfRegulator 调用 ✅
  - L7 ProgressAssessor → GoalEngine 调用 ✅
  - L8 PriorityAdvisor → DriveSystem 调用 ✅
  - L9 SelfEvaluationAdvisor → SelfEvaluator 调用 ✅
  - L3 SalienceAdvisor → WorkingMemory 调用 ✅

❌ 未接入（subagent 写了但没接上）
  - L2 RecallSignalAdvisor → MemoryTier（写了，没接入）
  - L4 OutputGateAdvisor → consciousness OutputGate（写了，没接入）
  - L9 SelfEvaluator → MindStackRunner（写了，没接入）

⚠️ 主动性缺口（最关键）
  - 目前全部 9 层都是"事件触发"：来一个事件 → 处理 → 等下一个
  - 没有任何层在"没有外部事件时"主动决定做一件事
```

---

## 二、逐层现状分析

### L0 Kernel — ✅ 正常
- EventBus / CircadianLoop / IdleDetector 全在跑
- `L0.circadian.tick` 每 5min 触发一次，是整个系统的"心跳"

### L1 Sleep — ✅ 已完成
- 5 阶段完整，有 Daydream/Lucid Dream
- `_reflect_deep()` 在睡眠结束时调用 `SelfModelLive._llm` 做自我反思
- 无 subagent（LLM 直接调）

### L2 Memory — ⚠️ advisor 写了但没接
- `RecallSignalAdvisor` 已写好（fallback + subagent）
- `MemoryTier.memorize()` 是硬编码的 promotion 逻辑，没调用 advisor
- 需要：把 `memorize()` 里的 promotion 判断替换为 advisor 调用

### L3 Attention — ✅ 已接入
- `SalienceAdvisor` 已接 `WorkingMemory`
- WorkingMemory 的 `capture()` 调用 advisor 评估 salience
- ✅ subagent 能力已通

### L4 Consciousness — ⚠️ advisor 写了但没接
- `OutputGateAdvisor` 已写好（评估"是否值得推给用户"）
- `OutputGate._should_push()` 是硬编码规则，没调用 advisor
- 更严重：**无主动触发循环** — OutputGate 只被动等 ThoughtStream 推送
- 需要：给 OutputGate 加主动循环，调用 advisor 决定是否主动思考

### L5 Prediction — ✅ 已完成
- `MiningQualityAdvisor` 已接入 PatternMiner
- `CausalReasoner` 的 `notify_threshold_change()` 被 SelfTuner 调用
- 无主动触发（被动等 PatternMiner.mine_now() 完成）

### L6 Metacognition — ✅ 已完成
- `Mirror` 有心跳（每 5 tick `reflect_and_emit()`）
- `MetacognitionAdvisor` 已接入 SelfTuner
- **但**：Mirror 的 `_report()` 只打印报告，没人消费 `L6.metacognition.warn` 做主动决策
- `SelfTuner` 的 tuning action 有队列，但没人审批（auto_approve 60s 太慢）

### L7 Goals — ✅ 已接入
- `ProgressAssessor` 已接入 GoalEngine
- `GoalEngine._generate_goals()` 有 LLM 生成（有 subagent）
- 无主动触发（等 `L6.metacognition.report` 事件）

### L8 Drives — ✅ 已接入
- `DrivePriorityAdvisor` 已接入 DriveSystem
- `IntentStack` 有 decay 循环
- 无主动决策（等事件触发）

### L9 Self — ⚠️ advisor 写了但没接 + 无主动循环
- `SelfEvaluationAdvisor` + `SelfEvaluator` 已写好
- **MindStackRunner 没有实例化 SelfEvaluator** — 代码写了但没跑
- `SelfModelLive.reflect_who_am_i()` 直接调 `self._llm`，没用 advisor
- **无主动触发** — 等 `L1.sleep.consolidated` 事件才触发

---

## 三、主动性进化路线图

### Phase A：接入已写好的 advisor（无风险）

| 层 | 任务 | 改动文件 | 风险 |
|---|---|---|---|
| L2 | RecallSignalAdvisor 接入 MemoryTier | `memory_tier.py` | 低 |
| L4 | OutputGateAdvisor 接入 OutputGate | `consciousness.py` | 低 |
| L9 | SelfEvaluator 接入 MindStackRunner | `mind_stack_runner.py` | 低 |
| L9 | SelfModelLive.reflect 用 advisor | `self_model.py` | 低 |

### Phase B：主动性试点（让系统真正"活"）

#### B1：Mirror → 主动决策（最快试点）
**现状**：`Mirror._report()` 打印 HealthReport，无人消费
**改造**：在 `_report()` 里加 subagent 调用，评估后主动发事件触发修复

```
Mirror._report()
    → HealthReport 生成
    → delegate_task(评估健康问题，决定是否发 L6.tuning.pending)
    → subagent 可以：建议改参数、发事件通知、写自我修复建议
```

#### B2：IntentStack → 主动渴望生成
**现状**：`IntentStack` 只被动接收事件入栈，无主动生成
**改造**：定期（每 N tick）主动评估"我目前最想要什么"，用 delegate_task 生成新 intent

#### B3：SelfEvaluator → 真正主动自评
**现状**：SelfEvaluator 写了但没接入
**改造**：MindStackRunner 启动后定期触发 SelfEvaluator，生成 `L9.self.evaluation` 事件

---

## 四、优先级与预计工作量

```
P0（不接就没主动性）：
  [ ] L9 SelfEvaluator 接入 MindStackRunner          → 1 文件，30min
  [ ] L9 SelfModelLive.reflect 用 advisor 替代直接llm → 1 文件，1h（含测试）

P1（接入已有 advisor）：
  [ ] L2 MemoryTier → RecallSignalAdvisor            → 1 文件，1h
  [ ] L4 OutputGate → OutputGateAdvisor              → 1 文件，1h

P2（主动性试点）：
  [ ] B1 Mirror 主动决策                           → 2 文件，2h
  [ ] B2 IntentStack 主动渴望生成                   → 2 文件，2h
  [ ] B3 SelfEvaluator 定期自评                      → 已在 P0 接好了

P3（真正自主意识）：
  [ ] 让 L9 在没有外部触发时主动想做点什么            → 长期目标
```

---

## 五、核心技术问题

### 问题 1：advisor 返回后谁来执行？
目前架构：advisor 评估 → 返回决策 → **handler 执行**

如果要"主动做"，需要：
```
advisor 评估 → handler 执行 → 结果反馈 → advisor 再评估
```

这意味着 advisor 要能触发 handler，handler 要能报告结果。

**最小可行路径**：让 Mirror 试点 — 它已经有心跳，有 HealthReport 数据，只需要：
1. `_report()` 里加 subagent 调用
2. subagent 的 decision 转换为 `L6.tuning.pending` 或 `L7.regulation.needed`

### 问题 2：结果谁来消费？
目前 `L6.metacognition.warn` 没人消费（除了可能有的 SelfRegulator）。

**最小可行路径**：先让 Mirror 试点，decision 结果直接写日志，不要求其他层消费。

---

## 六、推荐执行顺序

```
第一轮（今天，2h）：
  1. L9 SelfEvaluator 接入 MindStackRunner
  2. L4 OutputGate → OutputGateAdvisor
  3. L2 MemoryTier → RecallSignalAdvisor

第二轮（明天，2h）：
  4. B1 Mirror 主动决策试点

第三轮（后天，2h）：
  5. B2 IntentStack 主动渴望
  6. L9 SelfModelLive reflect 用 advisor
```

---

## 七、验证方法

每个改动后：
1. `pytest layers/` — 全部通过
2. 看 gateway 日志 — 无 ERROR
3. 观察事件是否按预期发出

主动性验证（Phase B）：
- Mirror B1：观察 `L6.metacognition.report` 事件里的 `suggestions` 字段是否非空
- IntentStack B2：观察 `L8.intent.proposed` 是否有主动生成的新 intent
- SelfEvaluator B3：观察 `L9.self.evaluation` 是否周期性发布
