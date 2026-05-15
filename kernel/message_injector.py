"""
Message Injector — 主动给用户发消息（不打扰模式）
===============================================

原理：
    L4/L5/L8 层在 idle 时产生的主动想法，需要发给用户。
    用 anan 的 send_message 工具走 gateway 投递给 home channel。

为什么重要：
    没有 injector，anan 的"主动想法"永远只存在于内部事件总线。
    有了 injector，anan 可以：
    - 在 idle 时给爸爸发："刚想到一个帮你修 bug 的思路，要看吗？"
    - 睡前给爸爸发："今天的 CI 还是 fail，你要我明天继续看吗？"
    - 检测到异常状态时告警

事件：
    L0.session.inject — inject a message to the user (from any layer)
    payload: {message: str, silent: bool, channel: Optional[str]}

设计原则：
    - Silent by default：主动消息默认不发用户，只写日志
    - Explicit "important enough" 才真正发送
    - 支持 channel 指定（weixin / telegram / etc.）
    - Failsafe：发不出去只 warn，不崩

依赖：
    - anan send_message 工具（通过 anan_tools 或直接调用）
    - gateway home channel 配置
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.kernel.message_injector")


@dataclass
class InjectorConfig:
    """Tunables for message injection."""

    # If True, messages go to home channel by default (weixin, telegram, etc.)
    # If False, default to silent (log only, don't send)
    default_send: bool = False
    # Maximum主动消息 per hour (rate limit to avoid spamming)
    max_per_hour: int = 6
    # Home channel platform name
    home_platform: str = "weixin"
    # Home channel chat ID
    home_chat_id: Optional[str] = None


class MessageInjector:
    """Subscribe to L0.session.inject and deliver messages.

    Usage:
        injector = MessageInjector(config=InjectorConfig(default_send=True))
        await injector.attach(bus)

        # From any layer:
        await bus.publish(Event(topic="L0.session.inject", source="L5",
                               payload={"message": "想到一个方案...", "silent": False}))

    Events consumed:
        L0.session.inject — payload: {message, silent?, channel?}

    Events published:
        L0.injector.sent — message actually delivered (payload: {message_preview, channel})
        L0.injector.dropped — rate limit hit (payload: {message_preview, reason})
        L0.injector.error — send failed (payload: {error, channel})
    """

    def __init__(
        self,
        *,
        config: Optional[InjectorConfig] = None,
        bus: Optional[EventBus] = None,
    ):
        self.config = config or InjectorConfig()
        self.bus = bus or get_bus()
        self._unsubs: list = []
        self._sent_this_hour: list[float] = []  # timestamps

    async def attach(self) -> None:
        self._unsubs.append(
            self.bus.subscribe("L0.session.inject", self._on_inject)
        )
        logger.info("MessageInjector attached (default_send=%s, max_per_hour=%d)",
                    self.config.default_send, self.config.max_per_hour)

    async def detach(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        logger.info("MessageInjector detached")

    def stats(self) -> dict:
        return {
            "sent_this_hour": len(self._sent_recent()),
            "max_per_hour": self.config.max_per_hour,
            "default_send": self.config.default_send,
        }

    def _sent_recent(self) -> list[float]:
        """Timestamps from the last hour."""
        import time
        cutoff = time.time() - 3600
        self._sent_this_hour = [t for t in self._sent_this_hour if t > cutoff]
        return self._sent_this_hour

    async def _on_inject(self, event: Event) -> None:
        import time
        payload = event.payload
        message: str = payload.get("message", "")
        silent: bool = payload.get("silent", not self.config.default_send)
        channel: Optional[str] = payload.get("channel")

        if not message:
            return

        preview = message[:60] + ("..." if len(message) > 60 else "")

        # Rate limit check
        if len(self._sent_recent()) >= self.config.max_per_hour:
            logger.warning("rate limit hit — dropping: %s", preview)
            await self.bus.publish(Event(
                topic="L0.injector.dropped",
                source="L0.message_injector",
                payload={"message_preview": preview, "reason": "max_per_hour"},
            ))
            return

        # Silent mode: log only
        if silent:
            logger.info("[silent] %s", preview)
            return

        # Actually send
        target = channel or self.config.home_platform
        success = await self._send(target, message)

        if success:
            self._sent_this_hour.append(time.time())
            await self.bus.publish(Event(
                topic="L0.injector.sent",
                source="L0.message_injector",
                payload={"message_preview": preview, "channel": target},
            ))
        else:
            await self.bus.publish(Event(
                topic="L0.injector.error",
                source="L0.message_injector",
                payload={"error": "send failed", "channel": target},
            ))

    async def _send(self, platform: str, message: str) -> bool:
        """Deliver message via anan send_message tool."""
        try:
            # Try via anan_tools if available
            from anan_tools import send_message as sc_send
            result = await asyncio.to_thread(
                sc_send, action="send", target=f"{platform}", message=message
            )
            logger.info("sent via %s: %s", platform, message[:50])
            return True
        except ImportError:
            pass

        try:
            # Try via anan CLI
            import subprocess, json
            result = subprocess.run(
                ["anan", "send", "--platform", platform, "--message", message],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except Exception as exc:
            logger.warning("send_message failed: %s", exc)
            return False
