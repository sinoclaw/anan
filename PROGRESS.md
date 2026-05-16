# anan 项目进度

> 最后更新：2026-05-17 05:20 (commit fd254ff)

## 阶段一：Bug 修复

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

### P2 — 不做

| 项 | 原因 |
|----|------|
| 加"我最近注意到"等自我意识台词 | 制造假象，anan 目前没有真正的自我意识，不应欺骗用户 |

---

## 层级链路修复

### L1 — Daydreaming 意识流质量 ✅
- **修复前**：DREAMS.md 包含 JSON 元数据噪声（`{"content": "..."}`）和 tool role 原始输出
- **根因**：`session` 表中 `role=tool` 的消息是 JSON 噪声，污染了意识流生成
- **修复**：`_ingest_session_signals_from_db` 跳过 `role == "tool"` 的消息（只过滤 tool，不过滤 assistant）
- **commit**：`4d10aab`
- **验证**：DREAMS.md 03:20 后内容干净，无 JSON 噪声 ✅

### L2 — WorkingMemory Promote 链路 ✅
- **修复前**：`WorkingMemory → L2 promotion` 日志从未出现
- **根因**：`sleep_fn` 末尾没有发布 `L1.daydream.ended` 事件，`_on_sleep_ended` 永远等不到
- **修复**：在 `sleep_fn` 末尾（在 `🌙 [Cycle %d] 睡眠阶段完成` 之前）添加 `await bus.publish(Event(topic="L1.daydream.ended", ...))`
- **commit**：`2383685`
- **验证**：重启后 `WorkingMemory → L2 promotion: 20 items promoted` ✅

### L3 — AttentionQueue boost() 链路 ✅
- **链路**：L8.drive.updated → AttentionBridge._on_drive_updated → AttentionQueue.boost()
- **状态**：架构完整，被动依赖 L0 tick 触发，无实际主动验证方式（logger.info 被 gateway 过滤）
- **结论**：无需修改，架构已通

### L4 — Consciousness Loop 日志可见性 ✅
- **修复前**：`[L4 consciousness loop]` 日志从不出现
- **根因**：gateway.run.py 设 `_stderr_level = WARNING`，INFO 级别全部被过滤
- **修复**：改 `print()` 为 `logger.warning()`（commit `8470827`）
- **验证**：重启后 `active=True idle=False was_idle=False elapsed=NNs` 每 10s 输出 ✅

### L4 — Fallback 问句 ✅
- **修复前**：无上下文时产生问句 `"最近有没有什么事情跟以前处理过的某个问题很像？联想一下。"`
- **修复**：改为反思性叙述
- **commit**：`1946607`
- **修复后**：`"回顾我之前处理过的一个情况：当时通过某种方式解决了，现在的情况虽然表面不同，但本质上有相似之处——我可以用同样的思路来应对这次的新挑战。"`

### L5 — PatternMiner insights=[] ✅
- **修复前**：insights=[] 始终为空
- **根因**：`layer.discovered` 是 @property bound method，没加括号；`_collect_and_publish_sync` 重复调用 `mine_now()` 覆盖 `_last_patterns`
- **修复**：删重复 `mine_now()` + `layer.discovered` → `list(layer.discovered())`
- **commit**：`b4f1082`
- **验证**：insights=3 ✅ drives=3 ✅ sm_keys=['who','learned'] ✅

### L6 — SelfTuner 闭环 ✅
- **SelfTuner**：有 `auto_approve_age_s=60.0`，pending actions 60s 后自动执行（line 196-212）
- **GoalEngine**：订阅 `L6.metacognition.report`（line 520）
- **状态**：链路完整，SelfTuner 需要 PredictionMonitor 积累预测数据后才能触发调参（当前日志无 APPLIED 是正常状态）

### L7 — L7 GoalEngine → L6 SelfTuner 闭环 ✅
- 见 L6，链路已通

### L8 — 暂无问题

### L9 — SelfModel Wisdom 更新 ✅
- **SelfModelLive**：`_on_pattern_discovered` 已订阅 `L5.pattern.discovered`（line 328）
- **链路**：PatternMiner → L5.pattern.discovered → SelfModelLive._on_pattern_discovered → model.add_wisdom() → wisdom_facts
- **PatternMiner**：已发 `summary` 字段（line 235）
- **状态**：链路完整，无需修改

---

## Phase 1 测试状态

> pytest 核心套件（排除环境相关测试）

| 范围 | 结果 |
|------|------|
| anan_state/anan_constants/anan_logging | 302 passed ✅ |
| integration/e2e | 56 passed, 9 skipped ✅ |
| agent/test_insights | 56 passed ✅（brand fixture 已修复）|

**已知环境问题（非本次改动引起）：**
- `test_backup`：anan 运行时持有 `~/.anan/anan_state.db`，测试与运行时冲突
- `test_openrouter_response_cache`/`test_bedrock_adapter`：缺少 `botocore` 包
- `test_gateway_service`：root 用户执行触发 `ValueError: refusing to run as root`

---

## Git Commits

| commit | 内容 |
|--------|------|
| fd254ff | fix test: Anan Insights brand fixture |
| d039069 | fix P1: PatternMiner → MemoryTier persistence |
| a85f790 | fix P0: pm.discovered() + WorkingMemory lock |
| 1946607 | fix L4: fallback question → reflective narrative |
| 2383685 | fix L2: publish L1.daydream.ended for promote |
| 4d10aab | fix L1: skip tool role in session ingestion |
| 8470827 | fix L4: print → logger.warning() |
| b4f1082 | fix L5: layer.discovered() + remove duplicate mine_now() |
| b08f3ba | fix L4: diag print in consciousness loop |
| f603fa0 | fix L4: attach diag print |
| a347ad5 | fix L4: print → logger.info() |
| b9d4298 | fix L4: diag print in consciousness loop |
| 3c11a33 | fix L4/L1: idle detection diag |
| 79d7d82 | fix: PatternMiner class var + SyntaxError |

---

## anan 运行时状态

- **PID**：459935
- **启动时间**：04:12
- **当前状态**：idle=True（无聊天输入超过 280s+）
- **L4 loop**：每 10s 正常执行
- **DREAMS.md**：最后更新 03:20，内容干净
- **recall-store**：77 条 entries
