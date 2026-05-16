"""
L4 — Stream of Consciousness（意识流）
=======================================

> 持续意识流：用户没说话时也在思考

升级自 OpenClaw `heartbeat` 插件，从"定时心跳"演进为"idle 触发的持续意识流"。

子模块：
  consciousness.py — 核心实现
    IdleDetector       — 检测用户是否 idle
    ThoughtStream      — 思考的短期记忆（最近 N 条）
    OutputGate         — 内部笔记 vs 主动推送决策
    ConsciousnessEngine — 编排以上三者

事件订阅：
  L4.idle.started       — 用户进入 idle 状态
  L4.idle.ended         — 用户恢复活动
  L4.thought.generated  — 产生了新想法
  L4.thought.pushed     — 想法推送给了用户（rare）
  L8.drive.suggestion   — 来自 L8 的驱动力建议

状态：✅ 主体完成（Phase 1）
"""

from layers.L4_consciousness.consciousness import (
    ConsciousnessEngine,
    IdleDetector,
    IdleThoughtEngine,
    OutputGate,
    Thought,
    ThoughtImportance,
    ThoughtStream,
    ThoughtType,
)

__all__ = [
    "ConsciousnessEngine",
    "IdleDetector",
    "IdleThoughtEngine",
    "OutputGate",
    "Thought",
    "ThoughtImportance",
    "ThoughtStream",
    "ThoughtType",
]
