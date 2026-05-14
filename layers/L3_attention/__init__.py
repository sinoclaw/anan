"""
L3 Attention System — 注意力调度
=================================

anan 的注意力门卫：决定什么值得思考，什么可以忽略。

设计：
  - 三维评分：urgency(紧急) / importance(重要) / interest(兴趣)
  - score = 0.5*urgency + 0.3*importance + 0.2*interest
  - 抢占机制：高优先级事件打断低优先级思考
  - 聚焦模式：长任务时主动屏蔽低相关消息
  - 走神检测：注意力集中度持续走低时触发 Daydreaming

事件：
  L3.attention.queued   — 新任务进入注意力队列
  L3.attention.focus     — 注意力聚焦到某个任务
  L3.attention.shift     — 注意力转移
  L3.attention.dropped   — 低优先级任务被丢弃
  L3.attention.vigilance_low — 走神检测触发（触发 Daydreaming）

发布者：L4_consciousness / L7_will / L8_drives
订阅者：L4_proactive（Probe）
"""

from layers.L3_attention.attention import (
    AttentionQueue,
    AttentionItem,
    AttentionScore,
    PreemptiveMode,
    VigilanceMonitor,
)

__all__ = [
    "AttentionQueue",
    "AttentionItem",
    "AttentionScore",
    "PreemptiveMode",
    "VigilanceMonitor",
]
