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
EVENTS = ["gateway:startup", "gateway:shutdown", "agent:start"]

# 全局 runner 引用，避免被 GC
_mind_stack_runner = None


# 全局格式化后的心智上下文（被 agent:start 填充，run.py 读取）
_mind_stack_context: str = ""


def handle(event_type: str, context: dict) -> None:
    """
    Gateway hook handler.

    Events:
        gateway:startup — 启动九层 Mind Stack
        gateway:shutdown — 停止九层 Mind Stack（如果 gateway 被关闭）
        agent:start — 注入九层产出到 LLM prompt
    """
    global _mind_stack_runner, _mind_stack_context

    if event_type == "gateway:startup":
        _start_mind_stack(context)
    elif event_type == "gateway:shutdown":
        _stop_mind_stack()
    elif event_type == "agent:start":
        _inject_cognition_to_agent(context)


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


def _inject_cognition_to_agent(context: dict) -> None:
    """
    agent:start 钩子：读取九层产出，格式化成 prompt 片段，写入 _mind_stack_context。
    run.py 在构建 context_prompt 时会追加这段内容。
    """
    global _mind_stack_context

    try:
        from kernel.mind_stack_runner import get_last_cognition
        cognition = get_last_cognition()
    except Exception as exc:
        logger.debug("Could not get mind cognition: %s", exc)
        cognition = {}

    if not cognition.get("has_thought"):
        _mind_stack_context = ""
        return

    # 格式化九层产出为 prompt 片段
    parts = ["[九层认知产出]"]

    # 洞察
    insights = cognition.get("insights", [])
    if insights:
        parts.append(f"最近发现的规律：{'; '.join(insights[:3])}")

    # 驱动
    drives = cognition.get("drives", [])
    if drives:
        drive_strs = [f"{d['drive']}({d['strength']:.2f})" for d in drives]
        parts.append(f"当前内在驱动：{', '.join(drive_strs)}")

    # 自我认知
    sm = cognition.get("self_model", {})
    if sm.get("who"):
        parts.append(f"自我认知：{sm['who']}")
    if sm.get("learned"):
        learned = sm["learned"]
        if isinstance(learned, str) and learned:
            parts.append(f"最近学到：{learned[:100]}")

    # 元认知
    mc = cognition.get("metacognition", {})
    if mc.get("latest_report"):
        parts.append(f"元认知：{mc['latest_report']}")

    # 记忆
    mem = cognition.get("memory", {})
    recent = mem.get("recent", [])
    if recent:
        parts.append(f"最近记忆：{' | '.join(str(m) for m in recent[-2:])}")

    if len(parts) == 1:
        _mind_stack_context = ""
        return

    _mind_stack_context = "\n".join(parts)


# 供 run.py 读取已格式化的心智上下文
def get_mind_stack_context() -> str:
    """返回格式化的九层产出 prompt 片段。"""
    return _mind_stack_context


def _expose_to_hooks(runner) -> None:
    """把 runner 暴露到 hooks 系统，供其他 hook 或调试使用。"""
    try:
        from gateway import hooks as _hooks_module
        _hooks_module._mind_stack_runner = runner
    except Exception:
        pass

    # 同样暴露 get_last_cognition（九层产出读取入口）
    try:
        from kernel.mind_stack_runner import get_last_cognition as _get_last_cognition
        _hooks_module._get_mind_cognition = _get_last_cognition
    except Exception:
        pass


# 暴露给 hooks 系统
def get_mind_stack_runner():
    """供其他模块获取当前 MindStackRunner 实例。"""
    return _mind_stack_runner
