"""
L7 Goals — 目标生成系统
=========================

anan 从 100% 响应式进化为主动设目标的层次。

设计：
  - 长期目标库：从对话中提取用户的隐性希望，转成 anan 的目标
  - 自主子目标分解：把"帮爸爸搞好 anan"拆成今天/本周/本月行动
  - 目标冲突解决：多个目标冲突时自主权衡
  - 机会识别：发现"现在是做 X 的好时机"主动行动

事件：
  L7.goal.proposed     — 新目标提出
  L7.goal.decomposed   — 目标被分解成子目标
  L7.goal.achieved     — 目标完成
  L7.goal.abandoned   — 目标放弃
  L7.goal.conflict     — 检测到目标冲突
  L7.goal.opportunity  — 发现机会窗口

订阅：L6.metacognition.report / L9.self.updated
发布：L8.intent（升格为持续渴望）
"""

from layers.L7_goals.goal_engine import (
    Goal,
    GoalStatus,
    GoalGenerator,
)

__all__ = ["Goal", "GoalStatus", "GoalGenerator"]
