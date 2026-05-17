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
EVENTS = ["gateway:startup", "gateway:shutdown", "agent:start", "agent:end"]

# 全局 runner 引用，避免被 GC
_mind_stack_runner = None
_runtime_handle = None


# ── MinimalRuntimeHandle ────────────────────────────────────────────────────
# A duck-type "parent agent" that satisfies delegate_task's getattr() calls.
# Allows MindStackRunner's async advisors to spawn real subagents via
# delegate_task without needing a full AIAgent instance.
#
# Required attributes (all getattr-safe, default to None):
#   _delegate_depth, _subagent_id, valid_tool_names, enabled_toolsets,
#   terminal_cwd, cwd, _delegate_spinner, tool_progress_callback
# ─────────────────────────────────────────────────────────────────────────────
class MinimalRuntimeHandle:
    """Minimal parent-agent handle for delegate_task in async contexts."""

    def __init__(self):
        # delegate_task reads these via getattr(x, attr, None)
        self._delegate_depth = 0
        self._subagent_id = None
        self.valid_tool_names = []          # empty = all tools allowed
        self.enabled_toolsets = None         # None = all toolsets enabled
        self.terminal_cwd = "/data/anan"
        self.cwd = "/data/anan"
        self._delegate_spinner = None
        self.tool_progress_callback = None
        self._subdirectory_hints = None

    async def _delegate_async(self, **kwargs) -> str:
        """Async wrapper: run sync delegate_task in a thread pool."""
        import concurrent.futures
        from tools.delegate_tool import delegate_task
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = await loop.run_in_executor(
                pool, lambda: delegate_task(parent_agent=self, **kwargs)
            )
        return result

    def delegate_task(self, **kwargs) -> str:
        """Sync delegate_task — call this from async code via _delegate_async."""
        raise RuntimeError("call _delegate_async() not delegate_task() directly")


# ── 全局格式化后的心智上下文（被 agent:start 填充，run.py 读取）
_mind_stack_context: str = ""

# agent:end 时需要配对的 context（上一轮 agent:start 的 message）
_last_agent_message: str = ""


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
    elif event_type == "agent:end":
        _on_agent_end_for_cognition(context)


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

    global _runtime_handle
    _runtime_handle = MinimalRuntimeHandle()

    config = CircadianConfig(
        tick_interval_s=30.0,   # 每30秒一次心跳（idle 场景）
        fatigue_per_tick=0.5,     # 每次心跳累积0.5疲劳值
        sleep_threshold=10.0,   # 20分钟idle后进入睡眠
        # idle_detector 会覆盖 tick_interval，当用户活跃时加速
    )

    _mind_stack_runner = MindStackRunner(
        circadian_config=config,
        gateway_events=True,
        runtime_handle=_runtime_handle,
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
    run.py 在构建 context_prompt 时把这段内容放到 system prompt 最前面（而非末尾）。
    """
    global _mind_stack_context, _last_agent_message
    # 保存用户消息，供 agent:end 时配对使用
    _last_agent_message = context.get("message", "") or ""

    try:
        from kernel.mind_stack_runner import get_last_cognition
        cognition = get_last_cognition()
    except Exception as exc:
        logger.debug("Could not get mind cognition: %s", exc)
        cognition = {}

    if not cognition.get("has_thought"):
        _mind_stack_context = ""
        return

    # 格式化九层产出为结构化文本
    parts = []

    # 洞察
    insights = cognition.get("insights", [])
    if insights:
        parts.append(f"最近规律：{'；'.join(insights[:3])}")

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

    # L5 预测状态
    pred = cognition.get("prediction", {})
    if pred:
        acc = pred.get("accuracy", 0.0)
        links = pred.get("links", 0)
        pending = pred.get("pending", 0)
        confirmed = pred.get("confirmed", 0)
        top_pending = pred.get("top_pending", [])
        parts.append(
            f"预测链路：共{links}条，命中{confirmed}次，失败{pred.get('failed', 0)}次"
            f"，准确率{acc:.0%}，待预测{pending}个"
            + (f"，最新：{'、'.join(top_pending)}" if top_pending else "")
        )

    # L6 调参状态
    tuning = cognition.get("tuning", {})
    if tuning:
        pending = tuning.get("pending", 0)
        applied = tuning.get("applied", 0)
        if pending > 0:
            parts.append(f"调参中：{pending}个待审批参数调整（已应用{applied}次）")

    if not parts:
        _mind_stack_context = ""
        return

    # 格式化为易读的分块，放在 system prompt 最前面
    header = "「九层认知状态」（以下内容来自anan内部认知系统，请作为重要背景参考）"
    _mind_stack_context = header + "\n" + "\n".join(f"• {p}" for p in parts)


def _on_agent_end_for_cognition(context: dict) -> None:
    """
    agent:end 钩子（同步阻塞）：收到 AI 回复后，立即触发 PatternMiner 挖掘，
    然后立刻收集九层产出写入 _last_cognition。

    这样下次 agent:start 时，九层内容已经是本轮新鲜的，而非上一轮的事后总结。
    """
    global _last_agent_message
    response = context.get("response", "") or ""
    if not response:
        return
    # 从全局变量取本轮用户消息（agent:start 时保存的）
    user_text = _last_agent_message
    if not user_text:
        return

    try:
        from kernel.mind_stack_runner import _collect_and_publish_sync, get_last_cognition
        # 同步阻塞式触发：等挖掘完成+收集完成后再返回
        # 最多等5秒，防止 gateway 响应被拖慢
        _collect_and_publish_sync(response=response, user_text=user_text)
        cog = get_last_cognition()
        logger.info("agent:end cognition collected: has_thought=%s insights=%d drives=%d sm_keys=%s",
                    cog.get("has_thought"), len(cog.get("insights", [])),
                    len(cog.get("drives", [])), list(cog.get("self_model", {}).keys()))
    except Exception as exc:
        logger.info("agent:end cognition collection failed: %s", exc)


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
