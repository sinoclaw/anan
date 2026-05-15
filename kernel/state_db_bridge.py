"""
StateDB Event Bridge — 将 state.db 历史对话注入 EventBus
=========================================================

在 MindStackRunner 启动后调用一次，把历史消息回填到 EventBus history，
这样 PatternMiner 才能从真实对话流中挖掘因果规律。

只填充最近 7 天的数据，避免历史过长。
每个消息转换为 gateway.message.sent 事件，模拟当时的对话流。
"""

from __future__ import annotations

import asyncio
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


async def bridge_state_db_to_event_bus(
    bus,
    max_history_days: int = 7,
    limit_per_session: int = 100,
) -> int:
    """
    异步入口 — 在 MindStackRunner 启动后调用。
    返回注入的事件数量。
    """
    loop = asyncio.get_running_loop()

    def _sync_load() -> int:
        return _load_state_db_bridge(bus, max_history_days, limit_per_session)

    return await loop.run_in_executor(None, _sync_load)
