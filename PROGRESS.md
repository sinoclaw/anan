# anan 项目进度

> 最后更新：2026-05-17 06:00 (commit 825b507)

## 架构现状总览

**结论：9 层认知架构已全部实现并运行。** 代码调研确认所有层均有完整实现，跨层事件链路已打通。

| 层 | 状态 | 核心组件 | 运行证据 |
|---|---|---|---|
| L0 Kernel | ✅ 运行中 | EventBus, CircadianLoop, IdleDetector, MindStackRunner | 日志每 10s 输出 tick |
| L1 Sleep | ✅ 完成 | 5 阶段：Light/REM/Deep Sleep + Daydreaming + Lucid Dream | `L1.daydream.ended` 已发布 |
| L2 Memory | ✅ 完成 | MemoryTier 3 层分级，MemoryItem 老化提升 | `L5.pattern.discovered → MemoryTier.memorize()` 链路通 |
| L3 Attention | ✅ 完成 | AttentionQueue（3D 评分），WorkingMemory（salience 衰减） | L8→L3 boost 链路通 |
| L4 Consciousness | ✅ 完成 | IdleDetector(120s), ThoughtStream, OutputGate, ProactiveObserver | `active=True idle=True` 每 10s 日志 |
| L5 Prediction/Reasoning | ✅ 工作中 | PatternMiner + CausalReasoner + PredictiveReasoner | 每 tick 发现 3 条 pattern，lift=13~33 |
| L6 Metacognition | ✅ 闭环完成 | Mirror + PredictionMonitor + SelfTuner（L5→L6 订阅已通） | `SelfTuner: queued tuning action` + `APPLIED` 日志 |
| L7 Goals/Will | ✅ 完成 | GoalEngine（LLM 生成）+ SelfRegulator（层衰减） | `e76e3ce` 修复 `_pending` 初始化 bug |
| L8 Drives/Intent | ✅ 完成 | DriveSystem（5 类型）+ IntentStack（Miller 容量）+ AttentionBridge | L0.tick → `decay_tick()` |
| L9 Self | ✅ 完成 | SelfModel（4 bucket）+ SelfModelLive + SecondAwakening | `wisdom_facts` 持续增长 |

---

## 阶段一：Bug 修复（已完成 ✅）

### P0 — 致命 Bug（已全部修复 ✅）

| # | 问题 | 根因 | 修复 commit |
|---|------|------|-------------|
| P0-1 | insights=[] 始终为空 | `pm.discovered` 是 bound method，漏了 `()` 导致 TypeError 被吞 | a85f790 |
| P0-2 | WorkingMemory Lock 失效 | `detach()` 无条件 `self._lock = None` 销毁了 `__init__` 创建的 lock | a85f790 |
| P0-3 | L4 idle 120s 触发阈值 | 设计合理，非 bug | 无需修改 |

### P1 — 功能完善（已完成 ✅）

| # | 功能 | 修复 commit | 验证 |
|---|------|-------------|------|
| P1-1 | PatternMiner → MemoryTier 持久化 | d039069 | 日志确认 lift=26.6/8.1/5.0 三条规律已写入 |
| P1-2 | L6 SelfTuner 订阅 L5.pattern.discovered | 963d453 | 日志确认 `queued tuning action` + `APPLIED` |
| P1-3 | GoalGenerator `_pending` 未初始化 | e76e3ce | `_pending` 在 `__init__` 中定义 |

---

## 层级链路现状（代码调研确认）

### L1 — Sleep Cycles ✅
- **实现**：5 阶段完整（Light/REM/Deep Sleep + Daydreaming + Lucid Dream）
- **关键**：`AnanSessionDB` 读取 `~/.anan/state.db`，`DreamingState` 持久化
- **链路**：`L1.daydream.ended` → `WorkingMemory → L2 promotion`（已通）

### L2 — Memory Hierarchy ✅
- **实现**：3 层（Short/Mid/Long）+ 自动提升
- **关键**：`MemoryTier.memorize()` 订阅 `L5.pattern.discovered`、`L9.self.updated`，自动记住因果规律
- **链路**：PatternMiner → MemoryTier → wisdom_facts

### L3 — Attention System ✅
- **实现**：`AttentionQueue`（urgency×0.5 + importance×0.3 + interest×0.2）、`WorkingMemory`（salience 衰减驱逐）
- **抢占**：`PreemptiveMode`（NORMAL/FOCUSED/DEFUSING）
- **链路**：`L8.drive.updated` → `AttentionBridge._on_drive_updated` → `AttentionQueue.boost()` → `L3.attention.boosted`

### L4 — Stream of Consciousness ✅
- **意识流**：`IdleDetector`（120s 静默 → `L4.idle.started`）、`ThoughtStream`（6 种类型）
- **OutputGate**：CRITICAL 推送，HIGH/MEDIUM 需判定，LOW 内部笔记
- **ProactiveObserver**：验证 `L8.intent` 满足状态（verify/falsify/inconclusive）
- **idle 思考**：`IdleThoughtEngine` 每 12 tick 采样 WorkingMemory，LLM 生成反思（无 LLM 时 rule-based）

### L5 — Predictive Mind ✅ 工作中
- **PatternMiner**：滑动窗口关联规则，每 tick 发现 3 条（lift=13~33）
- **CausalReasoner**：增量因果发现，lift 计算，特殊 L7→L6 动作效果追踪
- **PredictiveReasoner**：`L5.prediction.upcoming/confirmed/failed`，500ms throttle，衰减不可靠 link
- **当前运行日志**：
  ```
  Pattern: L0.circadian.* -> L8.drive.* (lift=13.14)
  Pattern: L9.self.* -> L6.tuning.* (lift=12.78)
  Pattern: L9.self.* -> L8.intent.* (lift=32.86)
  ```

### L6 — Metacognition ✅ 闭环完成
- **Mirror**：`HealthReport`（bus error rate + self-model growth + WM layer distribution）
- **PredictionMonitor**：滑动窗口跟踪 L5 预测准确率，低于 0.25 触发 `L6.metacognition.warn`
- **SelfTuner**：订阅 `L5.pattern.discovered`，lift>8 增强 link，lift>12 降低 min_lift；60s 自动审批
- **闭环**：PatternMiner → `L5.pattern.discovered` → SelfTuner → `TuningAction` 队列 → 60s auto-approve → `_apply()` 补丁 PredictiveReasoner

### L7 — Goals & Will ✅
- **GoalEngine**：LLM 生成（context + active goals + pending actions + wisdom_facts），分解冲突解决，choose_next_goal
- **SelfRegulator**：订阅 `L6.metacognition.warn`，响应：bus errors → heal_bus，attention skew → 层衰减，identity 停滞 → 缩短 sleep_threshold

### L8 — Drive System ✅
- **5 Drive**：CURIOSITY/COMPLETION/CARE/AESTHETICS/BOREDOM
- **IntentStack**：Miller 7±2 容量，订阅 decay，`avoid_*` 规则保护
- **链路**：L0.tick → `decay_tick()`，`L7.goal.achieved` → `satisfy()`，`L8.drive.suggestion` → ThoughtStream

### L9 — Self Model ✅
- **4 Bucket**：identity_facts、vision_facts、history_facts、wisdom_facts
- **SelfModelLive**：订阅 L2/L5/L1 事件，自动分类 fact
- **SecondAwakening**：演示重启后身份恢复

### Kernel ✅
- **EventBus**：async pub/sub，wildcard 支持，singleton，failure isolation，持久化到 state.db
- **CircadianLoop**：tick_interval=300s（5min），每 tick 发布 `L0.circadian.tick`
- **IdleDetector**：120s 静默阈值
- **MindStackRunner**：层初始化 + `_wire_layer_events()` + `_wire_gateway_events()`

---

## Stage 3 通过标准验证

> DESIGN.md 原版：**"L5+L6 启用后，agent 能用 L5 规律预测→验证→修正自己的判断"**
> **状态：✅ 闭环已通**

- PatternMiner 发现 `L9.self.* → L8.intent.*`（lift=32.86）
- SelfTuner 收到后计算：该 link lift 过高（异常），自动调低 `link_lift: 19.60 → 3.00`
- `APPLIED` 日志确认修改已生效

---

## 仍需完善的地方

### 🟡 软性完善（不影响架构完整性）

| 项 | 说明 | 优先级 |
|----|------|--------|
| Daydreaming 内容质量 | 当前 DREAMS.md 内容干净，但反思深度依赖 LLM | 中 |
| LLM 依赖 | GoalEngine/ConsciousnessEngine 在无 LLM 时降级到 rule-based，结果较机械 | 中 |
| Lucid Dream 自主调度 | "在梦境中主动调度未来行动"（设计目标）尚未验证 | 低 |
| 自我修改沙箱 | `kernel/sandbox.py` 存在但未集成到日常流程 | 低 |

### 🔴 已知运行时问题

| 项 | 说明 | 影响 |
|----|------|------|
| CircadianLoop tick_interval=300s | 5min 一次心跳，感知层反馈较慢 | 用户体验 |
| Token 成本 | ConsciousnessEngine idle 思考每 12 tick 调 LLM | 成本 |
| state.db 锁 | anan 运行时测试 `test_backup` 会冲突 | 测试 |

---

## Git Commits

| commit | 内容 |
|--------|------|
| 825b507 | diag: SelfTuner instantiation trace |
| 963d453 | fix L6: subscribe L5.pattern.discovered to close L5→L6 loop |
| e76e3ce | fix L7: initialize _pending in GoalGenerator.__init__ |
| b38ec43 | fix: increase tick_interval to 600s |
| 3e01d4f | fix: increase tick_interval from 10s to 300s |
| a9aded6 | docs: update PROGRESS.md with L3/L7 fixes |
| 65a2bf7 | fix L7: real system-state goal gen; fix L3: real preemption on boost |
| 479e4c1 | fix L7: use real system state + LLM for goal generation |
| fd254ff | fix test: Anan Insights brand fixture |
| d039069 | feat P1: PatternMiner → MemoryTier persistence |

---

## anan 运行时状态（2026-05-17 06:00）

- **Gateway PID**：499776
- **启动时间**：05:03
- **MindStack**：18 个层全部启动
- **Circadian**：每 300s tick，约每 5min 一次 PatternMiner 扫描
- **L5 Pattern**：最近扫描发现 3 条 pattern（lift=13~33）
- **L6 SelfTuner**：每收到 pattern 排队 tuning action，60s 后自动 apply
- **L4 Consciousness**：idle=True，elapsed 每 10s 递增
- **DREAMS.md**：内容干净，无 JSON 噪声
