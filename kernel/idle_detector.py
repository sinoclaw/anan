"""
Idle Detector — 检测用户沉默，触发意识流
==========================================

原理：
    轮询 sinoclaw session DB 的 messages 表，找用户最近一条消息的时间。
    超过 idle_threshold_s 没动静 → 发 L0.circadian.idle 事件。
    L4/L5/L8 订阅此事件，触发 Daydreaming / 主动思考。

为什么重要：
    没有 idle detection，anan 永远是"等用户说话才动"。
    有了 idle detection，anan 可以在用户沉默时主动：
    - 回顾刚才的对话有没有更好的回答
    - 思考用户提到但没展开的问题
    - 检查自己的目标有没有进展
    - 主动产生想法发给用户

事件：
    L0.circadian.idle — 用户沉默超过阈值 (payload: {idle_s, last_message_ts})

设计原则：
    - Non-blocking：轮询在独立 asyncio task，不卡事件总线
    - Failsafe：DB 查不到不崩溃，只 warn 并跳过
    - Configurable：idle_threshold / poll_interval 均可配置
    - Layer-aware：不跟 CircadianLoop 打架（idle 是"外部沉默"，sleep 是"内部疲劳"）
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.kernel.idle_detector")


@dataclass
class IdleConfig:
    """Tunables for idle detection."""

    # Path to sinoclaw session DB
    state_db_path: Path = field(
        default_factory=lambda: Path.home() / ".sinoclaw" / "state.db"
    )
    # How long (seconds) the user must be silent before we call it "idle"
    idle_threshold_s: float = 90.0
    # How often (seconds) to check the DB
    poll_interval_s: float = 15.0
    # Only watch this platform's sessions (None = any platform)
    platform_filter: Optional[str] = None
    # Only watch sessions with this source (None = any source, "weixin" = Wechat, etc.)
    source_filter: Optional[str] = None


class IdleDetector:
    """Poll session DB for user message gaps.

    Usage:
        detector = IdleDetector(config=IdleConfig(idle_threshold_s=120))
        await detector.attach(bus)
        # detector runs in background, publishes L0.circadian.idle on silence

    Detach to stop polling:
        await detector.detach()
    """

    def __init__(
        self,
        *,
        config: Optional[IdleConfig] = None,
        bus: Optional[EventBus] = None,
    ):
        self.config = config or IdleConfig()
        self.bus = bus or get_bus()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_idle_at: Optional[float] = None  # avoid repeated idle events

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def attach(self) -> None:
        """Start polling in background."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("IdleDetector attached (threshold=%.0fs, poll=%.0fs)",
                    self.config.idle_threshold_s, self.config.poll_interval_s)

    async def detach(self) -> None:
        """Stop polling gracefully."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("IdleDetector detached")

    def stats(self) -> dict:
        return {
            "running": self._running,
            "idle_threshold_s": self.config.idle_threshold_s,
            "poll_interval_s": self.config.poll_interval_s,
            "last_idle_at": self._last_idle_at,
        }

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                idle_for = await self._check_idle()
                if idle_for is not None and idle_for >= self.config.idle_threshold_s:
                    # Only fire once per idle period
                    if self._last_idle_at is None or \
                       (time.time() - self._last_idle_at) > self.config.idle_threshold_s:
                        await self._fire_idle(idle_for)
                        self._last_idle_at = time.time()
            except Exception as exc:
                logger.warning("IdleDetector poll error (non-fatal): %s", exc)
            await asyncio.sleep(self.config.poll_interval_s)

    async def _check_idle(self) -> Optional[float]:
        """Query DB for latest user message timestamp. Returns seconds idle or None."""
        db_path = self.config.state_db_path
        if not db_path.exists():
            logger.debug("state.db not found at %s", db_path)
            return None

        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            # Build WHERE clause
            conditions = ["role = ?"]
            params: list = ["user"]
            if self.config.source_filter:
                conditions.append("session_id IN (SELECT id FROM sessions WHERE source = ?)")
                params.append(self.config.source_filter)

            query = f"""
                SELECT m.timestamp
                FROM messages m
                JOIN sessions s ON m.session_id = s.id
                WHERE {' AND '.join(conditions)}
                ORDER BY m.timestamp DESC
                LIMIT 1
            """
            c.execute(query, params)
            row = c.fetchone()
            conn.close()

            if row is None or row["timestamp"] is None:
                return None

            last_msg_ts = row["timestamp"]
            idle_s = time.time() - last_msg_ts
            logger.debug("last user message: %.0fs ago", idle_s)
            return idle_s

        except Exception as exc:
            logger.debug("idle check failed: %s", exc)
            return None

    async def _fire_idle(self, idle_for: float) -> None:
        """Publish L0.circadian.idle event."""
        logger.info("User idle for %.0fs — firing idle event", idle_for)
        await self.bus.publish(Event(
            topic="L0.circadian.idle",
            source="L0.idle_detector",
            payload={
                "idle_s": round(idle_for, 1),
                "threshold_s": self.config.idle_threshold_s,
            },
        ))
