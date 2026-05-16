"""
L7 Goals — 目标生成引擎
=========================
LLM-driven goal generation, decomposition, conflict resolution and selection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

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
    # LLM-driven fields
    llm_score: float = 0.0       # 0.0-1.0, populated by choose_next_goal
    rationale: Optional[str] = None  # 为什么选这个目标

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
            "llm_score": round(self.llm_score, 3),
            "rationale": self.rationale,
        }


# ---------------------------------------------------------------------------
# LLM Prompt Templates
# ---------------------------------------------------------------------------

GOAL_GENERATION_PROMPT = """你是一个目标生成助手。基于以下上下文，为 AI agent "anan" 生成 3-5 个具体、可执行的目标。

上下文：
{context}

要求：
- 每个目标要有清晰的描述和合适的范围 (immediate/short/medium/long)
- 目标应该具体、可衡量、可实现
- 同时给出每个目标的主要标签（用于冲突检测）
- 如果没有足够信息，返回空列表 []

返回格式（严格 JSON）：
{{
  "goals": [
    {{
      "description": "目标描述",
      "scope": "immediate|short|medium|long",
      "tags": ["标签1", "标签2"]
    }}
  ]
}}
"""


GOAL_DECOMPOSITION_PROMPT = """将以下目标分解为 3-7 个可执行的子目标。

目标：{goal_description}
范围：{scope}

要求：
- 子目标应该是具体、可操作的下一步行动
- 分解要符合目标范围：immediate 目标只需今天行动；short 目标分解为本周和本月；medium 目标分解为本周、本月和季度；long 目标分解为月度计划 + 短期子目标
- 每个子目标要有描述和估算时间范围

返回格式（严格 JSON）：
{{
  "sub_goals": [
    {{
      "description": "子目标描述",
      "scope": "immediate|short|medium|long",
      "action_type": "immediate|this_week|this_month|quarterly"
    }}
  ]
}}
"""


CONFLICT_RESOLUTION_PROMPT = """分析以下目标之间的冲突，并给出解决建议。

目标A：{goal_a_description} (范围: {scope_a}, 标签: {tags_a})
目标B：{goal_b_description} (范围: {scope_b}, 标签: {tags_b})

冲突标签重叠：{overlap_tags}

请判断：
1. 这两个目标是否真的冲突（资源、注意力、时间上互斥）？
2. 如果冲突，应该保留哪个、修改哪个或放弃哪个？
3. 如果不冲突，说明原因。

返回格式（严格 JSON）：
{{
  "is_conflict": true|false,
  "resolution": "keep_a|keep_b|modify_a|modify_b|abandon_a|abandon_b|merge|none",
  "reason": "解释原因",
  "suggested_modification": "如果需要修改，描述修改后的目标（如果有）"
}}
"""


GOAL_SCORING_PROMPT = """为以下每个目标打分（0.0-1.0），决定下一个应该追求哪个。

当前时间：{current_time}
目标列表：
{goal_list}

评分标准（重要性 x 紧急度 x 可行性）：
- 重要性：这个目标对anan的长期发展和用户价值有多大？
- 紧急度：时间敏感性多高？
- 可行性：当前资源和技术条件下能完成吗？
- 协同效应：完成后对其他目标有帮助吗？

返回格式（严格 JSON）：
{{
  "scores": [
    {{
      "goal_id": "goal-0",
      "score": 0.85,
      "rationale": "为什么这个分数"
    }}
  ]
}}
"""


# ---------------------------------------------------------------------------
# LLM Provider Interface
# ---------------------------------------------------------------------------

# LLMProvider = Callable[[list[dict], float] -> Awaitable[str]]
# Messages format: [{"role": "user"|"assistant"|"system", "content": "..."}]
# Returns: raw model response string


# ---------------------------------------------------------------------------
# GoalGenerator
# ---------------------------------------------------------------------------

class GoalGenerator:
    """L7 — 目标生成 + 分解 + 冲突检测 + LLM驱动"""

    MAX_ACTIVE_GOALS = 10
    MAX_SUBGOALS_PER_GOAL = 5

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        # LLM provider: async fn(messages, temperature=0.3) -> str response
        # If None, falls back to rule-based behaviour
        llm: Optional[Callable[[list[dict[str, str]], float], Awaitable[str]]] = None,
        self_model: Optional[object] = None,
    ):
        self._bus = bus or get_bus()
        self._llm = llm
        self._self_model = self_model
        self._goals: dict[str, Goal] = {}
        self._active_order: deque[str] = deque(maxlen=self.MAX_ACTIVE_GOALS)
        self._id_counter: int = 0
        self._unsubs: list = []
        self._pending: list = []  # placeholder — pending actions live in SelfTuner, not GoalGenerator
        # Rate-limit LLM calls: minimum seconds between calls
        self._last_llm_call: float = 0.0
        self._llm_cooldown: float = 5.0  # seconds

    # -------------------------------------------------------------------------
    # LLM-driven public API
    # -------------------------------------------------------------------------

    async def generate_goals_from_context(self, prompt: str) -> list[Goal]:
        """用 LLM 从当前上下文生成目标建议。

        Args:
            prompt: 描述当前状态、对话或情境的文本

        Returns:
            新创建的 Goal 列表（已插入管理队列并发布 L7.goal.created 事件）
        """
        if not self._llm:
            logger.warning("generate_goals_from_context called with no LLM provider; skipping")
            return []

        try:
            text = await self._call_llm(GOAL_GENERATION_PROMPT.format(context=prompt))
            data = self._extract_json(text)
            goals_raw = (data or {}).get("goals", [])
        except Exception as exc:
            logger.error("generate_goals_from_context LLM call failed: %s", exc)
            return []

        created = []
        for g in goals_raw:
            scope_str = g.get("scope", "short")
            try:
                scope = GoalScope(scope_str)
            except ValueError:
                scope = GoalScope.SHORT

            goal = self._create_goal(
                description=g.get("description", "").strip(),
                scope=scope,
                tags=g.get("tags", []),
                source_event="L7.goal.created",
            )
            if goal:
                created.append(goal)

        if created:
            logger.info("LLM generated %d goals from context", len(created))
        return created

    async def decompose_goal_llm(self, goal: Goal) -> list[SubGoal]:
        """用 LLM 将大目标分解为可执行的子目标（返回 SubGoal dataclass 列表）。

        子目标会被附加到 goal.sub_goals，并发布 L7.goal.decomposed 事件。
        """
        if not self._llm:
            logger.warning("decompose_goal_llm called with no LLM provider; skipping")
            return []

        try:
            text = await self._call_llm(GOAL_DECOMPOSITION_PROMPT.format(
                goal_description=goal.description,
                scope=goal.scope.value,
            ))
            data = self._extract_json(text)
            subs_raw = (data or {}).get("sub_goals", [])
        except Exception as exc:
            logger.error("decompose_goal_llm LLM call failed: %s", exc)
            return []

        created = []
        for sg in subs_raw:
            scope_str = sg.get("scope", "short")
            try:
                scope = GoalScope(scope_str)
            except ValueError:
                scope = GoalScope.SHORT

            sub_goal = SubGoal(
                description=sg.get("description", "").strip(),
                scope=scope,
                action_type=sg.get("action_type", "immediate"),
            )
            created.append(sub_goal)

        # Attach to parent goal
        if created and goal:
            goal.sub_goals.extend([sg.description for sg in created])
            goal.touch()
            self._bus.publish_sync(Event(
                topic="L7.goal.decomposed",
                source="L7.goals",
                payload={
                    "goal_id": goal.id,
                    "sub_goals": [sg.description for sg in created],
                    "sub_goal_details": [sg.to_dict() for sg in created],
                },
            ))

        logger.info("LLM decomposed goal %s into %d sub-goals", goal.id, len(created))
        return created

    async def resolve_conflicts_llm(self, goals: list[Goal]) -> list[Goal]:
        """用 LLM 判断目标冲突并解决。

        分析传入的目标列表，识别冲突对，返回建议保留/修改/放弃的目标列表。
        实际修改会通过事件发布，由外部系统执行。
        """
        if not self._llm:
            logger.warning("resolve_conflicts_llm called with no LLM provider; skipping")
            return goals

        if len(goals) < 2:
            return goals

        # Build conflict pairs
        pairs = []
        for i, g in enumerate(goals):
            for j, h in enumerate(goals):
                if i >= j:
                    continue
                overlap = set(g.tags) & set(h.tags)
                if overlap:
                    pairs.append((g, h, list(overlap)))

        if not pairs:
            return goals

        resolutions = []
        for g, h, overlap in pairs:
            try:
                text = await self._call_llm(CONFLICT_RESOLUTION_PROMPT.format(
                    goal_a_description=g.description,
                    scope_a=g.scope.value,
                    tags_a=", ".join(g.tags),
                    goal_b_description=h.description,
                    scope_b=h.scope.value,
                    tags_b=", ".join(h.tags),
                    overlap_tags=", ".join(overlap),
                ))
                data = self._extract_json(text)
                resolution = data.get("resolution", "none") if data else "none"
                resolutions.append({
                    "goal_a": g.id,
                    "goal_b": h.id,
                    "resolution": resolution,
                    "reason": data.get("reason", "") if data else "",
                    "suggested_modification": data.get("suggested_modification", "") if data else "",
                })
            except Exception as exc:
                logger.error("resolve_conflicts_llm LLM call failed for pair %s/%s: %s", g.id, h.id, exc)

        # Publish conflict resolution event
        if resolutions:
            self._bus.publish_sync(Event(
                topic="L7.goal.conflict_resolved",
                source="L7.goals",
                payload={
                    "resolutions": resolutions,
                    "total_pairs": len(pairs),
                },
            ))

        # Determine surviving goals based on resolutions
        keep_ids: set[str] = {g.id for g in goals}
        for res in resolutions:
            if res["resolution"] in ("abandon_a", "keep_b"):
                keep_ids.discard(res["goal_a"])
            if res["resolution"] in ("abandon_b", "keep_a"):
                keep_ids.discard(res["goal_b"])

        surviving = [g for g in goals if g.id in keep_ids]
        logger.info("Conflict resolution: %d/%d goals kept", len(surviving), len(goals))
        return surviving

    async def choose_next_goal(self, goals: list[Goal]) -> Optional[Goal]:
        """基于 LLM 评分选择下一个目标。

        返回得分最高的目标，并将其 llm_score 和 rationale 字段填充。
        """
        if not goals:
            return None

        if not self._llm:
            # Fallback: use rule-based scoring
            return self._choose_next_rule_based(goals)

        if len(goals) == 1:
            goals[0].llm_score = 1.0
            goals[0].rationale = "only_goal"
            return goals[0]

        current_time = datetime.now().isoformat()
        goal_list = "\n".join(
            f'- id={g.id}, description="{g.description}", scope={g.scope.value}, '
            f'tags={g.tags}, priority_boost={g.priority_boost:.2f}'
            for g in goals
        )

        try:
            text = await self._call_llm(GOAL_SCORING_PROMPT.format(
                current_time=current_time,
                goal_list=goal_list,
            ))
            data = self._extract_json(text)
            scores_raw = (data or {}).get("scores", [])
        except Exception as exc:
            logger.error("choose_next_goal LLM call failed: %s", exc)
            return self._choose_next_rule_based(goals)

        # Apply scores
        score_map: dict[str, float] = {}
        rationale_map: dict[str, str] = {}
        for entry in scores_raw:
            gid = entry.get("goal_id", "")
            score_map[gid] = max(0.0, min(1.0, float(entry.get("score", 0.5))))
            rationale_map[gid] = entry.get("rationale", "")

        for g in goals:
            g.llm_score = score_map.get(g.id, 0.5)
            g.rationale = rationale_map.get(g.id, "")

        best = max(goals, key=lambda g: g.llm_score)
        logger.info("Chose next goal: %s (score=%.3f) — %s", best.id, best.llm_score, best.rationale)

        self._bus.publish_sync(Event(
            topic="L7.goal.chosen",
            source="L7.goals",
            payload={"goal_id": best.id, "score": best.llm_score, "rationale": best.rationale},
        ))
        return best

    # -------------------------------------------------------------------------
    # Rule-based fallback
    # -------------------------------------------------------------------------

    def _choose_next_rule_based(self, goals: list[Goal]) -> Optional[Goal]:
        """Simple rule-based selection when no LLM is available."""
        scope_order = {
            GoalScope.IMMEDIATE: 0,
            GoalScope.SHORT: 1,
            GoalScope.MEDIUM: 2,
            GoalScope.LONG: 3,
        }
        scored = [
            (g, g.priority_boost - scope_order[g.scope] * 0.1)
            for g in goals
            if g.status == GoalStatus.ACTIVE
        ]
        if not scored:
            return None
        scored.sort(key=lambda x: x[1], reverse=True)
        best = scored[0][0]
        best.llm_score = scored[0][1]
        best.rationale = "rule_based_fallback"
        return best

    # -------------------------------------------------------------------------
    # LLM Helper
    # -------------------------------------------------------------------------

    async def _call_llm(self, prompt: str, temperature: float = 0.3) -> str:
        """Call the LLM with rate limiting."""
        now = time.time()
        if now - self._last_llm_call < self._llm_cooldown:
            wait = self._llm_cooldown - (now - self._last_llm_call)
            await asyncio.sleep(wait)

        messages = [{"role": "user", "content": prompt}]
        self._last_llm_call = time.time()
        return await self._llm(messages, temperature=temperature)

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        """Extract JSON from LLM response, trying multiple strategies."""
        # Strategy 1: find ```json ... ``` block
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # Strategy 2: find raw {...}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        logger.warning("Could not extract JSON from LLM response: %s", text[:200])
        return None

    # -------------------------------------------------------------------------
    # Goal lifecycle (unchanged from original)
    # -------------------------------------------------------------------------

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
        self._unsubs.append(
            self._bus.subscribe("L8.drive.suggestion", self._on_drive_suggestion)
        )
        self._unsubs.append(
            self._bus.subscribe("L0.circadian.tick", self._on_circadian_tick)
        )
        logger.info("GoalGenerator attached (LLM=%s)", "yes" if self._llm else "no")

    async def detach(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()

    def _create_goal(
        self,
        description: str,
        scope: GoalScope,
        *,
        tags: Optional[list[str]] = None,
        source_event: Optional[str] = None,
    ) -> Optional[Goal]:
        """Internal goal factory — creates, registers, and publishes events."""
        if not description.strip():
            return None

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
            topic="L7.goal.created",
            source="L7.goals",
            payload=goal.to_dict(),
        ))
        self._bus.publish_sync(Event(
            topic="L7.goal.proposed",
            source="L7.goals",
            payload=goal.to_dict(),
        ))
        logger.info("Goal created (LLM): [%s] %s", scope.value, description)
        return goal

    def propose(
        self,
        description: str,
        scope: GoalScope,
        *,
        tags: Optional[list[str]] = None,
        source_event: Optional[str] = None,
    ) -> Goal:
        """Create and activate a goal (rule-based path)."""
        goal = self._create_goal(description, scope, tags=tags, source_event=source_event)
        if goal:
            return goal
        raise ValueError(f"propose failed: empty description")

    def decompose(self, goal_id: str) -> list[str]:
        """Rule-based decompose (original implementation)."""
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
        self._bus.publish_sync(Event(
            topic="L7.goal.created",
            source="L7.goals",
            payload=sub.to_dict(),
        ))
        return gid

    # -------------------------------------------------------------------------
    # Event handlers
    # -------------------------------------------------------------------------

    async def _on_metacognition_report(self, event: Event) -> None:
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
        # 已有太多活跃目标，跳过
        if len(self._active_order) >= 2:
            return

        # 构建真实 context（不用虚假叙事）
        context_parts = []
        active = self.active_goals()
        if active:
            goal_list = "; ".join(f"{g.description[:40]}({g.scope.value})" for g in active[:3])
            context_parts.append(f"当前活跃目标：{goal_list}")

        if self._pending:
            pending_list = "; ".join(f"{a.target}={a.new_value:.2f}" for a in self._pending[:3])
            context_parts.append(f"待审批调参：{pending_list}")

        if self._self_model is not None:
            wisdom = getattr(self._self_model, "wisdom_facts", []) or []
            if wisdom:
                context_parts.append(f"近期规律：{'; '.join(wisdom[-3:])}")

        context = "\n".join(context_parts) if context_parts else "系统正常运行，无特殊状态"

        # 用 LLM 生成有依据的目标（避免虚假自我叙事）
        goals = await self.generate_goals_from_context(
            prompt=f"AI agent 状态报告：\n{context}\n\n基于以上真实状态，生成 1-2 个具体、可执行的目标。不要编造不存在的信息。"
        )

        if goals:
            for g in goals:
                self.decompose(g.id)
        else:
            # fallback：只有没有任何目标时才提一个通用探索目标
            goal = self.propose(
                description="保持对世界的好奇，持续探索新知",
                scope=GoalScope.SHORT,
                tags=["好奇", "探索", "周期"],
                source_event="L0.circadian.tick",
            )
            self.decompose(goal.id)


# ---------------------------------------------------------------------------
# SubGoal (returned by decompose_goal_llm)
# ---------------------------------------------------------------------------

@dataclass
class SubGoal:
    description: str
    scope: GoalScope
    action_type: str = "immediate"  # immediate | this_week | this_month | quarterly

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "scope": self.scope.value,
            "action_type": self.action_type,
        }


# ---------------------------------------------------------------------------
# Backward-compatibility alias
# ---------------------------------------------------------------------------

GoalEngine = GoalGenerator
