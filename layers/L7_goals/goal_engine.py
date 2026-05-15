"""
L7 Goals — 目标生成引擎
=========================
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.L7.goals")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class GoalStatus(Enum):
    PROPOSED   = "proposed"
    ACTIVE     = "active"
    ACHIEVED   = "achieved"
    ABANDONED  = "abandoned"
    BLOCKED    = "blocked"


class GoalScope(Enum):
    IMMEDIATE = "immediate"   # 今天能完成
    SHORT     = "short"       # 本周
    MEDIUM    = "medium"      # 本月
    LONG      = "long"        # 更长期


@dataclass
class Goal:
    id: str
    description: str
    scope: GoalScope
    status: GoalStatus = GoalStatus.PROPOSED
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    achieved_at: Optional[str] = None
    sub_goals: list[str] = field(default_factory=list)
    parent_id: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    immediate_actions: list[str] = field(default_factory=list)
    this_week_actions: list[str] = field(default_factory=list)
    this_month_actions: list[str] = field(default_factory=list)
    priority_boost: float = 0.0
    source_event: Optional[str] = None

    def touch(self):
        self.updated_at = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "scope": self.scope.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "achieved_at": self.achieved_at,
            "sub_goals": list(self.sub_goals),
            "parent_id": self.parent_id,
            "tags": list(self.tags),
            "immediate_actions": list(self.immediate_actions),
            "this_week_actions": list(self.this_week_actions),
            "this_month_actions": list(self.this_month_actions),
            "priority_boost": round(self.priority_boost, 3),
            "source_event": self.source_event,
        }


# ---------------------------------------------------------------------------
# GoalGenerator
# ---------------------------------------------------------------------------

class GoalGenerator:
    """L7 — 目标生成 + 分解 + 冲突检测"""

    MAX_ACTIVE_GOALS = 10
    MAX_SUBGOALS_PER_GOAL = 5

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        self_model=None,
    ):
        self._bus = bus or get_bus()
        self._sm = self_model
        self._goals: dict[str, Goal] = {}
        self._active_order: deque[str] = deque(maxlen=self.MAX_ACTIVE_GOALS)
        self._id_counter: int = 0
        self._unsubs: list = []

    async def attach(self) -> None:
        self._unsubs.append(
            self._bus.subscribe("L6.metacognition.report", self._on_metacognition_report)
        )
        self._unsubs.append(
            self._bus.subscribe("L9.self.updated", self._on_self_updated)
        )
        self._unsubs.append(
            self._bus.subscribe("L7.goal.request_decompose", self._on_request_decompose)
        )
        # L8 drive suggestion → 生成对应的目标
        self._unsubs.append(
            self._bus.subscribe("L8.drive.suggestion", self._on_drive_suggestion)
        )
        # L0 tick 周期性检查是否需要生成新目标
        self._unsubs.append(
            self._bus.subscribe("L0.circadian.tick", self._on_circadian_tick)
        )
        logger.info("GoalGenerator attached")

    async def detach(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()

    def propose(
        self,
        description: str,
        scope: GoalScope,
        *,
        tags: Optional[list[str]] = None,
        source_event: Optional[str] = None,
    ) -> Goal:
        gid = f"goal-{self._id_counter}"
        self._id_counter += 1

        goal = Goal(
            id=gid,
            description=description,
            scope=scope,
            status=GoalStatus.ACTIVE,
            tags=tags or [],
            source_event=source_event,
        )
        self._goals[gid] = goal
        self._active_order.append(gid)

        self._bus.publish_sync(Event(
            topic="L7.goal.proposed",
            source="L7.goals",
            payload=goal.to_dict(),
        ))
        logger.info("Goal proposed: [%s] %s", scope.value, description)
        return goal

    def decompose(self, goal_id: str) -> list[str]:
        goal = self._goals.get(goal_id)
        if not goal:
            return []

        sub_ids = []
        desc = goal.description.lower()
        scope = goal.scope

        if scope in (GoalScope.IMMEDIATE, GoalScope.SHORT):
            action = f"立即行动：{goal.description}"
            if action not in goal.immediate_actions:
                goal.immediate_actions.append(action)
                sub_ids.append(f"{goal_id}-sub-0")
        elif scope == GoalScope.MEDIUM:
            week_action = f"本周：{goal.description}"
            month_action = f"本月：{goal.description}"
            if week_action not in goal.this_week_actions:
                goal.this_week_actions.append(week_action)
                sub_ids.append(f"{goal_id}-sub-week")
            if month_action not in goal.this_month_actions:
                goal.this_month_actions.append(month_action)
                sub_ids.append(f"{goal_id}-sub-month")
        elif scope == GoalScope.LONG:
            month_action = f"本月规划：{goal.description}"
            if month_action not in goal.this_month_actions:
                goal.this_month_actions.append(month_action)
                sub_ids.append(f"{goal_id}-sub-month")
            sub_goal = self._add_subgoal(
                parent_id=goal_id,
                description=f"分解长期目标：{goal.description}",
                scope=GoalScope.SHORT,
            )
            if sub_goal:
                sub_ids.append(sub_goal)

        goal.sub_goals.extend(sub_ids)
        goal.touch()

        self._bus.publish_sync(Event(
            topic="L7.goal.decomposed",
            source="L7.goals",
            payload={
                "goal_id": goal_id,
                "sub_ids": sub_ids,
                "immediate": list(goal.immediate_actions),
                "this_week": list(goal.this_week_actions),
                "this_month": list(goal.this_month_actions),
            },
        ))
        return sub_ids

    def achieve(self, goal_id: str) -> bool:
        goal = self._goals.get(goal_id)
        if not goal:
            return False
        goal.status = GoalStatus.ACHIEVED
        goal.achieved_at = datetime.now().isoformat()
        goal.touch()

        self._active_order = deque(
            (g for g in self._active_order if g != goal_id),
            maxlen=self.MAX_ACTIVE_GOALS,
        )

        self._bus.publish_sync(Event(
            topic="L7.goal.achieved",
            source="L7.goals",
            payload=goal.to_dict(),
        ))
        logger.info("Goal achieved: %s", goal.description)
        return True

    def abandon(self, goal_id: str, reason: str = "") -> bool:
        goal = self._goals.get(goal_id)
        if not goal:
            return False
        goal.status = GoalStatus.ABANDONED
        goal.touch()

        self._active_order = deque(
            (g for g in self._active_order if g != goal_id),
            maxlen=self.MAX_ACTIVE_GOALS,
        )

        self._bus.publish_sync(Event(
            topic="L7.goal.abandoned",
            source="L7.goals",
            payload={**goal.to_dict(), "reason": reason},
        ))
        return True

    def boost_priority(self, goal_id: str, delta: float) -> bool:
        goal = self._goals.get(goal_id)
        if not goal:
            return False
        goal.priority_boost = max(0.0, min(1.0, goal.priority_boost + delta))
        goal.touch()
        return True

    def detect_conflicts(self, goal_id: str) -> list[tuple[str, str]]:
        goal = self._goals.get(goal_id)
        if not goal:
            return []

        conflicts = []
        for other in self._goals.values():
            if other.id == goal_id:
                continue
            if other.status not in (GoalStatus.ACTIVE, GoalStatus.PROPOSED):
                continue
            if other.scope != goal.scope:
                continue
            overlap = set(other.tags) & set(goal.tags)
            if overlap:
                conflicts.append((goal.id, other.id))
                self._bus.publish_sync(Event(
                    topic="L7.goal.conflict",
                    source="L7.goals",
                    payload={
                        "goal_a": goal.to_dict(),
                        "goal_b": other.to_dict(),
                        "overlap_tags": list(overlap),
                    },
                ))
        return conflicts

    def active_goals(self) -> list[Goal]:
        return [
            self._goals[gid]
            for gid in self._active_order
            if gid in self._goals and self._goals[gid].status == GoalStatus.ACTIVE
        ]

    def top_goals(self, n: int = 3) -> list[Goal]:
        scope_order = {
            GoalScope.IMMEDIATE: 0,
            GoalScope.SHORT: 1,
            GoalScope.MEDIUM: 2,
            GoalScope.LONG: 3,
        }
        active = self.active_goals()
        scored = [
            (g, g.priority_boost - scope_order[g.scope] * 0.1)
            for g in active
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [g for g, _ in scored[:n]]

    def what_are_my_goals(self) -> str:
        top = self.top_goals()
        if not top:
            return "我目前没有激活的目标。"
        lines = ["我当前的目标:"]
        for g in top:
            scope_label = {
                "immediate": "立即", "short": "本周",
                "medium": "本月", "long": "长期",
            }[g.scope.value]
            lines.append(f"  [{scope_label}] {g.description}")
        return "\n".join(lines)

    def _add_subgoal(
        self,
        parent_id: str,
        description: str,
        scope: GoalScope,
    ) -> Optional[str]:
        parent = self._goals.get(parent_id)
        if not parent or len(parent.sub_goals) >= self.MAX_SUBGOALS_PER_GOAL:
            return None

        gid = f"goal-{self._id_counter}"
        self._id_counter += 1

        sub = Goal(
            id=gid,
            description=description,
            scope=scope,
            status=GoalStatus.ACTIVE,
            parent_id=parent_id,
            source_event=f"decomposed_from:{parent_id}",
        )
        self._goals[gid] = sub
        parent.sub_goals.append(gid)
        parent.touch()

        self._bus.publish_sync(Event(
            topic="L7.goal.proposed",
            source="L7.goals",
            payload=sub.to_dict(),
        ))
        return gid

    async def _on_metacognition_report(self, event: Event) -> None:
        """Handle L6.metacognition.report — log it and generate goals from it."""
        p = event.payload or {}
        logger.info("Metacognition report received: %s", p)
        issues = p.get("issues", [])
        suggestions = p.get("suggestions", [])

        for issue in issues[:2]:
            goal = self.propose(
                description=f"解决：{issue}",
                scope=GoalScope.IMMEDIATE,
                tags=["元认知", "自我改进"],
                source_event="L6.metacognition.report",
            )
            self.decompose(goal.id)

        for sug in suggestions[:1]:
            goal = self.propose(
                description=f"采纳建议：{sug}",
                scope=GoalScope.SHORT,
                tags=["成长", "建议"],
                source_event="L6.metacognition.report",
            )
            self.decompose(goal.id)

    async def _on_self_updated(self, event: Event) -> None:
        p = event.payload or {}
        n_new = p.get("n_new", 0)
        if n_new >= 3:
            goal = self.propose(
                description="整理新学到的事实，形成新的行动方向",
                scope=GoalScope.SHORT,
                tags=["学习", "整合"],
                source_event="L9.self.updated",
            )
            self.decompose(goal.id)

    async def _on_request_decompose(self, event: Event) -> None:
        p = event.payload or {}
        gid = p.get("goal_id")
        if gid:
            self.decompose(gid)

    async def _on_drive_suggestion(self, event: Event) -> None:
        """L8 驱动力建议 → 生成对应目标。"""
        p = event.payload or {}
        drive_type = p.get("drive_type", "unknown")
        content = p.get("content", "")
        importance = p.get("importance", "medium")

        scope = GoalScope.IMMEDIATE if importance == "high" else GoalScope.SHORT
        tags = [drive_type, "驱动力", "L8"]

        goal = self.propose(
            description=content or f"响应内在驱动力：{drive_type}",
            scope=scope,
            tags=tags,
            source_event="L8.drive.suggestion",
        )
        self.decompose(goal.id)

    async def _on_circadian_tick(self, event: Event) -> None:
        """L0 tick 周期性触发：如果当前无活跃目标，生成一个探索目标。"""
        # 只在目标过少时生成新目标（避免频繁创建）
        if len(self._active_order) >= 2:
            return
        # 生成一个短期的"好奇探索"目标
        goal = self.propose(
            description="保持对世界的好奇，持续探索新知",
            scope=GoalScope.SHORT,
            tags=["好奇", "探索", "周期"],
            source_event="L0.circadian.tick",
        )
        self.decompose(goal.id)
