"""
L8 Drives — 驱动力系统
=======================

不是真情感，是优先级机制。五种内驱力影响所有注意力决策：

| Drive       | 触发条件         | 影响                        |
|-------------|------------------|-----------------------------|
| Curiosity   | 遇到新概念        | 主动学习相关知识            |
| Completion  | 任务未完成        | 优先级提升                  |
| Care        | 用户相关事         | 优先级提升                  |
| Aesthetics  | 代码/方案丑        | 触发优化                    |
| Boredom     | 重复劳动          | 触发寻找新方法              |

事件：
  L8.drive.satisfied  — 驱动力被满足
  L8.drive.active     — 驱动力激活
  L8.drive.dormant    — 驱动力沉寂

订阅：L3.attention.* / L7.goal.*
发布：L8.intent（驱动意图）
"""

from layers.L8_drives.drive_system import (
    Drive,
    DriveType,
    DriveSystem,
)

__all__ = ["Drive", "DriveType", "DriveSystem"]
