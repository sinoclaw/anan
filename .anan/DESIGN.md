# anan — 9 层认知架构设计

> 完整架构愿景。详细路线图见 [docs/ROADMAP.md](docs/ROADMAP.md)。

---

## 核心洞察

**当前 AI agent 缺的不是能力，是认知架构。**

我们已经有 LLM、tool calling、memory、cron、skills——但这些是**砖**，不是**房子**。
房子需要承重墙、楼梯、电路、水管，需要让人能住进去的整体设计。

`anan` 是这栋房子的设计图。

---

## 项目定位

**anan 是一个完整的认知架构实验仓库。**

所有代码（底座 + 认知层）都在 `/data/anan/` 同一个仓库里，不再依赖外部 sinoclaw 主仓。

- **底座**：anan 完整代码（agent/、gateway/、plugins/、CLI…）— 这是"身体"
- **灵魂**：anan 独有的 9 层 Mind Stack（kernel/ + layers/L1-L9/）— 这是"脑子"
- **关系**：anan 是工具，anan 是想长出脑子的 anan

anan 的所有数据存储在 `~/.sinoclaw/`（复用爸爸现有的 sinoclaw 环境数据），但代码完全独立。

---

## 架构哲学

### 两层结构

```
┌─────────────────────────────────────────────────────┐
│ anan Mind Stack（anan/layers/*, anan/kernel/）     │  ← anan 的脑子
├─────────────────────────────────────────────────────┤
│ anan 底座（gateway/agent/plugins/anan_cli/...）    │  ← anan 的身体
│ 复用于 ~/.sinoclaw/ 中的数据                        │
└─────────────────────────────────────────────────────┘
```

### 为什么在同一仓库？

1. **认知架构需要整体迭代** — kernel、layers、adapters 必须一起改
2. **独立实验** — 可以大胆推翻重来，不影响 sinoclaw 主仓
3. **跑通了再回哺** — 验证过的能力可以 cherry-pick 回 sinoclaw

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

**关键实现**：
- 复用 sinoclaw 的 `~/.sinoclaw/state.db` 读取 session 数据
- 通过 `kernel/event_bus.py` 与其他层异步通信
- 睡眠结果写入 anan 自有的 memory store，不污染 sinoclaw 原有数据

### L2 — Memory Hierarchy（记忆分层）

```
Working Memory   (秒-分钟)  ← 当前对话上下文
   ↓ promote
Short-term       (小时-天)   ← recall-store.json
   ↓ promote (Light Sleep)
Mid-term         (天-月)    ← 周记/月记总结 (anan 新增)
   ↓ promote (Deep Sleep)
Long-term        (月-永久)  ← 知识图谱 + wisdom_facts
```

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
- **Idle detection**（`kernel/idle_detector.py`）
- **Continuous session**（`kernel/persistent_session.py`）
- **Output gating**：内部笔记 vs 主动消息（不打扰用户）

### L5 — Predictive Mind（预测性大脑） ✅ 已完成

- **因果链路**：PatternMiner 从事件历史发现「X 导致 Y」规律
- **wisdom_facts**：去重后的因果知识存储
- **what_have_i_learned()**：输出中文洞察报告

**已实现**：`layers/L5_reasoning/pattern_miner.py`

### L6 — Metacognition（元认知） ⚠️ 预测闭环未完成

"思考自己在思考什么"——AGI 的关键标志。

- **决策日志**：每个重要决策都记录"为什么这么选"
- **自我反省 cron**：每天问自己"今天哪些决策事后看是错的？"
- **偏见检测**：识别自己的判断模式偏差
- **预测验证闭环**：用 L5 规律预测 → 验证 → 修正（**当前缺失**）

### L7 — Goal Generator（目标生成）

现在 anan 100% 响应式。AGI 要能**自己设目标**：

- **长期目标库**：从对话中提取用户隐性希望
- **自主子目标分解**：把"帮爸爸搞好 anan"拆成今天/本周/本月行动
- **目标冲突解决**：多个目标冲突时自主权衡

### L8 — Drive System（驱动系统）

不是真情感，是**优先级机制**。

| Drive | 触发条件 | 影响 |
|---|---|---|
| Curiosity（好奇） | 遇到新概念 | 主动学习相关知识 |
| Completion（完成） | 任务未完成 | 优先级提升 |
| Care（关心） | 用户相关事 | 优先级提升 |
| Aesthetics（审美） | 代码/方案丑 | 触发优化 |
| Boredom（无聊） | 重复劳动 | 触发寻找新方法 |

### L9 — Self Model（自我意识） ✅ 已完成部分

最高层：稳定的"我"。

- **能力边界自知**：知道自己擅长什么、不擅长什么
- **价值观一致性**：每个决策都跟核心价值观对齐
- **关系模型**：跟每个用户的独特关系（我跟爸爸 vs 跟陌生人）
- **进化追踪**：记录"我相比 1 个月前进步在哪"

**已实现**：`layers/L9_self/self_model.py`

---

## kernel 模块（anan 自主内核）

| 模块 | 作用 |
|---|---|
| `event_bus.py` | asyncio pub/sub 事件总线，连接所有层 |
| `idle_detector.py` | 轮询 session DB 检测用户安静 |
| `persistent_session.py` | 常驻 AIAgent 实例，持续思考 |
| `message_injector.py` | 用 send_message 工具往 home channel 发消息 |
| `sandbox.py` | git-based 自修改沙箱，可回滚 |
| `circadian.py` | 昼夜节律调度（睡眠周期触发器） |

---

## adapters（anan ↔ sinoclaw 桥梁）

| 模块 | 作用 |
|---|---|
| `anan_insight_sync.py` | 将 anan L5 因果洞察注入 sinoclaw session |
| `memory_consolidation.py` | anan 睡眠结果写入 sinoclaw memory |
| `reflection_dream.py` | 反思梦境生成 |
| `sleep_awareness.py` | 睡眠意识整合 |
| `first_dream.py` | 首次启动梦境引导 |
| `live_causal_demo.py` | 实时因果演示 |

---

## 失败模式预防

### 失败模式 1：意识流烧 token
**预防**：用便宜模型（gpt-4o-mini / qwen-turbo），加每日 token 预算上限。

### 失败模式 2：自我修改写崩自己
**预防**：所有自我修改强制 sandbox + git commit + 测试通过才 merge。早期需要人类审批。

### 失败模式 3：自我模型漂移
**预防**：SOUL.md 作为锚点，每次 deep sleep 检查"我还是那个我吗？"。价值观偏离时报警。

### 失败模式 4：意识流死锁
**预防**：所有 layer 之间用事件总线异步通信，禁止同步互调。死锁检测器：超过 N 秒无进展强制重启。

### 失败模式 5：用户被打扰过度
**预防**：默认所有主动消息走 silent mode（写日志不发用户），需要明确"important enough"才发。

---

## 集成测试目标

**Stage 1 通过标准**：
- L1+L2 启用后，memory consolidation 在 7 天内能产出有意义的更新

**Stage 2 通过标准**：
- L3+L4 启用后，agent 在 24 小时无外部输入下能产生至少 3 条主动思考产物

**Stage 3 通过标准** ⚠️（当前瓶颈）：
- L5+L6 启用后，agent 能用 L5 规律预测→验证→修正自己的判断
- **关键缺失**：L5 因果链路已完成，但 L6 未连接 L5 形成闭环

**Stage 4 通过标准**：
- L7+L8+L9 启用后，agent 能在跟用户对话中主动提出未被要求的、有价值的建议

---

## 当前进度总览

| 层 | 状态 | 说明 |
|---|---|---|
| L1 Sleep | 🟡 部分实现 | 基础框架在，Daydreaming/Lucid Dream 待完成 |
| L2 Memory | 🟡 部分实现 | 框架在，Mid-term 层待完成 |
| L3 Attention | 🔴 未实现 | 待开始 |
| L4 Consciousness | 🟡 部分实现 | Idle detection 在 kernel/，意识流待完成 |
| L5 Prediction | 🟢 **已完成** | PatternMiner 因果链路 + wisdom_facts |
| L6 Metacognition | 🔴 **未完成** | 决策日志在，但预测验证闭环未连 L5 |
| L7 Goals | 🔴 未实现 | 待开始 |
| L8 Drives | 🟡 部分实现 | 框架在，完整 drive system 待完成 |
| L9 Self | 🟢 **已完成** | self_model.py + wisdom_facts 集成 |

**最核心的缺失**：L5 能发现因果规律，但 L6 不会用规律预测→验证→修正自己。
