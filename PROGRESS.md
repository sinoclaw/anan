# anan 项目进度

> 最后更新：2026-05-17 05:25 (commit 963d453)

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

### L3 — AttentionQueue boost() + 抢占机制 ✅
- **链路**：L8.drive.updated → AttentionBridge._on_drive_updated → AttentionQueue.boost() → 发布 L3.attention.boosted
- **旧问题**：boost() 只提升分数，无实际抢占动作
- **修复**：添加 `_on_attention_boosted` handler（commit `65a2bf7`）——当 boosted 项分数比当前 focus 高 0.15 以上时，调用 `_preempt_to()` 强制切换
- **commit**：`65a2bf7`
- **验证**：待下次 L0 tick 触发 drive.updated 验证

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

### L6 — SelfTuner L5→L6 闭环 ✅ 2026-05-17
- **修复前**：L5 发现因果规律后没有任何后续动作，规律发现 = 终点
- **修复**：`SelfTuner` 新增订阅 `L5.pattern.discovered`（commit `963d453`）
  - lift > 8：提升对应链路的 `probability_boost`（+0.3，封顶 3.0）
  - lift > 12：降低 `min_lift`（从当前值 -0.2，下限 1.3），让 L5 更激进挖掘
  - actions 进入 `pending` 队列 → 60s auto_approve → `_apply()` 执行
- **commit**：`963d453`
- **意义**：L5 因果规律终于有了消费方——L6 根据规律强度主动调参，形成 **发现→行动→验证** 闭环

### L7 — LLM-driven 目标生成 ✅
- **旧问题**：`_on_circadian_tick` 用硬编码 goal（"保持好奇"），从未调用 LLM 生成
- **修复**：改为真实 context 驱动（active goals + pending actions + wisdom facts）→ LLM 生成有依据的目标（commit `479e4c1`）
- **Bug 修复**：`GoalGenerator.__init__` 中 `_pending` 未定义就被引用，修复为初始化空列表（commit `e76e3ce`）
- **context 素材**：活跃目标列表、待审批调参动作、PatternMiner wisdom_facts
- **commit**：`479e4c1`（真实系统状态 + LLM）+ `65a2bf7`（L3 抢占）+ `e76e3ce`（_pending 初始化）
- **验证**：待下次 L0 tick 触发 L7 goal generation

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

|| commit | 内容 |
|--------|------|
| 963d453 | fix L6: subscribe L5.pattern.discovered to close L5→L6 metacognition loop |
| e76e3ce | fix L7: initialize _pending in GoalGenerator.__init__ |
| b38ec43 | fix: increase tick_interval to 600s (10min) |
| 3e01d4f | fix: increase CircadianLoop tick_interval from 10s to 300s (save token costs) |
| 65a2bf7 | fix L7: real system-state goal gen; fix L3: real preemption on boost |
| 479e4c1 | fix L7: use real system state + LLM for goal generation on circadian tick |
| fd254ff | fix test: Anan Insights brand fixture |
| d039069 | feat P1: PatternMiner discovered patterns → MemoryTier persistence |
| a85f790 | fix P0: pm.discovered() call with parens + keep WorkingMemory lock |
| 1946607 | fix L4: replace question fallback with reflective narrative |
| 2383685 | fix L2: publish L1.daydream.ended for promote |
| 4d10aab | fix L1: skip tool role in session ingestion |
| 8470827 | fix L4: print → logger.warning() |
| b4f1082 | fix L5: layer.discovered() + remove duplicate mine_now() |

---

## anan 运行时状态

- **PID**：497713
- **启动时间**：04:49
- **当前状态**：idle=True（无聊天输入超过 120s）
- **L4 loop**：每 10s 正常执行
- **DREAMS.md**：最后更新 03:20，内容干净
- **recall-store**：77 条 entries
- **MindStack**：18 个层启动完成
