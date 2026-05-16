# Event Loop Safety — 不要阻塞事件循环

> **教训时间**：2026-05-16，QQ 消息 `接下来干啥` 在 03:23:00 发出后杳无音讯。根因：PatternMiner.mine_now() 同步 O(n²) 无 await + Predictor 订阅 `"*"` 反馈循环 → CPU 100% 事件循环饥饿，消息卡在 `After processing` 后直接被静默丢弃。修复花了一天。

---

## 核心原则

**asyncio 是 cooperative multitasking。** 你的协程必须定期 `await` 才能让其他协程有机会运行。如果你的代码路径上没有 `await`，它就会霸占事件循环，直到完成。

---

## 不要这样做

### 1. 同步循环里没有 await

```python
# 坏：O(n²) 循环，没有任何 await
for i, x in enumerate(topics):
    for j in range(i + 1, min(i + 1 + self._window, len(topics))):
        y = topics[j]
        # ... 密集计算

# 好：每 N 次迭代 yield 一次
for i, x in enumerate(topics):
    # ...
    if i % 10 == 0:   # 不是 100，是 10
        await asyncio.sleep(0)
```

### 2. fire-and-forget 的 asyncio.create_task 没有错误处理

```python
# 坏：异常会被 asyncio 吞掉，但更重要的是它会传播到 gather
asyncio.create_task(self.mine_now())  # 如果 mine_now 抛异常...

# 好：包装在 try/except 里
task = asyncio.create_task(self.mine_now())
task.add_done_callback(
    lambda t: logger.debug("done: %s", t.result() if t.done() and not t.cancelled() else "cancelled")
)
```

更好的做法是用专门的异常捕获包装：

```python
async def mine_now(self) -> list[Pattern]:
    try:
        return await self._mine_now_impl()
    except Exception as exc:
        logger.debug("L5 mine_now failed (non-fatal): %s", exc)
        return []   # ← 关键：永远不向调用者抛异常
```

### 3. 订阅 "*/**" 的 handler 里同步发布

```python
# 坏：Predictor 订阅 "*"，收到每个事件都 _emit_predictions_for，
# 里头的 _async_publish → 新事件 → on_any 回调 → 死循环
self._bus.subscribe("*", on_any)  # on_any 里又有 await _async_publish

# 好：加 re-entrancy guard
if getattr(self, "_in_on_event", False):
    return
self._in_on_event = True
try:
    await self._emit_predictions_for(topic, now)
finally:
    self._in_on_event = False
```

### 4. 对高频事件没有全局节流

```python
# 坏：每个事件都处理，session replay 注入几千个事件直接打爆
async def on_any(event: Event):
    await self._on_event(event)

# 好：100ms 最多处理一次
if now - self._last_on_event_time < self._on_event_throttle_s:
    return
```

---

## 为什么这些错误很隐蔽

1. **日志里有 `After processing`，但没有 `response ready`** — 消息被适配器接收了，但事件循环没有机会把它分发给 agent 处理
2. **CPU 100% 但 gateway 没有冻住** — 事件循环在转，只是转的是 miner/predictor，不是消息处理
3. **单独测试各模块都正常** — 只有在 session replay + gateway 一起跑时才暴露

---

## 验证方法

```bash
# 1. 检查事件循环是否阻塞
# 在 gateway 运行时用另一个终端：
ps aux | grep python
top -p <pid>   # 看 CPU 是否长期 100%

# 2. 检查事件分发
# 观察 inbound message 和 response ready 的数量是否匹配
grep "inbound message\|response ready" ~/.anan/logs/gateway.log | tail -20

# 3. 检查 Predictor 是否反馈循环
grep "PRED-LINK-INVOKED\|PRED-DEBUG" ~/.anan/logs/gateway.log | tail -20

# 4. 测试事件循环响应速度
# 在 gateway 运行期间发一条消息，预期 response ready 在 15s 内出现
# 超过 30s 说明事件循环受阻
```

---

## 相关文件

- `layers/L5_reasoning/pattern_miner.py` — PatternMiner.mine_now()
- `layers/L5_prediction/predictor.py` — PredictiveReasoner._on_event()
- `kernel/event_bus.py` — EventBus.publish() 和它的 gather(return_exceptions=False)
