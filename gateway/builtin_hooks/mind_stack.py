"""
MindStack Builtin Hook — gateway/builtin_hooks/mind_stack.py
===========================================================

Gateway 启动时自动加载的 builtin hook。
在 gateway:startup 时启动 MindStackRunner，九层 Mind Stack 开始在后台运转。

Usage:
    无需配置。gateway 启动时自动加载并启动九层。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import os

logger = logging.getLogger("gateway.builtin.mind_stack")

# 声明本 hook 处理的 events（builtin_hooks 系统会读取这个）
EVENTS = ["gateway:startup", "gateway:shutdown"]

# 全局 runner 引用，避免被 GC
_mind_stack_runner = None


def handle(event_type: str, context: dict) -> None:
    """
    Gateway hook handler。

    Events:
        gateway:startup — 启动九层 Mind Stack
        gateway:shutdown — 停止九层 Mind Stack（如果 gateway 被关闭）
    """
    global _mind_stack_runner

    if event_type == "gateway:startup":
        _start_mind_stack(context)
    elif event_type == "gateway:shutdown":
        _stop_mind_stack()


def _start_mind_stack(context: dict) -> None:
    """在 gateway 启动时异步启动 MindStackRunner。"""
    global _mind_stack_runner
    if _mind_stack_runner is not None and _mind_stack_runner.is_running:
        logger.info("MindStack already running, skipping")
        return

    try:
        from kernel.mind_stack_runner import MindStackRunner
        from kernel.circadian import CircadianConfig
    except Exception as exc:
        logger.error("Failed to import MindStackRunner: %s", exc)
        return

    config = CircadianConfig(
        tick_interval_s=30.0,   # 每30秒一次心跳（idle 场景）
        fatigue_per_tick=0.5,     # 每次心跳累积0.5疲劳值
        sleep_threshold=10.0,   # 20分钟idle后进入睡眠
        # idle_detector 会覆盖 tick_interval，当用户活跃时加速
    )

    _mind_stack_runner = MindStackRunner(
        circadian_config=config,
        gateway_events=True,
    )

    # 在新任务里启动，不阻塞 gateway 启动流程
    async def _start():
        try:
            await _mind_stack_runner.start()
            logger.info("MindStackRunner started successfully")
            # 把 runner 暴露给 hooks 系统，供外部调试
            _expose_to_hooks(_mind_stack_runner)
        except Exception as exc:
            logger.error("MindStackRunner start failed: %s", exc)

    asyncio.create_task(_start())


def _stop_mind_stack() -> None:
    """在 gateway 关闭时停止 MindStackRunner。"""
    global _mind_stack_runner
    if _mind_stack_runner is None:
        return
    try:
        asyncio.create_task(_mind_stack_runner.stop())
    except Exception as exc:
        logger.error("MindStackRunner stop failed: %s", exc)


def _expose_to_hooks(runner) -> None:
    """把 runner 暴露到 hooks 系统，供其他 hook 或调试使用。"""
    try:
        from gateway import hooks as _hooks_module
        _hooks_module._mind_stack_runner = runner
    except Exception:
        pass


# 暴露给 hooks 系统
def get_mind_stack_runner():
    """供其他模块获取当前 MindStackRunner 实例。"""
    return _mind_stack_runner
