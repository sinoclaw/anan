# 九层架构混合架构改造计划

## 目标

将九层从"全硬编码"演进为"已知逻辑→Handler / 未知推理→Subagent"的混合架构，并通过 subagent 决策积累反向强化 handler。

---

## 试点：L7 Goals Progress 追踪

### 为什么从 L7 Goals 开始

- `GoalEngine` 已有完整基础设施（achieve/abandon 事件、GoalStatus 枚举）
- `progress` 字段缺失，需要 LLM 推理才能合理量化（硬编码算不出完成度）
- 风险低：goal 状态变更有 handler 兜底，不会因 subagent 故障丢失状态
- 改动范围小，适合作为第一个试点

### 现状

- `Goal` 数据模型：有 `status`、`achieved_at`、`abandoned_at`，**无 progress 字段**
- `GoalEngine`：有 `achieve()`、`abandon()` 方法，无 progress 评估
- EventBus：`L7.goal.achieved`、`L7.goal.abandoned` 事件已存在
- L7.will（`SelfRegulator`）订阅 `L7.goal.achieved/abandoned`，但目前无实际触发（因无 goal 到达 achieved）

---

## Phase 1：代码现状梳理（已完成）

- [x] 读取 `goal_engine.py` 完整代码（941行）
- [x] 确认 `Goal` 数据模型缺少 progress 字段
- [x] 确认 `GoalEngine` 有 achieve/abandon 但无 progress 评估机制
- [x] 确认 `GoalStatus` 枚举有 `active/suspended/achieved/abandoned` 四个状态

---

## Phase 2：Progress 推理 Subagent 设计

### 设计原则

1. **Handler 兜底**：achieve/abandon 等确定性状态变更走 handler，不走 subagent
2. **Subagent 负责**：给定 goal 描述 + 当前上下文，评估 progress 0-100%
3. **混合输出**：subagent 输出 `{progress: 65, reasoning: "...", next_milestone: "..."}`
4. **Failback**：subagent 不可用时，handler 给出保守默认值（当前 milestone 完成数 / 总 milestone 数）

### 数据流

```
外部事件（如 L6 metacognition report）
    ↓
GoalEngine._on_metacognition_report()
    ↓
检查哪些 goal 处于 active 状态
    ↓
对每个 active goal 调用 ProgressAssessor（subagent）
    ↓
返回 progress 值，更新 Goal.progress
    ↓
若 progress == 100 → 调用 achieve() handler
    ↓
发布 L7.goal.progress_updated 事件
```

### ProgressAssessor Prompt 设计

```
给定 Goal：
- 描述：{goal.description}
- 创建时间：{goal.created_at}
- 当前状态：{goal.status}
- 关联的 milestone：{goal.milestones}
- 相关上下文事件：{recent_events}

评估该 goal 的完成进度（0-100%）。

要求：
1. 给出具体数值
2. 简要说明理由（1-2句）
3. 指出当前最关键的下一个 milestone（如果有）

输出 JSON 格式：
{"progress": 整数, "reasoning": "字符串", "next_milestone": "字符串或null"}
```

### Fallback Handler（保守算法）

当 subagent 不可用时：
```
progress = 已完成 milestone 数 / 总 milestone 数 × 100
```

若 goal 无 milestone：progress = 50（未知状态，需要人工介入）

---

## Phase 3：Milestone 机制增强

当前 `Goal` 数据模型无 milestone 字段，需添加：

```python
class Goal(BaseModel):
    id: str
    description: str
    status: GoalStatus
    milestones: list[Milestone] = []  # 新增
    progress: int = 0                  # 新增，0-100
    achieved_at: Optional[datetime] = None
    abandoned_at: Optional[datetime] = None

class Milestone(BaseModel):
    id: str
    description: str
    completed: bool = False
    completed_at: Optional[datetime] = None
```

Milestone 由用户在创建 goal 时指定，或由 subagent 推理建议。

---

## Phase 4：实现步骤

### Step 1：数据模型变更
- [ ] 给 `Goal` 加 `milestones: list[Milestone]` 字段
- [ ] 给 `Goal` 加 `progress: int` 字段（默认0）
- [ ] 给 `Milestone` 加 `completed`、`completed_at` 字段
- [ ] 给 `GoalEngine` 加 `add_milestone()`、`complete_milestone()` 方法
- [ ] 更新 `goal_engine.py` 的序列化逻辑（如果有 DB 持久化）

### Step 2：ProgressAssessor Subagent 封装
- [ ] 新建 `layers/L7_goals/progress_assessor.py`
- [ ] 实现 `ProgressAssessor` 类，封装 `async_call_llm` 调用
- [ ] 设计 prompt（few-shot examples + output schema）
- [ ] 实现 fallback handler

### Step 3：GoalEngine 集成
- [ ] `GoalEngine.__init__` 实例化 `ProgressAssessor`
- [ ] 修改 `achieve()`：progress 必须 == 100 才允许调用（否则 warn）
- [ ] 新增 `_assess_goal_progress()` 方法，在 `_on_metacognition_report` 等触发路径中调用
- [ ] 当 progress 到达 100，自动调用 `achieve()`

### Step 4：事件发布
- [ ] 新增 `L7.goal.progress_updated` 事件（携带 goal_id、progress_old、progress_new）
- [ ] `SelfRegulator` 订阅该事件（当 progress 显著变化时调整驱动）

### Step 5：测试
- [ ] 单元测试：Goal 数据模型变更
- [ ] 单元测试：ProgressAssessor fallback handler
- [ ] 集成测试：goal progress 0→100 完整流程
- [ ] 验证 achieve() 在 progress<100 时被拒绝

---

## Phase 5：推广路径（试点完成后）

| 层 | Handler（已知逻辑） | Subagent（未知推理） |
|---|---|---|
| L1 Sleep | circadian 调度、sleep_stage 状态机 | Daydream/Lucid narrative 生成（已有 LLM bridge） |
| L5 Pattern | 窗口滑动、计数器管理 | lift/conf 阈值动态调整 |
| L6 Meta | issue 积累、warn 条件判断（硬编码） | MetacognitionAdvisor: tuning 效果评估 + auto rollback ✓ 已完成 |
| L7 Goals | achieve/abandon/milestone 状态变更 ✓ 已有 | ProgressAssessor: progress 量化 ✓ 已完成 |
| L7 Will | adaptation history、执行 action、avoid signal 检查 | **DriveStrengthAdvisor: action 选择 + 强度评估 ✓ 已完成** |
| L9 Self | 状态写入、wisdom_facts 存储 | self-evaluation 总体评估 |

---

## 核心原则

1. **subagent 不能绕过 handler 的状态一致性保护** — goal 状态变更是事务性的，subagent 只给建议，handler 才是决策者
2. **所有 subagent 输出可审查** — 每个 subagent 调用都记日志，包括输入、输出、耗时
3. **handler 优先于 subagent** — subagent 不可用或超时时，handler 提供保守 fallback
4. **自我强化是长期目标** — 积累 subagent 决策历史，定期分析，识别可 formalize 的模式

---

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| subagent 输出不稳定 | structured output + schema validation，不合格重试3次 |
| progress 评估主观性强 | 要求 subagent 输出 reasoning，人类可审查 |
| 错误 progress 导致误 achieve | achieve() 加 progress==100 强校验 |
| subagent 延迟高 | 异步调用，不阻塞 event dispatch |
