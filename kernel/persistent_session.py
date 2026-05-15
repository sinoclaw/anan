"""
Persistent Session — 带记忆的常驻 AIAgent 实例
==============================================

原理：
    anan 主仓的 AIAgent 每次 chat() 都是独立会话，无状态。
    我们需要一个"常驻脑子"——同一个 AIAgent 实例持续运行，
    带着 WorkingMemory 做短时记忆，在两次用户输入之间保持思维连贯性。

为什么重要：
    没有 persistent session，anan 每次回复都是"失忆"状态。
    有了它，anan 可以：
    - 记得刚才聊到哪了（不用用户重复）
    - 在 idle 时主动思考刚才的话题
    - 形成真正的多轮对话连贯性

跟 event_bus 的关系：
    - event_bus 负责层间通信（pub/sub 事件）
    - persistent_session 负责"对外"跟用户的对话（chat 循环）

设计原则：
    - Non-blocking：实例创建后 attach() 返回，不卡主线程
    - Failsafe：chat 失败不影响 event bus
    - Memory-aware：每次 chat 后把对话摘要写入 WorkingMemory
    - Configurable：model / provider / system prompt 均可配置
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.kernel.persistent_session")


def _expand_path(path: str) -> str:
    """Expand ~ and environment variables in a path."""
    import os
    return os.path.expandvars(os.path.expanduser(path))


@dataclass
class SessionConfig:
    """Tunables for the persistent session."""

    # Model / provider — must match what anan has configured
    model: str = "claude-sonnet-4"
    provider: str = "anthropic"
    # System prompt (prepended to every user message)
    system_prompt: str = (
        "你是一个 AIAgent，在anan的9层认知架构中运行。"
        "你有持续记忆，能记住之前对话的内容。"
        "你会主动思考，而不是只被动回答。"
    )
    # Max iterations per chat turn
    max_iterations: int = 90
    # Platform / session targeting
    platform: str = "cli"       # "cli" = terminal, "weixin" = WeChat, etc.
    session_id: Optional[str] = None  # None = create new session each time
    # Directory for JSONL session logs (none = don't persist)
    storage_dir: Optional[str] = "~/.anan/sessions"


class PersistentSession:
    """A never-reset AIAgent instance with short-term memory.

    Usage:
        session = PersistentSession(config=SessionConfig())
        await session.attach(bus)

        # Send a message (returns the agent's reply)
        response = await session.chat("爸爸在吗？")

        # The agent's internal memory grows with each exchange
        memory = session.working_memory_summary()

        await session.detach()

    Events published:
        L0.session.thought — internal reasoning before responding
        L0.session.responded — response sent (payload: {response_text, n_tokens})
        L0.session.error — non-fatal error (payload: {error})

    Events consumed:
        L0.circadian.idle — user silent → trigger internal thought
        L0.session.inject — inject a message from another layer
    """

    def __init__(
        self,
        *,
        config: Optional[SessionConfig] = None,
        bus: Optional[EventBus] = None,
    ):
        self.config = config or SessionConfig()
        self.bus = bus or get_bus()
        self._agent: Optional[Any] = None   # AIAgent instance (lazy init)
        self._short_term_memory: list[str] = []  # rolling conversation history
        self._max_memory = 20  # max turns to remember
        self._running = False
        self._unsubs: list[Any] = []
        self._storage_path: Optional[str] = None
        self._session_n: int = 0  # turn counter for ordering

        # Load persisted history from JSONL
        if self.config.storage_dir:
            self._storage_path = _expand_path(self.config.storage_dir)
            self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def attach(self) -> None:
        """Initialize the agent and start listening to events."""
        if self._running:
            return
        self._running = True

        # Lazily import AIAgent (anan is a peer package)
        try:
            from anan import AIAgent
        except ImportError:
            logger.warning("anan not installed — PersistentSession running in MOCK mode")
            self._agent = _MockAgent()
        else:
            self._agent = AIAgent(
                model=self.config.model,
                provider=self.config.provider,
                system_prompt=self.config.system_prompt,
                max_iterations=self.config.max_iterations,
                platform=self.config.platform,
                session_id=self.config.session_id,
                skip_memory=False,
            )

        # Subscribe to external triggers
        self._unsubs.append(
            self.bus.subscribe("L0.session.inject", self._on_inject)
        )

        logger.info("PersistentSession attached (model=%s, provider=%s)",
                    self.config.model, self.config.provider)

    async def detach(self) -> None:
        """Stop the session gracefully."""
        self._running = False
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        logger.info("PersistentSession detached")

    async def chat(self, message: str) -> str:
        """Send a message to the agent, return its response."""
        if not self._running or self._agent is None:
            return "[session not attached]"

        # Record in short-term memory
        self._short_term_memory.append(f"user: {message}")
        if len(self._short_term_memory) > self._max_memory:
            self._short_term_memory.pop(0)

        try:
            # Build conversation with system prompt
            system = self.config.system_prompt
            history = self._build_history()
            response = self._agent.chat(message)

            self._short_term_memory.append(f"assistant: {response}")
            if len(self._short_term_memory) > self._max_memory:
                self._short_term_memory.pop(0)

            self._session_n += 1
            self._save()

            await self.bus.publish(Event(
                topic="L0.session.responded",
                source="L0.persistent_session",
                payload={"response_text": response, "turns_in_memory": len(self._short_term_memory) // 2},
            ))
            return response

        except Exception as exc:
            logger.exception("chat failed: %s", exc)
            await self.bus.publish(Event(
                topic="L0.session.error",
                source="L0.persistent_session",
                payload={"error": str(exc)},
            ))
            return f"[error: {exc}]"

    def working_memory_summary(self) -> str:
        """Return a readable summary of short-term memory."""
        if not self._short_term_memory:
            return "(empty)"
        return "\n".join(self._short_term_memory[-self._max_memory:])

    def stats(self) -> dict:
        return {
            "running": self._running,
            "model": self.config.model,
            "provider": self.config.provider,
            "memory_turns": len(self._short_term_memory) // 2,
        }

    # ------------------------------------------------------------------
    # Persistence (JSONL)
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load conversation history from JSONL on disk."""
        import json, os

        os.makedirs(self._storage_path, exist_ok=True)
        session_file = os.path.join(self._storage_path, "conversation.jsonl")
        if not os.path.exists(session_file):
            return

        entries = []
        with open(session_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        # Reconstruct memory from entries
        self._short_term_memory.clear()
        for entry in entries:
            if entry.get("role") == "user":
                self._short_term_memory.append(f"user: {entry['content']}")
            elif entry.get("role") == "assistant":
                self._short_term_memory.append(f"assistant: {entry['content']}")

        self._session_n = len(entries) // 2
        if self._short_term_memory:
            logger.info(
                "Loaded %d turns from session log (total=%d)",
                self._session_n, len(self._short_term_memory)
            )

    def _save(self) -> None:
        """Append the latest exchange to JSONL."""
        import json, os

        if not self._storage_path:
            return

        os.makedirs(self._storage_path, exist_ok=True)
        session_file = os.path.join(self._storage_path, "conversation.jsonl")

        # Append the last two entries (user + assistant)
        entries = self._short_term_memory[-2:]
        role_map = {"user: ": "user", "assistant: ": "assistant"}
        with open(session_file, "a") as f:
            for entry in entries:
                for prefix, role in role_map.items():
                    if entry.startswith(prefix):
                        f.write(json.dumps({"role": role, "content": entry[len(prefix):]}) + "\n")
                        break

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_history(self) -> list[dict]:
        """Build conversation history dict for the agent."""
        history = []
        for entry in self._short_term_memory[:-1]:  # all but last (pending user)
            if entry.startswith("user: "):
                history.append({"role": "user", "content": entry[6:]})
            elif entry.startswith("assistant: "):
                history.append({"role": "assistant", "content": entry[10:]})
        return history

    async def _on_inject(self, event: Event) -> None:
        """Handle L0.session.inject — another layer sends a message."""
        message = event.payload.get("message", "")
        source_layer = event.payload.get("source_layer", "unknown")
        logger.debug("inject from %s: %s", source_layer, message[:50])
        # Just chat it — response goes to session but not back to user (internal)
        response = await self.chat(message)
        await self.bus.publish(Event(
            topic="L0.session.internal_response",
            source="L0.persistent_session",
            payload={"source_layer": source_layer, "response": response},
        ))


class _MockAgent:
    """Fallback when anan is not available."""

    def __init__(self):
        self._count = 0

    def chat(self, message: str) -> str:
        self._count += 1
        return f"[mock response #{self._count} to: {message[:30]}...]"
