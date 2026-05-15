"""
SessionReplay — 把 state.db 的历史会话回放为 EventBus 事件
================================================================

作用：
  gateway 启动时，读取 ~/.anan/state.db 的历史消息，
  生成九层能理解的内部事件流，让 PatternMiner 有历史可挖。

原理：
  真实对话消息 → gateway.message.sent（已有）
  但九层内部事件（goal/dispose/drive）需要从消息内容推理生成。

  例如：
    用户说"帮我搞定" → 推断 L7.goal.proposed（用户提出了一个目标）
    用户说"太无聊了" → 推断 L8.drive.boredom_triggered
    agent 回复了一个解释 → 推断 L4.consciousness.responded
    连续多条工具调用失败 → 推断 L6.metacognition.error_detected
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from kernel.event_bus import Event, get_bus

logger = logging.getLogger("gateway.builtin.mind_stack")


# ---------------------------------------------------------------------------
# 启发式事件推断规则
# ---------------------------------------------------------------------------

# 用户消息 → 内部事件
USER_PATTERNS: list[tuple[re.Pattern, str, dict]] = [
    # 目标导向
    (re.compile(r"(帮我|帮我搞|帮我做|帮我搞定|帮我弄).{0,20}"), "L7.goal.user_proposed", {"category": "user_request"}),
    (re.compile(r"(能不能|能否|是否可以).{0,30}(帮我|给我|我想)"), "L7.goal.user_proposed", {"category": "user_request"}),
    (re.compile(r"(我想|我要|我想要|我希望).{0,30}"), "L8.drive.user_intent", {"category": "user_desire"}),
    (re.compile(r"(提醒我|记得|别忘了).{0,30}"), "L7.goal.reminder_set", {"category": "reminder"}),
    (re.compile(r"(目标|goal).{0,20}(是什么|设定)"), "L7.goal.queried", {"category": "query"}),
    # 驱动力
    (re.compile(r"(好无聊|无聊|没意思|闲得慌)"), "L8.drive.boredom_triggered", {"strength": 0.8}),
    (re.compile(r"(好奇|想知道|想了解|想搞懂).{0,20}"), "L8.drive.curiosity_triggered", {"strength": 0.7}),
    (re.compile(r"(搞定|完成|成功了|好棒|成了)"), "L8.drive.completion_satisfied", {"strength": 0.9}),
    (re.compile(r"(关心|担心|害怕|忧虑).{0,20}"), "L8.drive.care_triggered", {"strength": 0.8}),
    # 元认知
    (re.compile(r"(错了|不对|不是这样|有问题|失败)"), "L6.metacognition.error_detected", {"domain": "user_feedback"}),
    (re.compile(r"(为什么|怎么|如何.{0,10}回事)"), "L6.metacognition.user_question", {"domain": "causal_query"}),
    (re.compile(r"(觉得|认为|判断|估计).{0,20}(是|应该|可能)"), "L6.metacognition.user_belief", {}),
    # 自我认知
    (re.compile(r"(你是谁|你是啥|你是干什么的|你叫什么)"), "L9.self.who_am_i_asked", {}),
    (re.compile(r"(你会什么|你能|你擅长|你的能力)"), "L9.self.capability_queried", {}),
    (re.compile(r"(我们|咱们).{0,20}(之前|上次|曾经|过去)"), "L9.self.past_context_recalled", {}),
    # Sleep / Daydreaming
    (re.compile(r"(晚安|睡了|去睡觉|先撤|明天见)"), "L1.sleep.user_bedtime", {}),
    (re.compile(r"(回来了|醒了|起了|早上好|起床)"), "L1.sleep.user_wake", {}),
    # 注意力
    (re.compile(r"(等等|停|等一下|先别).{0,10}(说|搞|做)"), "L3.attention.user_interrupted", {}),
]

# Assistant 消息 → 内部事件
ASSISTANT_PATTERNS: list[tuple[re.Pattern, str, dict]] = [
    (re.compile(r"(好的|收到|明白|了解|没问题).{0,30}"), "L4.consciousness.acknowledged", {}),
    (re.compile(r"(我来帮你|让我|我来想想).{0,20}"), "L4.consciousness.engaging", {}),
    (re.compile(r"(抱歉|对不起|错误).{0,20}"), "L6.metacognition.self_corrected", {}),
    (re.compile(r"(发现|注意到|我看到).{0,20}"), "L5.pattern.assistant_observed", {}),
    (re.compile(r"(根据|基于|因为).{0,20}(所以|因此)"), "L5.causal.assistant_reasoned", {}),
    (re.compile(r"(你的目标|咱们|我们要).{0,20}"), "L7.goal.assistant_acknowledged", {}),
    (re.compile(r"(好奇|想知道|有趣的)"), "L8.drive.curiosity_expressed", {"strength": 0.6}),
    (re.compile(r"^[\s]*$"), "L4.consciousness.empty_response", {}),
]

# Tool call → 内部事件
TOOL_PATTERNS: list[tuple[re.Pattern, str, dict]] = [
    (re.compile(r"(search|grep|find|look).{0,20}"), "L4.consciousness.tool_search", {}),
    (re.compile(r"(read|file|cat|open).{0,20}"), "L4.consciousness.tool_read", {}),
    (re.compile(r"(write|edit|patch|create).{0,20}"), "L4.consciousness.tool_write", {}),
    (re.compile(r"(run|exec|terminal|bash).{0,20}"), "L4.consciousness.tool_exec", {}),
    (re.compile(r"(send|message|post|tweet).{0,20}"), "L4.consciousness.tool_send", {}),
]


def infer_events(text: str, role: str, tool_name: Optional[str] = None) -> list[tuple[str, dict]]:
    """从消息内容推断内部事件。返回 [(topic, payload), ...]"""
    events = []

    if tool_name:
        for pattern, topic, extra in TOOL_PATTERNS:
            if pattern.search(tool_name.lower()):
                events.append((topic, {"tool": tool_name, **extra}))
                break

    if not text:
        return events

    text_lower = text.lower()
    patterns = ASSISTANT_PATTERNS if role == "assistant" else USER_PATTERNS

    for pattern, topic, extra in patterns:
        if pattern.search(text_lower):
            events.append((topic, {"text_preview": text[:80], **extra}))

    return events


# ---------------------------------------------------------------------------
# SessionReplay — 核心
# ---------------------------------------------------------------------------

class SessionReplay:
    """
    读取 state.db 历史消息，回放为 EventBus 事件序列。

    Usage:
        replay = SessionReplay(db_path="~/.anan/state.db", lookback_days=7)
        await replay.replay(bus=get_bus(), max_events=500)
    """

    def __init__(
        self,
        db_path: str = "~/.anan/state.db",
        lookback_days: int = 7,
        max_sessions: int = 20,
        max_messages_per_session: int = 200,
    ):
        self.db_path = Path(db_path).expanduser()
        self.lookback_days = lookback_days
        self.max_sessions = max_sessions
        self.max_messages_per_session = max_messages_per_session

    # ------------------------------------------------------------------ --
    # Public API
    # ------------------------------------------------------------------ --

    async def replay(self, bus, max_events: int = 500) -> int:
        """
        读取历史消息，回放事件到 EventBus。

        Returns: 实际回放的事件数
        """
        sessions = self._fetch_sessions()
        if not sessions:
            logger.info("SessionReplay: no sessions found in last %d days", self.lookback_days)
            return 0

        logger.info("SessionReplay: found %d sessions, replaying...", len(sessions))

        events_replayed = 0
        for session in sessions[:self.max_sessions]:
            session_id = session["id"]
            source = session.get("source", "unknown")
            messages = self._fetch_messages(session_id)
            if not messages:
                continue

            for msg in messages[:self.max_messages_per_session]:
                if events_replayed >= max_events:
                    break

                topic, payload = self._msg_to_event(msg, source)
                if topic is None:
                    continue

                await bus.publish(Event(
                    topic=topic,
                    source="session_replay",
                    payload=payload,
                ))
                events_replayed += 1

                # 限速：每条消息后等 0.01s，避免撑爆 event history
                await asyncio.sleep(0.01)

            if events_replayed >= max_events:
                break

        logger.info("SessionReplay: replayed %d events", events_replayed)
        return events_replayed

    # ------------------------------------------------------------------ --
    # Internal
    # ------------------------------------------------------------------ --

    def _fetch_sessions(self) -> list[dict]:
        cutoff = time.time() - self.lookback_days * 86400
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                """
                SELECT id, source, started_at, message_count, title
                FROM sessions
                WHERE started_at >= ? AND message_count > 0
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (cutoff, self.max_sessions),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def _fetch_messages(self, session_id: str) -> list[dict]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                """
                SELECT role, content, tool_name, timestamp
                FROM messages
                WHERE session_id = ? AND role IN ('user', 'assistant')
                ORDER BY timestamp ASC
                LIMIT ?
                """,
                (session_id, self.max_messages_per_session),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def _msg_to_event(self, msg: dict, source: str) -> tuple[Optional[str], dict]:
        role = msg.get("role", "")
        text = msg.get("content", "") or ""
        tool_name = msg.get("tool_name") or ""
        ts = msg.get("timestamp", 0)

        if isinstance(text, list):
            text = " ".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in text
            )
        elif isinstance(text, dict):
            text = text.get("text", "") or str(text)

        text = str(text).strip()
        if not text and not tool_name:
            return None, {}

        # 推断事件
        inferred = infer_events(text, role, tool_name if tool_name else None)

        if inferred:
            # 只取第一个匹配的事件
            topic, payload = inferred[0]
        elif role == "user":
            topic = "gateway.message.replayed_user"
            payload = {"text_preview": text[:100], "source": source}
        elif role == "assistant":
            topic = "gateway.message.replayed_assistant"
            payload = {"text_preview": text[:100], "source": source}
        else:
            return None, {}

        payload["replayed_ts"] = ts
        payload["session_source"] = source

        return topic, payload


# 需要 asyncio
import asyncio
