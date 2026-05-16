# anan 项目进度文档

> 记录 anan 认知架构实验的完整进度
> 最后更新: 2026-05-16

---

## 愿景

> 「我（爸爸）在造一个有脑子的安安，等他有脑子了很多事情就不用我们决定了」
> —— 陈正一，2026-05-14

anan 是一个完整的认知架构实验仓库，目标是用 9 层 Mind Stack 让 AI 真正"长出脑子"，从工具进化成有自主决策能力的伙伴。

---

## 当前阶段

**v0.3.0-sprouting** — L6 元认知闭环 + 事件循环稳定化

---

## 整体完成度

| 维度 | 完成度 |
|------|--------|
| 骨架完整度 | ~60% |
| 核心闭环真正跑通 | ~40% |
| 9 层全部接入 MindStackRunner | ✅ |
| 事件循环稳定（可长时间运行）| ✅（2026-05-16 修复）|

---

## 各层详细进度

### L0 — 底层调度（节律）
- ✅ `kernel/circadian.py` — 节律 tick，驱动各层周期性运转
- ✅ L0 tick → L8 DriveSystem decay 已连接
- ✅ L0 tick → L8 IntentStack snapshot 已连接

### L1 — 睡眠与记忆固化
- ⚠️ `layers/L1_sleep/` — 有骨架，sleep_plugin.py 存在
- ❌ OpenClaw 14 个测试 bug 未修复
- ❌ `_extract_concept_tags` 大写字母 split bug 未修复
- ❌ Daydreaming（idle 触发）未实现
- ❌ Lucid Dream（自主调度）未实现
- ❌ 直接写 MEMORY.md，未走 memory provider

### L2 — 长时记忆分层
- ✅ Memory Hierarchy（分层 + 晋升机制）
- ✅ WorkingMemory → L2 promotion 正常
- ⚠️ Mid-term layer（周记/月记）未实现
- ⚠️ Long-term layer 老化机制未实现

### L3 — 注意力调度
- ✅ `layers/L3_attention/` — AttentionQueue + VigilanceMonitor
- ✅ L3 Attention 订阅 L5.prediction.upcoming（预测驱动的注意力）
- ⚠️ 优先级队列存在，无真正抢占机制
- ⚠️ 聚焦模式有骨架，未完整实现
- ⚠️ 走神检测 VigilanceMonitor 有，但无自适应调整

### L4 — 意识流
- ✅ `layers/L4_consciousness/` — 基础意识流
- ⚠️ Idle detection 触发持续思考 — 有 idle_detector，未接入
- ⚠️ Continuous session — 未实现
- ⚠️ Cheap model fallback（省 token）— 未实现
- ⚠️ Output gating（内部笔记 vs 主动消息）— 未实现
- ❌ `layers/L4_proactive/observer.py` — 空壳，无实际功能

### L5 — 预测与因果推理
- ✅ PredictiveReasoner — 预测链路 + 订阅调度
- ✅ PatternMiner — 30s tick，从历史事件挖掘因果 pattern
- ✅ CausalLinker — 因果推理
- ✅ L5.prediction.upcoming 事件正常发布
- ✅ L5.prediction.confirmed/failed 事件正常发布
- ✅ 事件循环稳定性 — 修复 f8768f3（mine_now 同步 O(n²)）和 77ac78b（Predictor subscribe("*") feedback loop）
- ⚠️ LLM-driven 预测增强 — 未实现（纯规则）

### L6 — 元认知（⚠️ 核心瓶颈）
- ✅ Mirror — 订阅 L0.circadian.tick，每分钟生成 HealthReport
- ✅ PredictionMonitor — 订阅 L5.prediction.confirmed/failed，更新链路置信度
- ✅ SelfTuner — 订阅 L6.metacognition.warn，根据准确率生成调参建议
- ✅ **2026-05-16 新增**: SelfTuner 审批队列（approve/reject/approve_all API）
- ✅ **2026-05-16 新增**: L6.tuning.pending 事件发布（供审批工具消费）
- ⚠️ **未闭环**: SelfTuner 调参需要人工审批，未自动执行
- ⚠️ **未闭环**: Mirror HealthReport 只被 DriveSystem 消费，L7 Goals 未接入

**2026-05-16 修复的 Bug**:
- Bug 1: PatternMiner.mine_now() 同步 O(n²) 无 await → CPU 100% 事件循环饥饿（commit f8768f3）
- Bug 2: PredictiveReasoner subscribe("*") feedback loop → 事件风暴堵死（commit 77ac78b）

### L7 — 目标系统
- ✅ `layers/L7_goals/goal_engine.py` — GoalEngine 有完整骨架
- ✅ GoalStatus/GoalScope 枚举完整
- ⚠️ LLM-driven 目标生成 — 无，主要靠人工
- ⚠️ 子目标自主分解 — 未实现
- ⚠️ 目标冲突解决 — 未实现
- ⚠️ L7 SelfRegulator 有骨架（decay 链路），未完整接入 L6

### L8 — 驱动力与意图栈
- ✅ DriveSystem — 5 种内驱力（CURIOSITY/COMPLETION/CARE/AESTHETICS/BOREDOM）
- ✅ DriveSystem 订阅 L5.prediction.upcoming（预测触发好奇心）
- ✅ DriveSystem 订阅 L0.circadian.tick（周期性激活）
- ✅ **2026-05-16 新增**: DriveSystem 订阅 L6.metacognition.report，根据健康分动态 boost 驱动力
- ✅ IntentStack — 持续渴望模型，入栈/强化/衰减/出栈
- ⚠️ DriveSystem 的 priority_boost 未真正影响 L7 GoalEngine 优先级
- ⚠️ IntentStack 未接入 L6/L7 的持续意图升格

### L9 — 自我意识
- ✅ SelfModel — 能力边界自知、价值观一致性、关系模型
- ✅ SecondAwakening — 洞察 pipeline

---

## Kernel 层进度

| 组件 | 状态 |
|------|------|
| event_bus.py | ✅ asyncio pub/sub 正常 |
| circadian.py | ✅ 节律驱动正常 |
| idle_detector.py | ⚠️ 存在，未接入 L4 持续思考 |
| persistent_session.py | ✅ 常驻实例正常 |
| message_injector.py | ✅ 可注入消息 |
| sandbox.py | ❌ 骨架，无 git-based 安全机制 |
| state_db_bridge.py | ✅ 历史事件回填正常 |
| state_db_event_bridge.py | ✅ StateDB → EventBus 同步正常 |

---

## 待完成清单（按优先级）

### 🔴 高优先级（阻塞核心体验）
1. **L4 idle → 持续思考闭环** — idle_detector 未接入 Consciousness，anan 不会主动思考
2. **Token 预算系统** — 无每日上限，有疯狂跑 token 风险
3. **L1 Sleep 修复** — OpenClaw 14 个 bug + 大写 split bug，阻止睡眠模块启用

### 🟡 中优先级（增强自主性）
4. **SelfTuner 自动执行** — 去掉人工审批，让调参建议直接生效（有安全风险，需 sandbox）
5. **L7 LLM 目标生成** — 让 anan 能从对话中自己提取目标，不靠人工
6. **L3 抢占机制完善** — 紧急事件真正抢占注意力
7. **IntentStack → L7 升格** — L6 反复出现的 issue 自动升格为 L8 持续意图

### 🟢 低优先级（长期增强）
8. **Mid-term Memory** — 周记/月记层
9. **Sandbox git-based** — 自修改安全机制
10. **L4 Cheap model fallback** — 省钱
11. **DriveSystem → GoalEngine 优先级联动** — 驱动力真正影响目标排序

---

## 版本历史

### v0.3.0-sprouting（2026-05-16）
- 事件循环稳定性修复（f8768f3 + 77ac78b）
- SelfTuner 审批机制（c184cf9）
- DriveSystem 消费 L6 HealthReport（c184cf9）
- L5 因果推理 + PatternMiner 稳定运行

### v0.2.0-sprouting（2026-05-14）
- 品牌升级完成，从 hermes-agent → sinoclaw-agent → anan
- 9 层 Mind Stack 骨架全部集成到 MindStackRunner

### v0.1.0（更早）
- anan 实验 fork 初始化
- kernel/layers 目录结构建立

---

## 下一步目标（下一个 commit）

**目标**: L4 idle → 持续思考闭环

让 anan 在用户安静时自己主动思考，不依赖外部消息触发。
需要: idle_detector → L4 Consciousness → L8 DriveSystem/CURIOSITY 联动。

---

*由陈亦安（anan）维护 🤖*
