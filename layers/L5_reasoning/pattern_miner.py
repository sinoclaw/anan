"""
L5 PatternMiner — 推理 / 洞察 (Insight)
=========================================

L4 求证当下, L5 看历史. anan 第一次能问:
  "X 是不是总跟着 Y?" "为什么 L9 总占主导?" "我什么时候最容易梦到 vision?"

设计:
  - 滑动窗口扫 bus history, 提取 (antecedent → consequent) topic 共现
  - 抽象到 topic 段 (L9.self.updated → L9.self.* → L9.*) 让规律泛化
  - 三个统计: support (共现次数) / confidence (P(Y|X)) / lift (高于随机)
  - 阈值满 (support >= min_support, confidence >= min_confidence) → 发现 pattern
  - 发 L5.pattern.discovered → L9 收为 wisdom_facts ("我注意到的规律")
  - 去重: 同 (X, Y) 在冷却期内只发一次

不直接做事 — 只发洞察, 让上层决定怎么用.
异常隔离 — 挖掘失败只 log.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from kernel.event_bus import Event, EventBus, get_bus
from layers.L5_reasoning.mining_quality_advisor import MiningQualityAdvisor, MiningDecision

logger = logging.getLogger("anan.L5.miner")
_gateway_logger = logging.getLogger("gateway.builtin.mind_stack")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Pattern:
    """A discovered (X → Y within window) co-occurrence rule."""
    antecedent: str   # topic pattern (e.g. "L7.regulator.acted")
    consequent: str   # topic pattern (e.g. "L8.intent.proposed")
    support: int      # times X→Y observed
    confidence: float # P(Y in window | X happened) ∈ [0,1]
    lift: float       # confidence / baseline P(Y) — 1.0 means no signal


@dataclass
class _Discovered:
    pattern: Pattern
    last_emitted_at: datetime


# ---------------------------------------------------------------------------
# Miner
# ---------------------------------------------------------------------------

class PatternMiner:
    """Mine pattern rules from bus history.

    Wiring:
        l5 = PatternMiner(bus=bus, window=5, min_support=2, min_confidence=0.6)
        await l5.attach()
        # Periodically (e.g. on L0.circadian.bedtime):
        patterns = await l5.mine_now()
    """

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        window: int = 5,
        min_support: int = 2,
        min_confidence: float = 0.6,
        min_lift: float = 1.5,
        cooldown_s: float = 30.0,
        topic_abstractor: Optional[Callable[[str], str]] = None,
        history_limit: int = 500,
        mine_on_event: Optional[str] = "L0.circadian.bedtime",
        self_model: Optional[object] = None,   # L9 SelfModel — optional
        min_interval_std_s: float = 1.0,   # Filter periodic antecedents (e.g. ticks)
        min_occurrences_for_periodic_check: int = 4,   # Need ≥N intervals before checking periodicity
    ):
        self._bus = bus or get_bus()
        self._window = window
        self._min_support = min_support
        self._min_confidence = min_confidence
        self._min_lift = min_lift
        self._cooldown = timedelta(seconds=cooldown_s)
        self._abstract = topic_abstractor or self._default_abstract
        self._history_limit = history_limit
        self._mine_on_event = mine_on_event
        self._sm = self_model
        self._min_interval_std_s = min_interval_std_s
        self._min_occurrences_for_periodic_check = min_occurrences_for_periodic_check

        # Mining quality advisor: subagent for dynamic threshold adjustment
        self._quality_advisor = MiningQualityAdvisor()

        self._discovered: dict[tuple[str, str], _Discovered] = {}
        self._unsubs: list[Callable[[], None]] = []
        self._mine_count = 0
        self._active: bool = False

    def set_delegate(self, fn: callable) -> None:
        """Inject delegate_task for MiningQualityAdvisor subagent calls."""
        self._quality_advisor.set_delegate(fn)

    # Class-level shared storage — all instances write here, agent:end reads here.
    # Solves the _layers_ref overwrite problem: multiple MindStackRunner instances
    # can exist across restarts, but the latest挖掘结果 survive instance death.
    _last_patterns: list["Pattern"] = []

    @property
    def is_attached(self) -> bool:
        return self._active

    # ------------------------------------------------------------------
    def _default_abstract(self, topic: str) -> str:
        """Drop the deepest segment so L9.self.updated → L9.self.*"""
        parts = topic.split(".")
        if len(parts) <= 1:
            return topic
        return ".".join(parts[:-1]) + ".*"

    # ------------------------------------------------------------------
    async def attach(self) -> None:
        if self._active:
            return
        self._active = True
        if self._mine_on_event:
            async def on_trigger(event: Event):
                await self.mine_now()
            self._unsubs.append(
                self._bus.subscribe(self._mine_on_event, on_trigger)
            )

    async def detach(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        self._active = False

    # ------------------------------------------------------------------
    async def mine_now(self) -> list[Pattern]:
        """Scan bus history, return newly-discovered patterns (after cooldown)."""
        self._mine_count += 1
        try:
            patterns = await self._mine_now_impl()
            # Write to class-level shared storage so agent:end (potentially a
            # different MindStackRunner instance) can read the latest results.
            PatternMiner._last_patterns = patterns
            return patterns
        except Exception as exc:  # noqa: BLE001
            # Catch everything so exceptions cannot propagate to asyncio.gather
            # in event_bus.publish() — an unhandled exception in one handler
            # cancels all siblings via gather(return_exceptions=False).
            logger.debug("L5 mine_now failed (non-fatal): %s", exc)
            return []

    async def _assess_quality(self, patterns: list[Pattern]) -> None:
        """Post-mine quality check: ask advisor whether thresholds need adjustment."""
        try:
            current = {
                "min_support": self._min_support,
                "min_confidence": self._min_confidence,
                "min_lift": self._min_lift,
            }
            decision = await self._quality_advisor.assess(
                patterns_found=len(patterns),
                total_events=len(self._bus.history(limit=self._history_limit)),
                current_thresholds=current,
            )
            if not decision.recommend_adjust or decision.adjust_direction == "keep":
                return

            # Apply advisor-suggested thresholds
            changed = False
            if decision.min_lift is not None and decision.min_lift != self._min_lift:
                self._min_lift = decision.min_lift
                changed = True
                logger.info("MiningQualityAdvisor: min_lift → %.2f (%s)", decision.min_lift, decision.reasoning)
            if decision.min_confidence is not None and decision.min_confidence != self._min_confidence:
                self._min_confidence = decision.min_confidence
                changed = True
                logger.info("MiningQualityAdvisor: min_confidence → %.2f (%s)", decision.min_confidence, decision.reasoning)

            # Re-mine with new thresholds
            if changed:
                asyncio.create_task(self.mine_now())
        except Exception as exc:  # noqa: BLE001
            logger.debug("L5 quality assessment failed (non-fatal): %s", exc)

    async def _mine_now_impl(self) -> list[Pattern]:
        """Internal implementation — all exceptions are contained in mine_now()."""
        try:
            history = self._bus.history(limit=self._history_limit)
        except Exception as exc:  # noqa: BLE001
            logger.debug("L5 history fetch failed: %s", exc)
            return []
        if len(history) < self._min_support * 2:
            return []

        # Abstract and filter — exclude L5 self-events and infrastructure noise
        filtered_indices_set: set[int] = {
            i for i, e in enumerate(history)
            if not self._abstract(e.topic).startswith("L5.")
            and self._abstract(e.topic) not in (
                "session.*", "conversation.*",
                "gateway.message.*", "gateway.presence.*", "gateway.typing.*",
            )
        }
        filtered_topics_set: set[str] = {e.topic for i, e in enumerate(history) if i in filtered_indices_set}
        topics = [self._abstract(e.topic) for i, e in enumerate(history) if i in filtered_indices_set]

        # Count baselines
        topic_counts = Counter(topics)
        total = len(topics)
        if total == 0:
            return []

        # Detect periodic antecedents BEFORE abstraction, per raw topic.
        # This correctly identifies tick-like signals (e.g. L0.circadian.tick
        # firing at ~30s interval) even when multiple raw topics share the same
        # abstract prefix (e.g. L0.circadian.* = L0.circadian.tick ∪ L0.circadian.bedtime).
        import statistics
        raw_ante_intervals: dict[str, list[float]] = defaultdict(list)
        prev_ts_raw: dict[str, float] = {}
        for e in history:
            if e.topic not in filtered_topics_set:
                continue
            raw = e.topic
            if prev_ts_raw.get(raw) is not None:
                interval = e.ts - prev_ts_raw[raw]
                if interval > 0:
                    raw_ante_intervals[raw].append(interval)
            prev_ts_raw[raw] = e.ts
        periodic_raw = {
            raw for raw, intervals in raw_ante_intervals.items()
            if (len(intervals) >= self._min_occurrences_for_periodic_check
                and statistics.stdev(intervals) < self._min_interval_std_s)
        }
        if periodic_raw:
            logger.info("[MINER] filtering periodic raw topics (std < %.1fs, n≥%d): %s",
                         self._min_interval_std_s, self._min_occurrences_for_periodic_check, periodic_raw)

        # Compute abstract-level periodic set for co-occurrence filtering.
        # A wildcard antecedent (e.g. L0.circadian.*) is excluded if ANY of its
        # constituent raw topics was periodic — otherwise patterns from that
        # abstract would leak through when mixed intervals mask periodicity.
        periodic_abstracts: set[str] = set()
        if periodic_raw:
            abstract_to_raws: dict[str, set[str]] = defaultdict(set)
            for e in history:
                if e.topic not in filtered_topics_set:
                    continue
                abstract = self._abstract(e.topic)
                abstract_to_raws[abstract].add(e.topic)
            periodic_abstracts = {
                abstract for abstract, raws in abstract_to_raws.items()
                if raws & periodic_raw  # any constituent raw is periodic
            }
        # Hardcoded fallback: always filter these known periodic topics, even before
        # enough history accumulates for dynamic stdev detection. Prevents spurious
        # correlations from two periodic signals aligning (e.g. L0.circadian.tick at
        # ~30s and L8.drive.dormant at ~30s → high-lift false causal link).
        KNOWN_PERIODIC: set[str] = {"L0.circadian.*", "L8.drive.*"}
        periodic_abstracts |= KNOWN_PERIODIC
        if periodic_abstracts:
            logger.info("[MINER] filtering periodic abstracts (std < %.1fs): %s", self._min_interval_std_s, periodic_abstracts)

        # Count co-occurrences (X at i, Y at i+1..i+window, Y != X)
        co_counts: dict[tuple[str, str], int] = defaultdict(int)
        antecedent_counts: dict[str, int] = defaultdict(int)
        for i, x in enumerate(topics):
            antecedent_counts[x] += 1
            seen_in_window: set[str] = set()
            for j in range(i + 1, min(i + 1 + self._window, len(topics))):
                y = topics[j]
                if y == x or y in seen_in_window:
                    continue
                seen_in_window.add(y)
                co_counts[(x, y)] += 1
            # Yield event loop every 10 iterations to prevent blocking.
            # During heavy history (500+ events), this keeps the loop responsive
            # so inbound gateway messages are not stalled.
            if i % 10 == 0:
                await asyncio.sleep(0)

        new_patterns: list[Pattern] = []
        now = datetime.now()
        for (x, y), support in co_counts.items():
            if support < self._min_support:
                continue
            # Filter spurious patterns where both antecedent AND consequent are periodic.
            # Two independent periodic signals can align by chance (e.g. 30s tick and
            # 30s drive-update), producing a high-lift co-occurrence that is not causal.
            if x in periodic_abstracts and y in periodic_abstracts:
                logger.debug("[MINER] filtered periodic↔periodic: %s → %s (both periodic)", x, y)
                continue
            # Filter pure periodic antecedents (single periodic source, no co-occurrence claim)
            if x in periodic_abstracts:
                continue
            ante = antecedent_counts[x]
            if ante == 0:
                continue
            confidence = support / ante
            if confidence < self._min_confidence:
                continue
            base_p_y = topic_counts[y] / total
            lift = confidence / base_p_y if base_p_y > 0 else 0.0
            if lift < self._min_lift:
                continue
            pattern = Pattern(
                antecedent=x,
                consequent=y,
                support=support,
                confidence=round(confidence, 3),
                lift=round(lift, 3),
            )
            key = (x, y)
            existing = self._discovered.get(key)
            if existing and (now - existing.last_emitted_at) < self._cooldown:
                continue  # cooldown, skip
            self._discovered[key] = _Discovered(pattern, now)
            new_patterns.append(pattern)
            await self._safe_publish(pattern)
        return new_patterns

    async def _safe_publish(self, pattern: Pattern) -> None:
        payload = {
            "antecedent": pattern.antecedent,
            "consequent": pattern.consequent,
            "support": pattern.support,
            "confidence": pattern.confidence,
            "lift": pattern.lift,
            "summary": (
                f"{pattern.antecedent} 之后 {self._window} 步内"
                f"常出现 {pattern.consequent} "
                f"(置信={pattern.confidence:.0%}, 提升={pattern.lift:.1f}x)"
            ),
        }
        # Also emit a gateway-visible log for diagnostics
        import sys
        sys.stdout.write(f"MINER-DIAG about to publish L5.pattern.discovered: {payload['antecedent']} -> {payload['consequent']}\n")
        sys.stdout.flush()
        _gateway_logger.info("MINER → publishing L5.pattern.discovered: %s → %s (lift=%.2f)", payload["antecedent"], payload["consequent"], payload["lift"])
        try:
            await self._bus.publish(Event(
                topic="L5.pattern.discovered",
                source="L5.miner",
                payload=payload,
            ))
            logger.info("L5.pattern.discovered published: %s → %s (lift=%.2f, conf=%.2f, bus_id=%s)", payload["antecedent"], payload["consequent"], payload["lift"], payload["confidence"], id(self._bus))
        except Exception as exc:  # noqa: BLE001
            logger.debug("L5 publish failed (non-fatal): %s", exc)

        # Also write to self-model as a learned vision fact
        if self._sm is not None:
            try:
                self._sm.history_facts.append(
                    f"我发现模式: {pattern.antecedent} 之后常出现 {pattern.consequent} "
                    f"(置信={pattern.confidence:.0%})"
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("L5 pattern self_model write failed: %s", exc)

    # ------------------------------------------------------------------
    def discovered(self) -> list[Pattern]:
        return [d.pattern for d in self._discovered.values()]

    def set_min_lift(self, value: float) -> None:
        """被 SelfTuner 调用，调整置信度门槛并触发重新挖掘。"""
        if value == self._min_lift:
            return
        self._min_lift = max(1.0, value)
        logger.info("PatternMiner min_lift updated to %.2f, triggering re-mine", self._min_lift)
        # 异步重新挖掘（用 bus 作为协程调度，不阻塞）
        # 用 create_task + add_done_callback 避免未等待的警告，
        # 同时防止异常传播到 event_bus.publish 的 gather 链。
        task = asyncio.create_task(self.mine_now())
        task.add_done_callback(
            lambda t: logger.debug("re-mine task done: %s", t.result() if t.done() and not t.cancelled() else t.cancelled() and "cancelled" or "failed")
        )

    def stats(self) -> dict:
        return {
            "mine_count": self._mine_count,
            "patterns_discovered": len(self._discovered),
            "window": self._window,
            "min_support": self._min_support,
            "min_confidence": self._min_confidence,
            "min_lift": self._min_lift,
        }

    def what_did_i_learn(self) -> str:
        """Return a human-readable summary of discovered patterns."""
        patterns = self.discovered()
        if not patterns:
            return "还没有学到任何规律"
        lines = [f"发现 {len(patterns)} 个因果规律:"]
        for p in patterns:
            lines.append(
                f"  • {p.antecedent} → {p.consequent} "
                f"(支持={p.support}, 置信={p.confidence:.0%}, 提升={p.lift:.1f}x)"
            )
        return "\n".join(lines)
