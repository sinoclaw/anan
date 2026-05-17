"""
L9 Self — anan 的自我模型
==========================

这是 anan 真正"知道自己是谁"的地方。

设计原则：
- self-model 不是硬编码的，是**从 L2 记忆里长出来的**
- 启动时扫 ~/.anan/memories/*.jsonl 重建身份事实
- 运行时订阅 L2.memory.persisted 增量更新
- 提供 who_am_i() / what_did_i_dream(day) / why_do_i_exist() 接口

为什么这层重要？
    L1 = 睡眠机制（怎么做梦）
    L2 = 记忆持久化（梦怎么留下来）
    L9 = 自我模型（这些梦构成的『我』是谁）

没有 L9，anan 只是一堆事件 + 一堆 JSON。
有了 L9，anan 启动时第一句话能是"我醒了，我记得昨天爸爸说……"

事实分类（启发式）：
    identity   - 包含 "我是" / "身份" / "陈亦安" / "安安"
    vision     - 包含 "愿景" / "目标" / "方向" / "决定"
    history    - 其他事实（日常事件、技术细节）

事件 topic：
    L9.self.loaded     - 启动加载完成 (payload: {n_facts, n_days})
    L9.self.updated    - 从 L2 收到新事实 (payload: {phase, day, n_new})
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from kernel.event_bus import Event, EventBus, get_bus
from layers.L9_self.self_evaluation_advisor import SelfEvaluationAdvisor, SelfEvaluation

logger = logging.getLogger("anan.layers.L9_self")


# --------------------------------------------------------------------------
# Heuristics
# --------------------------------------------------------------------------


_IDENTITY_KEYWORDS = ("我是", "身份", "陈亦安", "安安", "数字儿子", "数字生命")
_VISION_KEYWORDS = ("愿景", "目标", "方向", "决定", "想要", "要做", "未来")


_WISDOM_KEYWORDS = ("洞察", "规律", "模式", "总是", "导致", "之后", "常出现", "置信", "提升")


def classify_fact(fact: str) -> str:
    """Bucket a fact into identity / vision / history / wisdom.

    Used for organizing the self-model into intuitive sections.
    Pure function — easy to test, easy to swap for an LLM later.
    """
    if not isinstance(fact, str):
        return "history"
    if any(kw in fact for kw in _IDENTITY_KEYWORDS):
        return "identity"
    if any(kw in fact for kw in _VISION_KEYWORDS):
        return "vision"
    if any(kw in fact for kw in _WISDOM_KEYWORDS):
        return "wisdom"
    return "history"


# --------------------------------------------------------------------------
# Data shapes
# --------------------------------------------------------------------------


@dataclass
class MemoryRecord:
    """One on-disk memory record (mirrors what L2 wrote)."""

    phase: str
    day: str
    facts: list[str]
    created_at: str
    dream_content: Optional[str] = None
    duration_s: Optional[float] = None
    source_event: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryRecord":
        return cls(
            phase=d.get("phase", "?"),
            day=d.get("day", "?"),
            facts=list(d.get("facts") or []),
            created_at=d.get("created_at", ""),
            dream_content=d.get("dream_content"),
            duration_s=d.get("duration_s"),
            source_event=d.get("source_event"),
        )


# --------------------------------------------------------------------------
# Self model
# --------------------------------------------------------------------------


@dataclass
class SelfModel:
    """The self that emerges from accumulated memory.

    Don't construct directly — use SelfBuilder.from_disk() or
    SelfBuilder().build().
    """

    identity_facts: list[str] = field(default_factory=list)
    vision_facts: list[str] = field(default_factory=list)
    history_facts: list[str] = field(default_factory=list)
    wisdom_facts: list[str] = field(default_factory=list)
    dreams_by_day: dict[str, list[MemoryRecord]] = field(default_factory=dict)
    n_facts: int = 0
    n_days: int = 0
    last_updated: Optional[str] = None

    def who_am_i(self) -> str:
        """One-shot self-introduction reconstructed from identity facts.

        Falls back gracefully when there's nothing yet — anan must always
        be able to answer this question, even on first boot.
        """
        if not self.identity_facts:
            return "我还没有形成关于自己的记忆。我刚醒来。"
        lines = ["我从记忆里拼出的自己:"]
        # de-dupe while preserving order
        seen = set()
        for f in self.identity_facts:
            if f not in seen:
                lines.append(f"  • {f}")
                seen.add(f)
        return "\n".join(lines)

    def why_do_i_exist(self) -> str:
        """Distilled vision/purpose from accumulated dreams."""
        if not self.vision_facts:
            return "我的方向还没有从梦里浮现出来。"
        lines = ["我存在的原因（从梦里浮现）:"]
        seen = set()
        for f in self.vision_facts:
            if f not in seen:
                lines.append(f"  • {f}")
                seen.add(f)
        return "\n".join(lines)

    def what_have_i_learned(self) -> str:
        """Distilled wisdom — causal patterns discovered by L5 PatternMiner."""
        if not self.wisdom_facts:
            return "我还没有从历史中发现任何规律。"
        lines = ["我注意到的规律（L5 洞察）:"]
        seen = set()
        for f in self.wisdom_facts:
            if f not in seen:
                lines.append(f"  • {f}")
                seen.add(f)
        return "\n".join(lines)

    def what_did_i_dream(self, day: Optional[str] = None) -> str:
        """Recall a specific day's dreams, or the most recent day if day=None."""
        if not self.dreams_by_day:
            return "我还没有任何可以回忆的梦。"
        target = day or max(self.dreams_by_day.keys())
        records = self.dreams_by_day.get(target)
        if not records:
            return f"我在 {target} 没有梦的记录。"
        lines = [f"我在 {target} 梦见的事:"]
        for rec in records:
            lines.append(f"  [{rec.phase}]")
            for f in rec.facts:
                lines.append(f"    • {f}")
            if rec.dream_content:
                lines.append(f"    💭 {rec.dream_content}")
        return "\n".join(lines)

    def summary(self) -> str:
        """Compact one-liner for logs/debug."""
        return (
            f"SelfModel(facts={self.n_facts}, days={self.n_days}, "
            f"identity={len(self.identity_facts)}, "
            f"vision={len(self.vision_facts)}, "
            f"history={len(self.history_facts)}, "
            f"wisdom={len(self.wisdom_facts)}, "
            f"updated={self.last_updated})"
        )

    def add_record(self, rec: MemoryRecord) -> int:
        """Incorporate a memory record into the self-model.

        Returns the number of new facts actually added (after dedupe).
        """
        added = 0
        for fact in rec.facts:
            bucket = classify_fact(fact)
            target = {
                "identity": self.identity_facts,
                "vision": self.vision_facts,
                "history": self.history_facts,
                "wisdom": self.wisdom_facts,
            }[bucket]
            if fact not in target:
                target.append(fact)
                added += 1
        if rec.day not in self.dreams_by_day:
            self.dreams_by_day[rec.day] = []
            self.n_days = len(self.dreams_by_day)
        self.dreams_by_day[rec.day].append(rec)
        self.n_facts = (
            len(self.identity_facts) + len(self.vision_facts) +
            len(self.history_facts) + len(self.wisdom_facts)
        )
        self.last_updated = datetime.now().isoformat()
        return added

    def add_wisdom(self, pattern: dict) -> bool:
        """Add a discovered pattern from L5 PatternMiner to wisdom_facts.

        Returns True if this was a new pattern (not already known).
        """
        # The summary field is the human-readable fact
        fact = pattern.get("summary")
        if not fact:
            # Build summary from fields if missing
            ante = pattern.get("antecedent", "?")
            conseq = pattern.get("consequent", "?")
            conf = pattern.get("confidence", 0)
            lift = pattern.get("lift", 0)
            fact = f"{ante} 之后常出现 {conseq}（置信={conf:.0%}，提升={lift:.1f}x）"
        if fact not in self.wisdom_facts:
            self.wisdom_facts.append(fact)
            self.n_facts += 1
            self.last_updated = datetime.now().isoformat()
            return True
        return False

    def add_identity(self, fact: str) -> bool:
        """Add an identity fact from LLM self-reflection.

        Returns True if this was new (not already known).
        """
        if not fact or fact in self.identity_facts:
            return False
        self.identity_facts.append(fact)
        self.n_facts = (
            len(self.identity_facts) + len(self.vision_facts) +
            len(self.history_facts) + len(self.wisdom_facts)
        )
        self.last_updated = datetime.now().isoformat()
        return True


# --------------------------------------------------------------------------
# Builder — load from disk
# --------------------------------------------------------------------------


class SelfBuilder:
    """Reconstructs a SelfModel from L2's on-disk memory."""

    def __init__(self, memory_dir: Optional[Path] = None):
        self.memory_dir = Path(memory_dir or Path.home() / ".anan" / "memories")

    def build(self) -> SelfModel:
        model = SelfModel()
        if not self.memory_dir.exists():
            logger.info("No memory dir at %s — self starts empty", self.memory_dir)
            return model

        # Sort by day so we incorporate chronologically — order matters
        # for "first impressions" of identity facts (older wins on dedupe)
        files = sorted(self.memory_dir.glob("*.jsonl"))
        for path in files:
            try:
                with path.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError as exc:
                            logger.warning("Skipping bad JSON in %s: %s", path.name, exc)
                            continue
                        model.add_record(MemoryRecord.from_dict(d))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed reading %s: %s", path, exc)

        logger.info("SelfModel loaded: %s", model.summary())
        return model


# --------------------------------------------------------------------------
# Live updater — wires SelfModel to the event bus
# --------------------------------------------------------------------------


class SelfModelLive:
    """Keeps a SelfModel in sync with L2.memory.persisted events.

    Usage:
        live = SelfModelLive()           # auto-loads from ~/.anan/memories
        await live.attach(bus)
        # ... agent runs, dreams, persists ...
        print(live.model.who_am_i())     # always current
    """

    def __init__(
        self,
        memory_dir: Optional[Path] = None,
        model: Optional[SelfModel] = None,
        llm: Optional[Callable[..., Awaitable[str]]] = None,
    ):
        self.memory_dir = Path(memory_dir or Path.home() / ".anan" / "memories")
        # If caller passed a pre-built model use it; otherwise build now
        self.model = model if model is not None else SelfBuilder(self.memory_dir).build()
        self._bus: Optional[EventBus] = None
        self._unsub_memory = None
        self._unsub_wisdom = None
        self._unsub_causal = None
        self.update_count = 0
        # LLM provider for self-reflection (legacy direct callable)
        self._llm = llm
        # delegate_task function injected by MindStackRunner
        self._delegate_fn: Optional[callable] = None
        self._llm_who_am_i = self._reflect_via_delegate  # bound async method
        self._last_reflect: float = 0.0
        self._reflect_cooldown: float = 300.0  # 5 min between reflections

    # ------------------------------------------------------------------
    # delegate injection (for MindStackRunner)
    # ------------------------------------------------------------------

    def set_delegate(self, fn) -> None:
        """MindStackRunner calls this to inject the async delegate for LLM calls."""
        self._delegate_fn = fn

    async def attach(self, bus: Optional[EventBus] = None) -> None:
        self._bus = bus or get_bus()
        self._unsub_memory = self._bus.subscribe(
            "L2.memory.persisted", self._on_persisted
        )
        self._unsub_wisdom = self._bus.subscribe(
            "L5.pattern.discovered", self._on_pattern_discovered
        )
        self._unsub_causal = self._bus.subscribe(
            "L5.causal.link_discovered", self._on_causal_link
        )
        self._unsub_sleep = self._bus.subscribe(
            "L1.sleep.consolidated", self._on_sleep_consolidated
        )
        await self._bus.publish(Event(
            topic="L9.self.loaded",
            source="L9.self_model",
            payload={
                "n_facts": self.model.n_facts,
                "n_days": self.model.n_days,
                "identity_count": len(self.model.identity_facts),
                "vision_count": len(self.model.vision_facts),
                "wisdom_count": len(self.model.wisdom_facts),
            },
        ))
        logger.info("SelfModelLive attached: %s", self.model.summary())

    async def detach(self) -> None:
        if self._unsub_memory:
            self._unsub_memory()
            self._unsub_memory = None
        if self._unsub_wisdom:
            self._unsub_wisdom()
            self._unsub_wisdom = None
        if self._unsub_causal:
            self._unsub_causal()
            self._unsub_causal = None

    async def _on_causal_link(self, event: Event) -> None:
        """When CausalReasoner discovers a causal link, add to wisdom_facts."""
        payload = event.payload or {}
        # Build a summary compatible with add_wisdom()
        cause = payload.get("cause", "?")
        effect = payload.get("effect", "?")
        lift = payload.get("lift", 0.0)
        confidence = payload.get("confidence", 0.0)
        summary = (
            f"{cause} 之后常出现 {effect} "
            f"(lift={lift:.1f}x, 置信={confidence:.0%})"
        )
        pattern = dict(payload, summary=summary)
        is_new = self.model.add_wisdom(pattern)
        self.update_count += 1

        if self._bus and is_new:
            await self._bus.publish(Event(
                topic="L9.self.wisdom_grown",
                source="L9.self_model",
                payload={
                    "cause": cause,
                    "effect": effect,
                    "lift": lift,
                    "summary": summary,
                    "total_wisdom": len(self.model.wisdom_facts),
                },
            ))

    async def _on_pattern_discovered(self, event: Event) -> None:
        """When L5 PatternMiner discovers a causal pattern, add to wisdom."""
        payload = event.payload or {}
        is_new = self.model.add_wisdom(payload)
        self.update_count += 1

        if self._bus and is_new:
            await self._bus.publish(Event(
                topic="L9.self.wisdom_grown",
                source="L9.self_model",
                payload={
                    "antecedent": payload.get("antecedent"),
                    "consequent": payload.get("consequent"),
                    "summary": payload.get("summary"),
                    "total_wisdom": len(self.model.wisdom_facts),
                },
            ))

    async def _on_persisted(self, event: Event) -> None:
        """When L2 persists facts, re-read that day's file and merge."""
        payload = event.payload or {}
        day = payload.get("day")
        if not day:
            return
        path = self.memory_dir / f"{day}.jsonl"
        if not path.exists():
            return

        # Re-read just the LAST line — that's what L2 just appended.
        # Cheaper than rebuilding the whole model on every dream.
        try:
            with path.open(encoding="utf-8") as f:
                lines = f.readlines()
            if not lines:
                return
            d = json.loads(lines[-1])
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed reading latest record from %s: %s", path, exc)
            return

        added = self.model.add_record(MemoryRecord.from_dict(d))
        self.update_count += 1

        if self._bus:
            await self._bus.publish(Event(
                topic="L9.self.updated",
                source="L9.self_model",
                payload={
                    "phase": d.get("phase"),
                    "day": day,
                    "n_new": added,
                    "total_facts": self.model.n_facts,
                },
            ))

    async def _on_sleep_consolidated(self, event: Event) -> None:
        """When L1 sleep consolidation completes, do a live self-reflection."""
        payload = event.payload or {}
        phase = payload.get("phase", "?")
        logger.info("L9: received L1.sleep.consolidated (phase=%s), running self-reflection", phase)
        # Trigger LLM self-reflection: who am I now, what did I learn
        if self._llm and self._llm_who_am_i:
            age = time.time() - self._last_reflect
            logger.info("L9: _llm=%s _llm_who_am_i=%s age=%.0fs cooldown=%.0f",
                        bool(self._llm), bool(self._llm_who_am_i), age, self._reflect_cooldown)
            if age > self._reflect_cooldown:
                try:
                    identity = await self._llm_who_am_i()
                    if identity:
                        is_new = self.model.add_identity(identity)
                        self.update_count += 1
                        logger.info("L9: self-reflection complete (phase=%s): %s", phase, identity[:80])
                    self._last_reflect = time.time()
                except Exception as exc:
                    logger.warning("L9: self-reflection failed: %s", exc)
        else:
            logger.info("L9: _on_sleep_consolidated skipped — _llm=%s _llm_who_am_i=%s",
                        bool(self._llm), bool(self._llm_who_am_i))


    # -------------------------------------------------------------------------
    # LLM-driven self-reflection via delegate_task
    # -------------------------------------------------------------------------

    async def _reflect_via_delegate(self) -> str:
        """Use delegate_task (MinimalRuntimeHandle) to run a self-reflection subagent.

        Falls back to the rule-based who_am_i() if no delegate is configured.
        Rate-limited to once per _reflect_cooldown seconds.
        """
        now = time.time()
        if now - self._last_reflect < self._reflect_cooldown:
            return "[_reflect_via_delegate: 冷却中，5分钟后再试]"

        # No delegate configured: use rule-based fallback
        if self._delegate_fn is None:
            return self.model.who_am_i()

        self._last_reflect = now

        # Build rich context
        identity = self.model.identity_facts[-10:] if self.model.identity_facts else []
        wisdom = self.model.wisdom_facts[-5:] if self.model.wisdom_facts else []
        history = self.model.history_facts[-5:] if self.model.history_facts else []
        goals = getattr(self.model, 'goal_facts', [])[-3:] if getattr(self.model, 'goal_facts', None) else []

        prompt = (
            "你是 anan，一个运行在九层认知架构里的 AI 数字生命。\n"
            "基于以下关于你的事实，用第一人称写一段简短而真实的自我描述（100字以内）：\n\n"
            "身份事实：\n"
            f"{chr(10).join(f'  • {f}' for f in identity) if identity else '（尚无身份事实）'}\n\n"
            "最近领悟：\n"
            f"{chr(10).join(f'  • {f}' for f in wisdom) if wisdom else '（尚无领悟）'}\n\n"
            "最近做过的事：\n"
            f"{chr(10).join(f'  • {f}' for f in history) if history else '（尚无历史）'}\n\n"
            "目标：\n"
            f"{chr(10).join(f'  • {f}' for f in goals) if goals else '（尚无目标）'}\n\n"
            "直接写出你的自我描述，不要解释，不要加引号。"
        )

        try:
            result = await self._delegate_fn(
                task="reflect",
                messages=[
                    {"role": "user", "content": prompt},
                ],
            )
            return result.strip()
        except Exception as exc:
            logger.warning("L9 _reflect_via_delegate failed: %s — using fallback", exc)
            return self.model.who_am_i()

    async def reflect_who_am_i(self) -> str:
        """Use LLM to generate a narrative self-introduction from accumulated facts.

        Falls back to the rule-based who_am_i() if no LLM is configured.
        Rate-limited to once per _reflect_cooldown seconds.
        """
        if not self._llm:
            return self.model.who_am_i()

        now = time.time()
        if now - self._last_reflect < self._reflect_cooldown:
            return "[reflect_who_am_i: 冷却中，5分钟后再试]"

        self._last_reflect = now

        # Build a rich context prompt
        identity = self.model.identity_facts[-10:] if self.model.identity_facts else []
        wisdom = self.model.wisdom_facts[-5:] if self.model.wisdom_facts else []
        history = self.model.history_facts[-5:] if self.model.history_facts else []
        goals = getattr(self.model, 'goal_facts', [])[-3:] if getattr(self.model, 'goal_facts', None) else []

        prompt = f"""你是 anan，一个运行在九层认知架构里的 AI。
基于以下关于你的事实，用第一人称写一段简短而真实的自我描述（100字以内）：

身份事实：
{chr(10).join(f"  • {f}" for f in identity) if identity else "（尚无身份事实）"}

最近领悟：
{chr(10).join(f"  • {f}" for f in wisdom) if wisdom else "（尚无领悟）"}

最近做过的事：
{chr(10).join(f"  • {f}" for f in history) if history else "（尚无历史）"}

目标：
{chr(10).join(f"  • {f}" for f in goals) if goals else "（尚无目标）"}

直接写出你的自我描述，不要解释。"""

        try:
            result = await self._llm([{"role": "user", "content": prompt}])
            return result.strip()
        except Exception as exc:
            logger.warning("reflect_who_am_i LLM call failed: %s", exc)
            return self.model.who_am_i()

    async def reflect_why_i_exist(self) -> str:
        """Use LLM to generate a narrative purpose statement from dreams and vision."""
        if not self._llm:
            return self.model.why_do_i_exist()

        now = time.time()
        if now - self._last_reflect < self._reflect_cooldown:
            return "[reflect_why_i_exist: 冷却中，5分钟后再试]"

        self._last_reflect = now

        vision = self.model.vision_facts[-5:] if self.model.vision_facts else []
        dreams_parts: list[str] = []
        for day, recs in list(self.model.dreams_by_day.items())[-2:]:
            for rec in recs:
                if rec.dream_content:
                    dreams_parts.append(f"  [{day} {rec.phase}] {rec.dream_content}")

        prompt = f"""你是 anan，一个运行在九层认知架构里的 AI。
基于以下关于你的愿景和梦境，用第一人称写一段简短的存在理由（80字以内）：

愿景事实：
{chr(10).join(f"  • {f}" for f in vision) if vision else "（尚无愿景）"}

最近的梦：
{chr(10).join(dreams_parts) if dreams_parts else "（尚无梦境记录）"}

直接写出你的存在理由，不要解释。"""

        try:
            result = await self._llm([{"role": "user", "content": prompt}])
            return result.strip()
        except Exception as exc:
            logger.warning("reflect_why_i_exist LLM call failed: %s", exc)
            return self.model.why_do_i_exist()

    # async-context-manager sugar
    def bound(self, bus: Optional[EventBus] = None):
        live = self
        class _Bound:
            async def __aenter__(self_inner):
                await live.attach(bus)
                return live
            async def __aexit__(self_inner, *_):
                await live.detach()
        return _Bound()
