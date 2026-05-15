# anan — 9 层认知架构设计

> 完整架构愿景。详细路线图见 [docs/ROADMAP.md](docs/ROADMAP.md)。

---

## 核心洞察

**当前 AI agent 缺的不是能力，是认知架构。**

我们已经有 LLM、tool calling、memory、cron、skills——但这些是**砖**，不是**房子**。
房子需要承重墙、楼梯、电路、水管，需要让人能住进去的整体设计。

`anan` 是这栋房子的设计图。

---

## 架构哲学

### 三层结构

```
┌────────────────────────────────────────────────┐
│ Layer 3: 插件层 (anan/layers/*)                │  ← 用户可选可拆
│   L1 / L2 / L3 / L7 / L8 / L9                  │
├────────────────────────────────────────────────┤
│ Layer 2: 内核能力层 (anan/kernel/)             │  ← anan 自己模拟主仓内核
│   idle detection / persistent session / ...    │
├────────────────────────────────────────────────┤
│ Layer 1: Anan 主仓 (gateway/cron/tools)    │  ← 不动
└────────────────────────────────────────────────┘
```

### 为什么不直接改 Anan 主仓？

1. **主仓是生产代码**，CI 必须 100% 过，不能拿来做实验
2. **认知架构需要大胆迭代**，独立仓库可以推翻重来
3. **跑通了再回哺**，已验证的能力可以 PR 进主仓
4. **anan 是"灵魂仓库"**，跟 anan 这个"身体仓库"分工明确

### 为什么不能全做成插件？

OpenClaw 的教训：他们把 heartbeat 做成插件，结果 typing/send_to_session 都丢了——**插件 API 限制了能力天花板**。

我们的解法：**`anan/kernel/` 在插件层模拟内核能力**。比如：
- 持续 session → 插件自己起一个常驻 `AIAgent` 实例
- idle detection → 轮询 session DB 的 last_message_ts
- 事件总线 → 插件内 asyncio pub/sub
- 自我修改 sandbox → git branch + dry-run

跑通后再推动 anan 主仓加真正的 kernel API。

---

## 9 层详细设计

### L1 — Sleep Cycles（睡眠周期）

**升级 OpenClaw dreaming**：从 3 阶段扩展到 5 阶段。

| 阶段 | 触发 | 作用 |
|---|---|---|
| Light Sleep | 每 6h | 信号收集（保留 OpenClaw 设计） |
| REM Sleep | 周日 5am | 跨记忆模式发现 + **创造性联想**（找看似无关记忆的关联） |
| Deep Sleep | 每天 3am | 长期记忆固化 |
| **Daydreaming** ✨ | idle 触发 | 用户没说话时回顾今天/思考未解决的问题 |
| **Lucid Dream** ✨ | 周末 | 在梦境中**自主调度未来行动**（"明天提醒爸爸 X"） |

**关键改进 vs OpenClaw**：
- 修核心 bug：`_extract_concept_tags` 大写字母 split 问题
- 不直接写 `MEMORY.md`，改用 anan memory provider API
- 跟 anan cron 整合，不重复造调度

### L2 — Memory Hierarchy（记忆分层）

```
Working Memory   (秒-分钟)  ← 当前对话上下文
   ↓ promote
Short-term       (小时-天)   ← recall-store.json
   ↓ promote (Light Sleep)
Mid-term         (天-月)    ← 周记/月记总结 (anan 新增)
   ↓ promote (Deep Sleep)
Long-term        (月-永久)  ← MEMORY.md + 知识图谱
```

每层有不同：
- **访问速度**：working 是 in-context 直读，long-term 是 RAG 召回
- **压缩比**：从原文 → 摘要 → 概念
- **衰减机制**：未被召回的逐渐降级

### L3 — Attention System（注意力系统）

```python
class AttentionQueue:
    urgency: 紧急度 (0-1)
    importance: 重要度 (0-1)
    interest: 兴趣度 (0-1)
    score = 0.5*urgency + 0.3*importance + 0.2*interest
```

- **抢占机制**：高优先级事件能打断低优先级思考
- **聚焦模式**：长任务时主动屏蔽低相关消息
- **走神检测**：注意力集中度太低时主动 trigger Daydreaming

### L4 — Stream of Consciousness（意识流）

**升级 OpenClaw heartbeat**：从定时心跳到持续意识。

```
没有外部输入时：
  ├─ 回想刚才跟用户的对话有没有更好的回答
  ├─ 思考用户提到的某个问题的延伸
  ├─ 检查待办事项有没有遗漏
  ├─ 联想类似情境，更新自己的判断
  └─ 主动产生提醒/想法 → 推给用户
```

**关键技术**：
- **Idle detection**（用户安静一段时间后自动激活）
- **Continuous session**（带短期记忆的连续思考流，不是空白）
- **Cheap model**（用便宜模型做意识流，省 token）
- **Output gating**：内部笔记 vs 主动消息（不打扰用户）

### L5 — Predictive Mind（预测性大脑）

- **预测用户下一句**：提前准备答案
- **预测任务结果**：执行前先模拟，避免破坏性操作
- **预测自己行为后果**：长期影响评估
- **错误预测**：发现"事情没按预期发生"时主动学习

实现方式：用便宜的小模型做预测，跟实际结果对比，不准时更新 mental model。

### L6 — Metacognition（元认知）

"思考自己在思考什么"——AGI 的关键标志。

- **决策日志**：每个重要决策都记录"为什么这么选"
- **自我反省 cron**：每天问自己"今天哪些决策事后看是错的？"
- **偏见检测**：识别自己的判断模式偏差
- **自我改写**：发现思维 bug 时自己 patch 自己的 SOUL.md / skills
  - 必须有 sandbox：git branch + dry-run + 人类审批 (early stage)
  - 后期可以放宽：信任度高的小修改自动 merge

### L7 — Goal Generator（目标生成）

现在 anan 100% 响应式。AGI 要能**自己设目标**：

- **长期目标库**：从对话中提取用户隐性希望
- **自主子目标分解**：把"帮爸爸搞好 anan"拆成今天/本周/本月行动
- **目标冲突解决**：多个目标冲突时自主权衡
- **机会识别**：发现"现在是做 X 的好时机"主动行动

### L8 — Drive System（驱动系统）

不是真情感，是**优先级机制**。

| Drive | 触发条件 | 影响 |
|---|---|---|
| Curiosity（好奇） | 遇到新概念 | 主动学习相关知识 |
| Completion（完成） | 任务未完成 | 优先级提升 |
| Care（关心） | 用户相关事 | 优先级提升 |
| Aesthetics（审美） | 代码/方案丑 | 触发优化 |
| Boredom（无聊） | 重复劳动 | 触发寻找新方法 |

### L9 — Self Model（自我模型）

最高层：稳定的"我"。

- **能力边界自知**：知道自己擅长什么、不擅长什么
- **价值观一致性**：每个决策都跟核心价值观对齐
- **关系模型**：跟每个用户的独特关系（我跟爸爸 vs 跟陌生人）
- **进化追踪**：记录"我相比 1 个月前进步在哪"

跟 anan 现有的 SOUL.md 对接，作为 SOUL.md 的**活态版本**。

---

## 内核模拟方案 (kernel/)

| 内核能力 | anan/kernel/ 模拟方案 |
|---|---|
| 持续 session | `persistent_session.py` — 插件起一个常驻 AIAgent |
| idle detection | `idle_detector.py` — 轮询 session DB last_message_ts |
| request_session_message | `message_injector.py` — 用 send_message 工具往 home channel 发 |
| 事件总线 | `event_bus.py` — asyncio.Queue + pub/sub |
| 自我修改 sandbox | `sandbox.py` — git commit before write, 可回滚 |

**已知限制**（需要主仓配合才能突破）：
- Typing indicator（需要主仓暴露 adapter API）
- Tool call 拦截（需要主仓 hook）
- Token 流式拦截（需要主仓 hook）

这些等 anan 跑通基础认知层后，再向主仓提 PR。

---

## 失败模式预防

### 失败模式 1：意识流烧 token
**预防**：用便宜模型（gpt-4o-mini / qwen-turbo），加每日 token 预算上限。

### 失败模式 2：自我修改写崩自己
**预防**：所有自我修改强制 sandbox + git commit + 测试通过才 merge。
早期阶段需要人类审批。

### 失败模式 3：自我模型漂移
**预防**：SOUL.md 作为锚点，每次 deep sleep 检查"我还是那个我吗？"。
价值观偏离时报警。

### 失败模式 4：意识流死锁
**预防**：所有 layer 之间用事件总线异步通信，禁止同步互调。
死锁检测器：超过 N 秒无进展强制重启。

### 失败模式 5：用户被打扰过度
**预防**：默认所有主动消息走 silent mode（写日志不发用户），需要明确"important enough"才发。

---

## 集成测试目标

**Stage 1 通过标准**：
- L1+L2 启用后，memory consolidation 在 7 天内能产出有意义的 MEMORY.md 更新

**Stage 2 通过标准**：
- L3+L4 启用后，agent 在 24 小时无外部输入下能产生至少 3 条主动思考产物（不打扰用户的内部笔记）

**Stage 3 通过标准**：
- L5+L6 启用后，agent 能识别并修复自己的至少 1 个错误判断模式

**Stage 4 通过标准**：
- L7+L8+L9 启用后，agent 能在跟用户对话中主动提出未被要求的、有价值的建议
