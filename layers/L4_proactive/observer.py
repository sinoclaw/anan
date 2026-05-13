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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.L4.observer")


# ---------------------------------------------------------------------------
# Probe protocol
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    """What a probe found out about an intent."""
    verdict: str    # "verified" | "falsified" | "inconclusive"
    evidence: str   # human-readable explanation
    detail: dict = field(default_factory=dict)


# A probe takes (intent, ctx) and returns a ProbeResult.
# ctx exposes: bus, working_memory, self_model, intent_stack
Probe = Callable[["Intent", "ProbeContext"], ProbeResult]


@dataclass
class ProbeContext:
    bus: EventBus
    working_memory: Any = None
    self_model: Any = None
    intent_stack: Any = None


# ---------------------------------------------------------------------------
# Built-in probes
# ---------------------------------------------------------------------------

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


class ProactiveObserver:
    """L4 — listens to L8 intent snapshots, runs probes, emits observations.

    Wiring:
        l4 = ProactiveObserver(
            bus=bus,
            intent_stack=l8,
            working_memory=wm,
            self_model=l9.model,
            auto_satisfy=True,    # verified verdict → call l8.satisfy()
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
        self._unsubs: list[Callable[[], None]] = []
        self._observations: list[dict] = []

    # ------------------------------------------------------------------
    async def attach(self) -> None:
        async def on_snapshot(event: Event):
            await self._on_snapshot(event)
        self._unsubs.append(
            self._bus.subscribe("L8.intent.snapshot", on_snapshot)
        )

    async def detach(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()

    # ------------------------------------------------------------------
    def register_probe(self, key: str, probe: Probe) -> None:
        self._probes[key] = probe

    # ------------------------------------------------------------------
    async def observe_now(self) -> list[dict]:
        """Trigger probes immediately for current top intents.
        Returns list of observation dicts."""
        if self._intent_stack is None:
            return []
        results = []
        ctx = ProbeContext(
            bus=self._bus, working_memory=self._wm,
            self_model=self._sm, intent_stack=self._intent_stack,
        )
        for intent in self._intent_stack.top(7):
            probe = self._probes.get(intent.key)
            if probe is None:
                continue
            try:
                result = probe(intent, ctx)
            except Exception as exc:  # noqa: BLE001
                logger.debug("L4 probe %s failed: %s", intent.key, exc)
                result = ProbeResult("inconclusive", f"probe error: {exc}")
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
