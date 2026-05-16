"""
StateDB Event Bridge — 将 state.db 历史对话和事件注入 EventBus
=============================================================

在 MindStackRunner 启动后调用一次，把历史消息回填到 EventBus history，
这样 PatternMiner 才能从真实对话流中挖掘因果规律。

只填充最近 7 天的数据，避免历史过长。
每个消息转换为 gateway.message.sent 事件，模拟当时的对话流。

同时提供事件持久化能力：EventBus.publish() 时自动写入 event_history 表，
gateway 重启后可从 state.db 恢复事件历史，保证九层连续存在。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Optional

logger = logging.getLogger("gateway.builtin.mind_stack")


def _load_state_db_bridge(bus, max_history_days: int = 7, limit_per_session: int = 100):
    """
    同步加载器 — 把 state.db 历史注入 EventBus。
    每次 publish 都会追加到 EventBus._history，供 PatternMiner.mine_now() 扫描。

    Args:
        bus: EventBus 实例
        max_history_days: 只加载最近 N 天的数据
        limit_per_session: 每个 session 最多加载多少条消息
    """
    cutoff_ts = time.time() - max_history_days * 86400

    try:
        conn = sqlite3.connect("/root/.anan/state.db", timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
    except Exception as exc:
        logger.warning("StateDB Bridge: 无法连接 state.db: %s", exc)
        return

    try:
        # 取最近 N 天的 session
        cur.execute(
            "SELECT id, source, started_at FROM sessions WHERE started_at > ? ORDER BY started_at DESC",
            (cutoff_ts,),
        )
        sessions = cur.fetchall()
        logger.info("StateDB Bridge: 发现 %d 个 recent sessions", len(sessions))

        total_events = 0

        for session in sessions:
            session_id = session["id"]
            source = session["source"] or "unknown"

            # 取该 session 的消息（只取 user 和 assistant 的）
            cur.execute(
                """
                SELECT role, content, timestamp
                FROM messages
                WHERE session_id = ? AND role IN ('user', 'assistant')
                ORDER BY timestamp
                LIMIT ?
                """,
                (session_id, limit_per_session),
            )
            messages = cur.fetchall()

            for msg in messages:
                role = msg["role"]
                content = msg["content"] or ""
                ts = msg["timestamp"]

                # 跳过空消息和超长上下文压缩消息
                if not content or content.startswith("[CONTEXT COMPACTION"):
                    continue

                # 截断过长内容
                if len(content) > 500:
                    content = content[:500] + "..."

                from kernel.event_bus import Event

                event = Event(
                    topic="gateway.message.sent",
                    source="state_db_bridge",
                    payload={
                        "platform": source,
                        "user": session_id,
                        "role": role,
                        "text": content[:200] if role == "user" else "",
                        "response": content[:200] if role == "assistant" else "",
                        "session_id": session_id,
                        "ts": ts,
                    },
                )
                bus.publish_sync(event)
                total_events += 1

        logger.info(
            "StateDB Bridge: 注入 %d 个历史事件到 EventBus (最近 %d 天)",
            total_events,
            max_history_days,
        )
        return total_events

    except Exception as exc:
        logger.warning("StateDB Bridge: 加载失败: %s", exc)
        return 0
    finally:
        conn.close()


_EVENT_PERSIST_CALLBACK: Optional[callable] = None


def register_event_persister(callback: callable) -> None:
    """Register a function to be called on every EventBus.publish().
    
    The callback receives (topic, payload, source, ts, event_id).
    Used to persist events to StateDB for continuity across restarts.
    """
    global _EVENT_PERSIST_CALLBACK
    _EVENT_PERSIST_CALLBACK = callback


def _persist_event_sync(topic: str, payload: dict, source: str, ts: float, event_id: str) -> None:
    """Synchronous event persistence — runs in thread pool to avoid blocking event loop."""
    try:
        conn = sqlite3.connect("/root/.anan/state.db", timeout=5)
        cur = conn.cursor()
        cur.execute(
            """INSERT OR IGNORE INTO event_history (event_id, topic, payload, source, ts)
               VALUES (?, ?, ?, ?, ?)""",
            (event_id, topic, json.dumps(payload, default=str), source, ts),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # non-fatal


def ensure_event_history_table() -> None:
    """Create the event_history table if it doesn't exist."""
    try:
        conn = sqlite3.connect("/root/.anan/state.db", timeout=5)
        cur = conn.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS event_history (
                event_id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                payload TEXT NOT NULL,
                source TEXT,
                ts REAL NOT NULL
            )"""
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_event_history_ts ON event_history(ts)")
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("ensure_event_history_table failed: %s", exc)


def load_event_history(bus, max_events: int = 500) -> int:
    """Load persisted events from state.db into the EventBus on startup."""
    try:
        conn = sqlite3.connect("/root/.anan/state.db", timeout=5)
        cur = conn.cursor()
        cur.execute(
            "SELECT event_id, topic, payload, source, ts FROM event_history "
            "ORDER BY ts DESC LIMIT ?",
            (max_events,),
        )
        rows = cur.fetchall()
        conn.close()

        if not rows:
            return 0

        from kernel.event_bus import Event

        loaded = 0
        for row in reversed(rows):  # oldest first
            event_id, topic, payload_json, source, ts = row
            try:
                payload = json.loads(payload_json)
            except Exception:
                payload = {}
            event = Event(
                topic=topic,
                payload=payload,
                source=source or "recovery",
                ts=ts,
                event_id=event_id,
            )
            bus.publish_sync(event)
            loaded += 1

        logger.info("EventBus recovery: loaded %d events from state.db", loaded)
        return loaded
    except Exception as exc:
        logger.warning("load_event_history failed: %s", exc)
        return 0


async def bridge_state_db_to_event_bus(
    bus,
    max_history_days: int = 7,
    limit_per_session: int = 100,
) -> int:
    """
    异步入口 — 在 MindStackRunner 启动后调用。
    返回注入的事件数量。
    """
    # 确保 event_history 表存在
    ensure_event_history_table()

    # 恢复历史事件（gateway 重启后九层能接上）
    recovered = load_event_history(bus)
    logger.info("Recovered %d events from previous session", recovered)

    loop = asyncio.get_running_loop()

    def _sync_load() -> int:
        return _load_state_db_bridge(bus, max_history_days, limit_per_session)

    injected = await loop.run_in_executor(None, _sync_load)
    return injected
