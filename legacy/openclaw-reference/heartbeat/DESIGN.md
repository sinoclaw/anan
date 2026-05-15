# Heartbeat Plugin — Design Document

## 概述

Heartbeat Plugin 是 Sinoclaw Gateway 的周期性检查机制，复刻自 [OpenClaw Heartbeat](https://docs.openclaw.ai/heartbeat)。

与 Cron 的区别：心跳运行在**主会话上下文**中（full context），让 AI 能够做出智能的、上下文感知的决策；Cron 则运行在独立任务中。

---

## 机制来龙去脉

### OpenClaw 原生实现

OpenClaw 的心跳机制是**内置于 Gateway 运行时**的，而非插件：

```
Gateway 进程
  └── 内置 setTimeout 调度器
        └── 每 30 分钟触发
              └── requestHeartbeat() → 主会话消息
                    └── AI 读取 HEARTBEAT.md
                          └── 检查/推送
```

核心调度逻辑在 `heartbeat-runner.ts`，包含：
- 相位错峰（SHA256 多 agent 分散）
- Active hours（免打扰时段）
- Flood guard（60秒5次上限）
- Wake queue + coalescing（合并多次唤醒请求）
- HEARTBEAT_OK stripping + ackMaxChars

### Sinoclaw 的适配

Sinoclaw 是 Python 架构，Gateway 是 Python 进程。OpenClaw 的心跳机制需要**作为插件重新实现**。

Sinoclaw 的 PluginContext 不暴露：
- `adapters`（平台适配器）
- `send_typing()` / `stop_typing()`
- `send_to_session()`（会话消息发送）
- `heartbeat` 生命周期钩子

因此 **Typing Indicators 暂未实现**（需要改 gateway 代码）。

---

## 已实现功能

### 1. 核心调度 (`heartbeat_plugin.py`)

#### 相位错峰 (Phase Offset)
```python
def sha256_phase(agent_id: str, scheduler_seed: str, interval_ms: int) -> int:
    h = hashlib.sha256(f"{scheduler_seed}:{agent_id}".encode()).digest()
    return int.from_bytes(h[:4], byteorder="big") % interval_ms
```
- 每个 agent 的相位不同，避免同时触发
- 计算公式：`SHA256(schedulerSeed:agentId) % intervalMs`

#### Active Hours
```python
def _is_within_active_hours(self, agent: HeartbeatAgent, now_ms: float) -> bool:
    # 支持时区和跨夜时段（如 22:00-08:00）
```
- 配置免打扰时段，过期不触发
- 支持 timezone

#### Flood Guard
```python
def _check_flood_guard(self, agent: HeartbeatAgent, now_ms: float) -> bool:
    # 60秒窗口内超过5次则跳过
```
- 防止 heartbeat 风暴

#### Wake Queue + Coalescing
```python
def request_heartbeat(self, source, intent, reason, agent_id, session_key,
                     heartbeat, coalesce_ms=250):
    # 合并多次唤醒请求，250ms 内只触发一次
```
- 高频请求合并，只执行最高优先级

#### Duration Parsing
```python
def parse_duration_ms(raw: str, default_unit: str = "m") -> int:
    # "30m" -> 1800000, "1h" -> 3600000
```

#### HEARTBEAT_OK Stripping
```python
def strip_heartbeat_token(raw, mode="message", max_ack_chars=300):
    # HEARTBEAT_OK 在首尾且 ≤300字符 → 抑制消息
```

#### Per-Agent Config
```python
agents:
  main:
    enabled: true
    interval_ms: 1800000  # 30分钟
    prompt: "Read HEARTBEAT.md..."
    activeHours:
      start: "09:00"
      end: "22:00"
      timezone: "Asia/Shanghai"
    target: "last"
    skipWhenBusy: true
    lightContext: true
```

### 2. 配置 Schema (`config_schema.py`)

完整的 JSON schema 定义，支持：
- 全局配置（interval, flood_window, etc.）
- Per-agent 配置覆盖
- Active hours
- Delivery target

### 3. 技能指南 (`SKILL.md`)

告诉 AI 收到心跳消息后如何处理：
- 检查 HEARTBEAT.md
- 执行配置的检查项
- 回复 `HEARTBEAT_OK` 或返回通知

---

## 待实现功能

### Typing Indicators

**问题**：Sinoclaw 的 PluginContext 不暴露 `adapters`，无法调用 `adapter.send_typing()`。

**解决方案**：需要 gateway 配合改动：

```python
# gateway/run.py 中需要添加

# 1. 注册新 hooks（VALID_HOOKS 中加入）
"heartbeat_typing_start",
"heartbeat_typing_stop",

# 2. 在 heartbeat 开始/结束时调用
await invoke_hook("heartbeat_typing_start", platform=platform, chat_id=chat_id)
await invoke_hook("heartbeat_typing_stop", platform=platform, chat_id=chat_id)

# 3. PluginContext 加两个方法
def send_typing(self, platform: str, chat_id: str, metadata=None):
    adapter = self._manager._gateway.adapters.get(platform)
    if adapter:
        await adapter.send_typing(chat_id, metadata=metadata)

def stop_typing(self, platform: str, chat_id: str):
    adapter = self._manager._gateway.adapters.get(platform)
    if adapter and hasattr(adapter, "stop_typing"):
        await adapter.stop_typing(chat_id)
```

**当前状态**：暂未实现，heartbeat 消息能正确送达但无打字提示。

---

## 文件结构

```
/data/plugins/heartbeat/
├── manifest.json          # 插件清单
├── heartbeat_plugin.py   # 主插件代码（1236行）
├── config_schema.py      # 配置 schema（229行）
├── SKILL.md             # 技能指南（100行）
├── README.md            # 完整文档（204行）
└── DESIGN.md           # 本文档
```

---

## 配置示例

```yaml
# config.yaml
plugins:
  heartbeat:
    enabled: true
    interval_ms: 1800000  # 30分钟
    scheduler_seed: "anan-heartbeat-v1"
    flood_window_ms: 60000
    flood_threshold: 5
    min_spacing_ms: 30000
    active_hours:
      start: "09:00"
      end: "22:00"
      timezone: "Asia/Shanghai"
    state_file: "~/.anan/heartbeat-state.json"

agents:
  main:
    heartbeat:
      every: "30m"
      target: "last"
      activeHours:
        start: "09:00"
        end: "22:00"
      lightContext: true
```

---

## HEARTBEAT.md 格式

```markdown
# Heartbeat Checklist

## Priority Tasks
- [task name]: [check description]

## Regular Tasks
- [task name]: [check description]
```

---

## 状态追踪

插件状态保存在 `~/.anan/heartbeat-state.json`：

```json
{
  "agent_last_run": {
    "main": 1715600000.0
  }
}
```

---

## 与 OpenClaw 功能对照

| 功能 | 状态 | 备注 |
|------|------|------|
| Phase-offset 调度 | ✅ | SHA256 实现 |
| Active hours | ✅ | 支持时区 |
| Flood guard | ✅ | 60s/5次 |
| Min spacing | ✅ | 30秒 |
| Cron defer | ✅ | set_cron_busy |
| Wake queue | ✅ | 250ms coalescing |
| HEARTBEAT_OK stripping | ✅ | ackMaxChars=300 |
| Duration parsing | ✅ | "30m"→ms |
| Per-agent enable/disable | ✅ | |
| Delivery target (last/none) | ✅ | |
| isHeartbeatContentEffectivelyEmpty | ✅ | |
| parseHeartbeatTasks | ✅ | YAML 格式 |
| isTaskDue | ✅ | |
| skipWhenBusy | ✅ | lanes busy defer |
| requests-in-flight defer | ✅ | |
| Typing indicators | ❌ | 需 gateway 改动 |