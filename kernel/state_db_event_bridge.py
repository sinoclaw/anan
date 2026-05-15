"""
StateDB Event Bridge — 历史会话回填到 EventBus
==============================================

一次性把 state.db 里的历史对话事件回填到 EventBus，
让 PatternMiner 扫描历史时能看到真实的对话流。

工作方式：
  1. 从 state.db 读取近 N 天的会话
  2. 按时间顺序重放每个会话的 user→assistant 对
  3. 每个 pair 发一个 conversation.pair 事件（含摘要）
  4. 会话开始/结束发 session.started / session.ended 事件

这样 PatternMiner 能看到：
  - 跨会话的 topic 共现规律
  - 特定用户行为的常见后续
  - 对话节奏模式

用法（一次性初始化）：
  from kernel.state_db_event_bridge import replay_recent_sessions
  await replay_recent_sessions(bus, days=7, max_sessions=100)
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("anan.kernel.state_db_bridge")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class ConversationPair:
    session_id: str
    platform: str
    user_id: str
    user_message: str
    assistant_message: str
    timestamp: float


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class StateDBEventBridge:
    """
    从 state.db 读取历史会话，回填到 EventBus。

    只读取近 days 天的数据，最多 replay max_sessions 个会话
    （按消息数倒序，优先最丰富的会话）。
    """

    def __init__(
        self,
        db_path: str = "~/.anan/state.db",
        days: int = 7,
        max_sessions: int = 50,
        max_pairs_per_session: int = 50,
    ):
        import os
        self._db_path = os.path.expanduser(db_path)
        self._days = days
        self._max_sessions = max_sessions
        self._max_pairs = max_pairs_per_session

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _load_sessions(self, conn: sqlite3.Connection) -> list[dict]:
        """
        读取近 N 天最活跃的 sessions，按消息数倒序。
        """
        cutoff = time.time() - self._days * 86400
        cur = conn.cursor()
        cur.execute("""
            SELECT s.id, s.source, s.started_at,
                   COUNT(m.id) as msg_count
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.started_at > ?
              AND s.source != 'cron'
              AND s.message_count > 2
            GROUP BY s.id
            ORDER BY msg_count DESC
            LIMIT ?
        """, (cutoff, self._max_sessions))
        return [
            {"id": r[0], "source": r[1],
             "started_at": r[2], "msg_count": r[3]}
            for r in cur.fetchall()
        ]

    def _load_pairs(
        self, conn: sqlite3.Connection, session_id: str
    ) -> list[ConversationPair]:
        """
        读取一个 session 里的 user→assistant 对。
        只保留有实际内容的 pair。
        """
        cur = conn.cursor()
        cur.execute("""
            SELECT
                m.content,
                a.content,
                a.timestamp
            FROM (
                SELECT session_id, role, content, timestamp,
                       ROW_NUMBER() OVER (ORDER BY timestamp) as rn
                FROM messages
                WHERE session_id = ?
                  AND role = 'user'
                  AND length(content) > 5
            ) m
            LEFT JOIN messages a ON a.session_id = m.session_id
                AND a.role = 'assistant'
                AND a.timestamp >= m.timestamp
                AND a.timestamp < m.timestamp + 300
            ORDER BY m.timestamp
            LIMIT ?
        """, (session_id, self._max_pairs))

        pairs = []
        for r in cur.fetchall():
            if r[1] and len(str(r[1])) > 5:  # only keep pairs with non-empty response
                pairs.append(ConversationPair(
                    session_id=session_id,
                    platform="",
                    user_id="unknown",  # messages 表无 user_id 列
                    user_message=str(r[0])[:200],
                    assistant_message=str(r[1])[:200],
                    timestamp=r[2],
                ))
        return pairs

    async def replay(self, bus) -> dict:
        """
        执行回填。返回统计信息。
        """
        logger.info("StateDB 回填开始: days=%d, max_sessions=%d",
                    self._days, self._max_sessions)

        conn = self._connect()
        sessions = self._load_sessions(conn)
        logger.info("读取到 %d 个活跃会话", len(sessions))
        conn.close()

        if not sessions:
            return {"sessions": 0, "pairs": 0}

        from kernel.event_bus import Event

        total_pairs = 0
        for sess in sessions:
            # Session started event
            await bus.publish(Event(
                topic="session.started",
                source="state_db_bridge",
                payload={
                    "session_id": sess["id"],
                    "platform": sess["source"],
                    "user_id": "unknown",
                },
            ))

            # Load and replay pairs
            conn = self._connect()
            pairs = self._load_pairs(conn, sess["id"])
            conn.close()

            prev_topic = None
            for pair in pairs:
                # 发成 conversation.pair 事件
                # 提取内容标签（简化版：按长度+关键词打标）
                topic = self._extract_topic(pair.user_message)
                await bus.publish(Event(
                    topic="conversation.pair",
                    source="state_db_bridge",
                    payload={
                        "session_id": pair.session_id,
                        "platform": sess["source"],
                        "user_id": pair.user_id,
                        "user_preview": pair.user_message[:80],
                        "assistant_preview": pair.assistant_message[:80],
                        "topic": topic,
                        "timestamp": pair.timestamp,
                    },
                ))
                total_pairs += 1

            # Session ended event
            await bus.publish(Event(
                topic="session.ended",
                source="state_db_bridge",
                payload={
                    "session_id": sess["id"],
                    "platform": sess["source"],
                    "pair_count": len(pairs),
                },
            ))

        logger.info("StateDB 回填完成: %d sessions, %d pairs", len(sessions), total_pairs)
        return {"sessions": len(sessions), "pairs": total_pairs}

    def _extract_topic(self, text: str) -> str:
        """从消息内容提取简单 topic 标签。"""
        text_lower = text.lower()
        if any(k in text_lower for k in ["代码", "code", "bug", "error", "函数"]):
            return "topic:code"
        if any(k in text_lower for k in ["测试", "test", "验证"]):
            return "topic:testing"
        if any(k in text_lower for k in ["计划", "plan", "todo", "任务"]):
            return "topic:planning"
        if any(k in text_lower for k in ["部署", "deploy", "server", "服务器"]):
            return "topic:devops"
        if any(k in text_lower for k in ["内存", "memory", "记忆", "历史"]):
            return "topic:memory"
        if any(k in text_lower for k in ["模型", "model", "llm", "gpt"]):
            return "topic:model"
        if any(k in text_lower for k in ["config", "配置", "设置", "setup"]):
            return "topic:config"
        if len(text) < 20:
            return "topic:short"
        return "topic:general"


# ---------------------------------------------------------------------------
# One-shot convenience
# ---------------------------------------------------------------------------

async def replay_recent_sessions(
    bus,
    days: int = 7,
    max_sessions: int = 50,
) -> dict:
    """
    快捷函数：在 MindStackRunner 启动后调用一次，
    把近 N 天的历史对话回填到 EventBus。
    """
    bridge = StateDBEventBridge(days=days, max_sessions=max_sessions)
    return await bridge.replay(bus)
