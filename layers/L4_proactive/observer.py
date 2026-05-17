"""
L4 ProactiveObserver — 主动求证
================================

L8 渴望（"保持注意力均衡"）但只能加固/衰减自己。L4 是**主动眼睛**：
监听 L8 snapshot, 为每个 top intent 跑一个 probe（探针），找证据：
  - 证据说"已经实现了" → satisfy(intent) → 加速衰减 → anan 不再担心
  - 证据说"还没有"     → reinforce → anan 继续想着这事
  - 证据说"恶化了"     → 升格成更强的渴望

这是把单向渴望升级成**双向反馈循环** — anan 第一次会
**主动确认想法是否实现**, 不只是被动等待 L7 反复触发.

Probe 库 (内置启发式, 后续可热插拔):
  - keep_attention_balanced  → 看 WM 里 top layer 占比 < 阈值 (默认 0.4)
  - grow_identity            → 看 SelfModel.identity_facts 上次到现在涨了没
  - heal_bus                 → 看最近 N 个事件的 errors 是否归零
  - know_myself              → 看 self-model facts >= floor

设计:
  1. **不直接做事** — 只发证据信号, 让 L8 决定 satisfy/reinforce
  2. probe 必须**纯净** — 失败/异常只 log, 不抛, 不影响其他 probe
  3. 通过 ProbeRegistry 热插拔 — 任何人可以注册新 probe
  4. 发 L4.observation.* 事件 (verified/falsified/inconclusive)
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from kernel.event_bus import Event, EventBus, get_bus
from layers.L4_proactive.protocols import ProbeContext, ProbeResult

# Re-export for public API (imported by __init__.py and tests)
__all__ = [
    "DEFAULT_PROBES",
    "ProactiveObserver",
    "Probe",
    "ProbeContext",
    "ProbeResult",
    "probe_grow_identity",
    "probe_heal_bus",
    "probe_keep_attention_balanced",
]
from layers.L4_proactive.observability_advisor import ObservabilityAdvisor

logger = logging.getLogger("anan.L4.observer")


# ---------------------------------------------------------------------------
# Probe protocol
# ---------------------------------------------------------------------------

# A probe takes (intent, ctx) and returns a ProbeResult.
# ctx exposes: bus, working_memory, self_model, intent_stack
Probe = Callable[..., ProbeResult]


# ---------------------------------------------------------------------------
# Built-in probes
# --------------------------------------------------------------------------

def probe_keep_attention_balanced(intent, ctx: ProbeContext) -> ProbeResult:
    """L8 wants attention balanced. Look at WM — is the top layer still hogging?"""
    wm = ctx.working_memory
    if wm is None:
        return ProbeResult("inconclusive", "no working_memory wired")
    snapshot = wm.snapshot()
    if not snapshot:
        return ProbeResult("inconclusive", "WM is empty")
    layer_counts = Counter(e.event.topic.split(".")[0] for e in snapshot)
    total = sum(layer_counts.values())
    top_layer, top_count = layer_counts.most_common(1)[0]
    top_share = top_count / total
    detail = {"top_layer": top_layer, "top_share": round(top_share, 3),
              "wm_size": total}
    if top_share < 0.4:
        return ProbeResult(
            "verified",
            f"注意力已均衡: top {top_layer} 只占 {top_share:.0%}",
            detail,
        )
    elif top_share > 0.6:
        return ProbeResult(
            "falsified",
            f"注意力还偏: top {top_layer} 仍占 {top_share:.0%}",
            detail,
        )
    return ProbeResult(
        "inconclusive",
        f"注意力中等: top {top_layer} 占 {top_share:.0%}",
        detail,
    )


def probe_grow_identity(intent, ctx: ProbeContext) -> ProbeResult:
    """L8 wants identity to grow. Compare current identity_facts against the
    count baked into the intent's detail (set on previous probe)."""
    sm = ctx.self_model
    if sm is None:
        return ProbeResult("inconclusive", "no self_model wired")
    cur = len(sm.identity_facts)
    last = intent.detail.get("_l4_last_identity_count")
    if last is None:
        # Baseline this run; no verdict yet
        intent.detail["_l4_last_identity_count"] = cur
        return ProbeResult(
            "inconclusive",
            f"基线建立: identity_facts={cur}",
            {"current": cur},
        )
    intent.detail["_l4_last_identity_count"] = cur
    if cur > last:
        return ProbeResult(
            "verified",
            f"身份增长了: {last} → {cur}",
            {"current": cur, "previous": last, "delta": cur - last},
        )
    return ProbeResult(
        "falsified",
        f"身份停滞: 仍是 {cur} (上次也是 {last})",
        {"current": cur, "previous": last},
    )


def probe_heal_bus(intent, ctx: ProbeContext) -> ProbeResult:
    """L8 wants bus errors gone. Look at last 50 events — any errors?"""
    history = ctx.bus.history(limit=50)
    error_topics = [e for e in history if "error" in e.topic.lower()]
    if not error_topics:
        return ProbeResult(
            "verified",
            f"最近 50 个事件无错误",
            {"checked": len(history)},
        )
    return ProbeResult(
        "falsified",
        f"仍有 {len(error_topics)} 个错误事件",
        {"errors": len(error_topics)},
    )


# ---------------------------------------------------------------------------
# Registry + Observer
# ---------------------------------------------------------------------------

DEFAULT_PROBES: dict[str, Probe] = {
    "keep_attention_balanced": probe_keep_attention_balanced,
    "grow_identity": probe_grow_identity,
    "heal_bus": probe_heal_bus,
}


def probe_catchall(intent, ctx: ProbeContext) -> ProbeResult:
    """Catch-all probe for intents with no dedicated probe.

    Heuristic rules (no LLM needed):
      - keep_triggering_X  → look for X in bus history — if seen recently → verified
      - avoid_X            → look for X in bus history — if NOT seen recently → verified
      - keep_doing_X       → look for X in bus history — if seen recently → verified
    Falls back to inconclusive when the heuristic can't determine.
    """
    key = intent.key
    history = ctx.bus.history(limit=30)

    if key.startswith("keep_triggering_"):
        target = key.replace("keep_triggering_", "").replace("_", ".")
        found = any(target in e.topic or target in str(e.payload) for e in history)
        if found:
            return ProbeResult("verified", f"最近 30 事件中出现过 {target}", {"target": target})
        return ProbeResult("inconclusive", f"最近 30 事件中未出现 {target}，需更多信息", {"target": target})

    if key.startswith("avoid_"):
        action = key.replace("avoid_", "").replace("_", ".")
        found = any(action in e.topic or action in str(e.payload) for e in history)
        if not found:
            return ProbeResult("verified", f"{action} 未出现，避开了", {"action": action})
        return ProbeResult("falsified", f"{action} 仍在发生", {"action": action})

    if key.startswith("keep_doing_"):
        action = key.replace("keep_doing_", "").replace("_", ".")
        found = any(action in e.topic or action in str(e.payload) for e in history)
        if found:
            return ProbeResult("verified", f"{action} 仍在做", {"action": action})
        return ProbeResult("inconclusive", f"{action} 最近未观察到", {"action": action})

class _IntentVerificationRecord:
    """Per-intent verification state for OBS-1 adaptive scheduling."""

    __slots__ = ("key", "last_verdict", "last_evidence", "consecutive_count", "next_verify_at")

    def __init__(self, key: str):
        self.key: str = key
        self.last_verdict: str = "inconclusive"
        self.last_evidence: str = ""
        self.consecutive_count: int = 0  # how many times this intent was verified in a row
        self.next_verify_at: float = 0.0  # monotonic timestamp (time.time() based)


class _VerificationScheduler:
    """OBS-1: Adaptive verification interval scheduler.

    Instead of re-verifying every intent every N seconds, this tracks per-intent
    state and dynamically picks which intents are due right now:

      - verified + stable (consecutive ≥ 3) → slow down: base_interval × 2.5
      - verified + fresh (consecutive < 3)   → normal:  base_interval × 1
      - inconclusive                        → sooner:  base_interval × 0.7
      - falsified                           → urgent:  next tick (0)
      - newly seen / unknown                → immediate

    The base_interval is the proactive_interval_s configured on ProactiveObserver.
    """

    __slots__ = ("base_interval", "_records", "_fresh_keys")

    def __init__(self, base_interval: float):
        # Minimum interval cap so nothing is checked more than once per tick
        self.base_interval: float = max(base_interval, 10.0)
        self._records: dict[str, _IntentVerificationRecord] = {}
        # Keys seen this tick that have no prior record → verify immediately
        self._fresh_keys: set[str] = set()

    def mark_fresh(self, key: str) -> None:
        """Call this when an intent appears in the top-N snapshot."""
        if key not in self._records:
            self._fresh_keys.add(key)

    def get_due_keys(self, now: float) -> list[str]:
        """Return intent keys that are due for verification at time `now`."""
        due = []
        for key in self._fresh_keys:
            due.append(key)
        self._fresh_keys.clear()
        for key, rec in self._records.items():
            if now >= rec.next_verify_at:
                due.append(key)
        return due

    def record_result(self, key: str, verdict: str, evidence: str, now: float) -> None:
        """Update tracking after a probe run for `key`."""
        rec = self._records.get(key)
        if rec is None:
            rec = _IntentVerificationRecord(key)
            self._records[key] = rec
        rec.last_verdict = verdict
        rec.last_evidence = evidence
        if verdict == "verified":
            rec.consecutive_count += 1
        else:
            rec.consecutive_count = 0
        rec.next_verify_at = self._compute_next_at(rec, now)

    def _compute_next_at(self, rec: _IntentVerificationRecord, now: float) -> float:
        multiplier: float
        if rec.last_verdict == "verified":
            if rec.consecutive_count >= 3:
                multiplier = 2.5
            else:
                multiplier = 1.0
        elif rec.last_verdict == "inconclusive":
            multiplier = 0.7
        elif rec.last_verdict == "falsified":
            multiplier = 0.0  # verify on next tick
        else:
            multiplier = 1.0
        return now + self.base_interval * multiplier


class ProactiveObserver:
    """L4 — listens to L8 intent snapshots, runs probes, emits observations.

    Wiring:
        l4 = ProactiveObserver(
            bus=bus,
            intent_stack=l8,
            working_memory=wm,
            self_model=l9.model,
            auto_satisfy=True,    # verified verdict → call l8.satisfy()
            reinforce_on_falsify=True,
            llm_probe_fn=some_async_fn,   # optional async(intent_key, description, ctx) → ProbeResult
            proactive_interval_s=30.0,    # 0 = disabled (snapshot-driven only)
        )
        await l4.attach()
    """

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        intent_stack=None,
        working_memory=None,
        self_model=None,
        probes: Optional[dict[str, Probe]] = None,
        auto_satisfy: bool = True,
        reinforce_on_falsify: bool = True,
        # Optional LLM probe — async fn(intent_key, intent_description, context) -> ProbeResult
        # Called when no built-in probe matches. If None, unmatched intents are skipped.
        llm_probe_fn: Optional[Callable[..., Awaitable[ProbeResult]]] = None,
        # Proactive loop: how often to proactively verify top intents (seconds)
        proactive_interval_s: float = 0.0,  # 0 = disabled (snapshot-driven only)
    ):
        self._bus = bus or get_bus()
        self._intent_stack = intent_stack
        self._wm = working_memory
        self._sm = self_model
        self._probes: dict[str, Probe] = dict(DEFAULT_PROBES)
        if probes:
            self._probes.update(probes)
        self._auto_satisfy = auto_satisfy
        self._reinforce_on_falsify = reinforce_on_falsify
        self._llm_probe_fn = llm_probe_fn
        self._proactive_interval = proactive_interval_s
        self._delegate_fn: Optional[callable] = None  # delegate_task injected by MindStackRunner
        self._unsubs: list[Callable[[], None]] = []
        self._observations: list[dict] = []
        # OBS-1: ObservabilityAdvisor for generic intent verification (subagent mode)
        self._obs_advisor = ObservabilityAdvisor()
        # If no custom llm_probe_fn was provided, use the advisor as the LLM probe
        if self._llm_probe_fn is None:
            self._llm_probe_fn = self._obs_advisor.evaluate
        # OBS-1: adaptive verification scheduler
        self._verify_scheduler: Optional[_VerificationScheduler] = None
        if proactive_interval_s > 0:
            self._verify_scheduler = _VerificationScheduler(base_interval=proactive_interval_s)

    # ------------------------------------------------------------------
    # delegate injection (for MindStackRunner)
    # ------------------------------------------------------------------

    def set_delegate(self, fn) -> None:
        """MindStackRunner calls this to inject the async delegate."""
        self._delegate_fn = fn
        self._obs_advisor.set_delegate(fn)

    # ------------------------------------------------------------------
    async def attach(self) -> None:
        async def on_snapshot(event: Event):
            await self._on_snapshot(event)

        async def on_tick(event: Event):
            # Proactive loop: use scheduler to pick which intents are due
            if self._proactive_interval <= 0 or self._verify_scheduler is None:
                return
            payload = event.payload or {}
            ticks = payload.get("ticks", 0)
            if ticks % 3 != 0:  # every 3rd tick to keep overhead low
                return
            if self._intent_stack is None:
                return
            import time as _time
            now = _time.time()
            # Mark current top-N as fresh (new/unknown → immediate)
            for intent in self._intent_stack.top(7):
                self._verify_scheduler.mark_fresh(intent.key)
            due_keys = self._verify_scheduler.get_due_keys(now)
            if not due_keys:
                return
            logger.debug("L4 ProactiveObserver: proactive probe tick=%d, due=%s", ticks, due_keys)
            try:
                await self._observe_due(due_keys)
            except Exception as exc:  # noqa: BLE001
                logger.warning("L4 proactive observe_now failed: %s", exc)

        self._unsubs.append(
            self._bus.subscribe("L8.intent.snapshot", on_snapshot)
        )
        self._unsubs.append(
            self._bus.subscribe("L0.circadian.tick", on_tick)
        )

    async def detach(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()

    # ------------------------------------------------------------------
    def register_probe(self, key: str, probe: Probe) -> None:
        self._probes[key] = probe

    # ------------------------------------------------------------------
    async def _observe_due(self, intent_keys: list[str]) -> list[dict]:
        """Run probes for a specific set of intent keys (used by proactive loop)."""
        if self._intent_stack is None:
            return []
        import time as _time
        now = _time.time()
        ctx = ProbeContext(
            bus=self._bus, working_memory=self._wm,
            self_model=self._sm, intent_stack=self._intent_stack,
        )
        # Build a quick lookup: key → intent object
        key_to_intent = {i.key: i for i in self._intent_stack.top(10)}
        results = []
        for key in intent_keys:
            intent = key_to_intent.get(key)
            if intent is None:
                continue
            result = await self._run_probe(intent, ctx)
            if result is None:
                continue
            # Record in scheduler so next interval is adjusted
            if self._verify_scheduler is not None:
                self._verify_scheduler.record_result(key, result.verdict, result.evidence, now)
            obs = {
                "timestamp": datetime.now().isoformat(),
                "intent_key": intent.key,
                "verdict": result.verdict,
                "evidence": result.evidence,
                "detail": result.detail,
            }
            self._observations.append(obs)
            results.append(obs)
            await self._react(intent, result)
        return results

    async def _run_probe(self, intent, ctx: "ProbeContext") -> Optional[ProbeResult]:
        """Run the appropriate probe for an intent, returning the result."""
        probe = self._probes.get(intent.key)
        if probe is not None:
            try:
                return probe(intent, ctx)
            except Exception as exc:  # noqa: BLE001
                logger.debug("L4 probe %s failed: %s", intent.key, exc)
                return ProbeResult("inconclusive", f"probe error: {exc}")
        if self._llm_probe_fn is not None:
            try:
                return await self._llm_probe_fn(intent.key, intent.description, ctx)
            except Exception as exc:  # noqa: BLE001
                logger.debug("L4 LLM probe %s failed: %s", intent.key, exc)
                return ProbeResult("inconclusive", f"LLM probe error: {exc}")
        return probe_catchall(intent, ctx)

    # ------------------------------------------------------------------
    async def observe_now(self) -> list[dict]:
        """Trigger probes immediately for all current top intents (snapshot-driven).
        Returns list of observation dicts."""
        if self._intent_stack is None:
            return []
        import time as _time
        now = _time.time()
        ctx = ProbeContext(
            bus=self._bus, working_memory=self._wm,
            self_model=self._sm, intent_stack=self._intent_stack,
        )
        results = []
        for intent in self._intent_stack.top(7):
            result = await self._run_probe(intent, ctx)
            if result is None:
                continue
            # Update scheduler record (snapshot-driven = treat as fresh/unknown intent)
            if self._verify_scheduler is not None:
                self._verify_scheduler.mark_fresh(intent.key)
                self._verify_scheduler.record_result(intent.key, result.verdict, result.evidence, now)
            obs = {
                "timestamp": datetime.now().isoformat(),
                "intent_key": intent.key,
                "verdict": result.verdict,
                "evidence": result.evidence,
                "detail": result.detail,
            }
            self._observations.append(obs)
            results.append(obs)
            await self._react(intent, result)
        return results

    async def _on_snapshot(self, event: Event) -> None:
        # Snapshot-driven: verify all current top intents immediately
        await self.observe_now()

    async def _react(self, intent, result: ProbeResult) -> None:
        topic_map = {
            "verified": "L4.observation.verified",
            "falsified": "L4.observation.falsified",
            "inconclusive": "L4.observation.inconclusive",
        }
        await self._safe_publish(topic_map[result.verdict], {
            "intent_key": intent.key,
            "intent_description": intent.description,
            "evidence": result.evidence,
            "detail": result.detail,
        })
        if self._intent_stack is None:
            return
        if result.verdict == "verified" and self._auto_satisfy:
            await self._intent_stack.satisfy(intent.key)
        elif result.verdict == "falsified" and self._reinforce_on_falsify:
            await self._intent_stack.propose(
                intent.key, intent.description, source="L4",
                detail={"_l4_last_evidence": result.evidence},
            )

    async def _safe_publish(self, topic: str, payload: dict) -> None:
        try:
            await self._bus.publish(Event(
                topic=topic, source="L4.observer", payload=payload,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("L4 publish failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    def observations(self) -> list[dict]:
        return list(self._observations)

    def stats(self) -> dict:
        verdicts = Counter(o["verdict"] for o in self._observations)
        return {
            "total_observations": len(self._observations),
            "by_verdict": dict(verdicts),
            "probes_registered": list(self._probes.keys()),
        }
