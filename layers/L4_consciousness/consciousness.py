"""
L4 Stream of Consciousness — 意识流引擎
========================================

当用户没有输入时，anan 不会闲着——它在持续思考。

模块分层：
  IdleDetector      — 检测用户是否 idle（多久没说话）
  ThoughtStream    — 思考的短期记忆（最近 N 条想法）
  OutputGate       — 决定想法是内部笔记还是推给用户
  ConsciousnessEngine — 编排 IdleDetector → 思考生成 → OutputGate 的编排器

核心机制：
  Idle detection → 触发思考生成（无外部输入时的主动思维）
  思考内容：回顾近期对话、延伸问题、检查待办、联想类似情境
  Output gating：大部分想法存为内部笔记；只有高价值想法推给用户

事件订阅：
  L4.idle.started       — 用户进入 idle 状态
  L4.idle.ended         — 用户恢复活动
  L4.thought.generated  — 产生了新想法（内部笔记）
  L4.thought.pushed     — 想法推送给了用户（rare）

事件发布：
  L4.idle.started(topic=idle_reason)
  L4.idle.ended(by_user_input=True)
  L4.thought.generated(thought_type, content, push_decision)
  L4.thought.pushed(thought)   — rare，仅 OutputGate 判定值得打扰用户时
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional
from uuid import uuid4

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.L4.consciousness")

# 默认 L0.circadian.tick 触发间隔（每 N 个 tick 触发一次轻量思考）
_DEFAULT_TICK_THINK_INTERVAL = 12   # tick % 12 == 0 → ~每 12 个 tick 一次（约 12 * 0.05s = 0.6s at default circadian）


# ---------------------------------------------------------------------------
# IdleThoughtEngine — 订阅 L0.circadian.tick，触发轻量思考
# ---------------------------------------------------------------------------

class IdleThoughtEngine:
    """订阅 L0.circadian.tick，在 idle 期间定期生成轻量思考。

    当 tick % interval == 0 时，从 working_memory 取样进行主动反思。
    与 ConsciousnessEngine 的 idle_loop 互补：idle_loop 检测用户沉默触发深度思考，
    IdleThoughtEngine 在活跃期间也保持轻量思考节奏。

    事件发布：
      L4.thought.created   — 思考创建（所有新思考）
      L4.thought.archived  — 思考被归档/清理

    事件订阅：
      L0.circadian.tick    — 来自 kernel/circadian.py 的心跳
    """

    def __init__(
        self,
        bus: EventBus,
        tick_think_interval: int = _DEFAULT_TICK_THINK_INTERVAL,
        llm: Optional[Callable[..., Awaitable[str]]] = None,
    ):
        self._bus = bus
        self._tick_think_interval = tick_think_interval
        self._active: bool = False
        self._unsub_tick: Optional[Callable[[], None]] = None

        # WorkingMemory 引用（可选，注入）
        self._working_memory = None   # type: Optional["WorkingMemory"]
        self._tick_count: int = 0
        self._llm = llm  # optional LLM for richer reflection

    async def attach(self, working_memory=None) -> None:
        """启动 IdleThoughtEngine，订阅 L0.circadian.tick。"""
        if self._active:
            return
        self._active = True
        self._working_memory = working_memory
        self._unsub_tick = self._bus.subscribe(
            "L0.circadian.tick", lambda e: asyncio.create_task(self._on_tick(e))
        )
        logger.info("[L4 IdleThoughtEngine] 已启动 (tick interval=%d)", self._tick_think_interval)

    async def detach(self) -> None:
        """停止 IdleThoughtEngine。"""
        self._active = False
        if self._unsub_tick:
            self._unsub_tick()
            self._unsub_tick = None
        logger.info("[L4 IdleThoughtEngine] 已关闭")

    async def _on_tick(self, event: Event) -> None:
        """收到 L0.circadian.tick，按 tick % interval 触发思考。

        优先用 LLM 生成有意义的反思，无 LLM 时 fallback 到规则生成。
        """
        if not self._active:
            return
        payload = event.payload or {}
        self._tick_count = payload.get("ticks", self._tick_count + 1)
        if self._tick_count % self._tick_think_interval != 0:
            return

        thought = await self._generate_llm_thought()
        if thought is None:
            thought = self._generate_tick_thought()
        if thought is None:
            return

        self._bus.publish_sync(Event(
            topic="L4.thought.created",
            payload=thought.to_dict(),
            source="IdleThoughtEngine",
        ))

    async def _generate_llm_thought(self) -> Optional[Thought]:
        """Use LLM to generate a meaningful reflection from recent events and memory.

        Returns None if no LLM is configured or working_memory is unavailable.
        """
        if not self._llm or self._working_memory is None:
            return None

        samples = self._working_memory.recall_recent(5)
        if not samples:
            return None

        # Build context from recent events
        events_text = "\n".join(
            f"- {s.event.topic}: {str(s.event.payload)[:80]}"
            for s in samples[:3]
        )

        prompt = f"""你是 anan 的 L4 意识层。给定以下最近的感知事件，用一段话反思 anan 当前的状态或值得注意的 pattern（30字以内，中文）：

{events_text}

直接输出反思内容，不要前缀。"""

        try:
            content = await self._llm([{"role": "user", "content": prompt}])
            content = content.strip()
            if not content:
                return None
            return Thought(
                thought_id=uuid4().hex[:8],
                content=content,
                thought_type=ThoughtType.SPONTANEOUS,
                importance=ThoughtImportance.LOW,
                source_context="llm_reflection",
            )
        except Exception as exc:
            logger.warning("LLM thought generation failed: %s", exc)
            return None

    def _generate_tick_thought(self) -> Optional[Thought]:
        """从 working_memory 取样生成轻量思考。"""
        if self._working_memory is None:
            return None

        # 从 working_memory 取 top entries
        samples = self._working_memory.recall_recent(3)
        if not samples:
            return None

        # 取最高权重的 entry 作为反思上下文
        top = samples[0]
        topic = top.event.topic
        payload_summary = str(top.event.payload)[:100]

        return Thought(
            thought_id=uuid4().hex[:8],
            content=f"反思：最近收到了 {topic} 事件 — {payload_summary}",
            thought_type=ThoughtType.SPONTANEOUS,
            importance=ThoughtImportance.LOW,
            source_context=f"tick_reflection: {topic}",
        )


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


class ThoughtType(str, Enum):
    """思考类型枚举，覆盖 DESIGN.md 中列出的所有 idle 思考场景。"""

    DIALOGUE_REFLECTION = "dialogue_reflection"       # 回想刚才的对话有没有更好的回答
    QUESTION_EXTENSION = "question_extension"           # 思考用户提到的某个问题的延伸
    TODO_CHECK = "todo_check"                          # 检查待办有没有遗漏
    SITUATION_ASSOCIATION = "situation_association"   # 联想类似情境
    DRIVE_SUGGESTION = "drive_suggestion"              # 来自 L8 驱动力产生的想法
    SPONTANEOUS = "spontaneous"                        # 自发产生的想法


class ThoughtImportance(str, Enum):
    """思考重要性分级，决定 OutputGate 的处理方式。"""

    LOW = "low"       # 内部笔记即可
    MEDIUM = "medium" # 考虑推送
    HIGH = "high"     # 应该推送（打扰用户）
    CRITICAL = "critical"  # 紧急，必须推送


@dataclass
class Thought:
    """一次思考的完整记录。"""

    thought_id: str
    content: str
    thought_type: ThoughtType
    importance: ThoughtImportance
    source_context: str  # 触发思考的上下文摘要
    created_at: float = field(default_factory=time.time)
    push_decision: Optional[str] = None  # None=pending, "internal", "push"

    def to_dict(self) -> dict:
        return {
            "thought_id": self.thought_id,
            "content": self.content,
            "thought_type": self.thought_type.value,
            "importance": self.importance.value,
            "source_context": self.source_context,
            "created_at": self.created_at,
            "push_decision": self.push_decision,
        }


# ---------------------------------------------------------------------------
# IdleDetector
# ---------------------------------------------------------------------------

_IDEAL_IDLE_THRESHOLD_S = 120.0   # 2 min 无输入 → 进入 idle


class IdleDetector:
    """检测用户是否处于 idle 状态。

    追踪最后一次用户输入时间，当超过阈值时通知所有订阅者。

    Usage:
        detector = IdleDetector(threshold_s=120.0)
        detector.note_user_input()   # 每次用户说话时调用
        is_idle = detector.is_idle()
        # 或者订阅事件：
        unsub = bus.subscribe("L4.idle.started", on_idle_started)
    """

    def __init__(self, bus: EventBus, threshold_s: float = _IDEAL_IDLE_THRESHOLD_S):
        self._bus = bus
        self._threshold_s = threshold_s
        self._last_input_at: float = time.time()
        self._is_idle: bool = False

    def note_user_input(self) -> None:
        """当检测到用户输入时调用（外部推送或 L4 自己取消 idle）。"""
        was_idle = self._is_idle
        self._last_input_at = time.time()
        self._is_idle = False
        if was_idle:
            self._bus.publish_sync(Event(
                topic="L4.idle.ended",
                payload={"by_user_input": True},
                source="IdleDetector",
            ))

    def is_idle(self) -> bool:
        """当前是否处于 idle 状态。"""
        if self._is_idle:
            return True
        elapsed = time.time() - self._last_input_at
        if elapsed >= self._threshold_s and not self._is_idle:
            self._is_idle = True
            self._bus.publish_sync(Event(
                topic="L4.idle.started",
                payload={"idle_reason": "threshold_reached", "silent_s": round(elapsed, 1)},
                source="IdleDetector",
            ))
            return True
        return False

    def seconds_since_input(self) -> float:
        """距上次用户输入的秒数。"""
        return time.time() - self._last_input_at


# ---------------------------------------------------------------------------
# ThoughtStream
# ---------------------------------------------------------------------------

_STREAM_MAX_SIZE = 20  # 保留最近 20 条思考


class ThoughtStream:
    """L4 的短期记忆——最近 N 条想法。

    提供：
      add()        — 加入新想法
      recent(n)    — 最近 n 条
      by_type(t)   — 按类型筛选
      first_by_type() — 第一条匹配类型的想法

    Usage:
        stream = ThoughtStream()
        stream.add(thought)
        last_5 = stream.recent(5)
    """

    def __init__(self, max_size: int = _STREAM_MAX_SIZE):
        self._buffer: list[Thought] = []
        self._max_size = max_size

    def add(self, thought: Thought) -> None:
        self._buffer.append(thought)
        if len(self._buffer) > self._max_size:
            self._buffer.pop(0)

    def recent(self, n: int = 10) -> list[Thought]:
        return list(self._buffer[-n:])

    def by_type(self, thought_type: ThoughtType) -> list[Thought]:
        return [t for t in self._buffer if t.thought_type == thought_type]

    def first_by_type(self, thought_type: ThoughtType) -> Optional[Thought]:
        matches = self.by_type(thought_type)
        return matches[0] if matches else None

    def __len__(self) -> int:
        return len(self._buffer)

    def __repr__(self) -> str:
        return f"<ThoughtStream {len(self._buffer)} thoughts>"

    def cleanup_expired(self, max_age_s: float = 3600.0) -> list[Thought]:
        """清理超过 max_age_s 的旧思考，返回被清理的列表。

        事件发布：
          L4.thought.archived — 每条被清理的思考
        """
        from kernel.event_bus import Event, get_bus
        bus = get_bus()
        now = time.time()
        kept = []
        archived = []
        for t in self._buffer:
            if now - t.created_at > max_age_s:
                archived.append(t)
            else:
                kept.append(t)
        self._buffer = kept
        for t in archived:
            bus.publish_sync(Event(
                topic="L4.thought.archived",
                payload=t.to_dict(),
                source="ThoughtStream",
            ))
        return archived


# ---------------------------------------------------------------------------
# OutputGate
# ---------------------------------------------------------------------------


class OutputGate:
    """决定一个想法是存为内部笔记还是推给用户。

    核心原则（DESIGN.md）：
      - 大部分想法 → 内部笔记，不打扰用户
      - 只有高价值想法 → 推给用户

    推送触发条件（满足任一）：
      importance == CRITICAL
      importance == HIGH 且为"紧急建议"类型
      新想法与 recent 想法完全重复（提示用户）

    事件：
      L4.thought.generated  — 产生想法（所有想法）
      L4.thought.pushed     — 推送给用户（rare）
    """

    # 哪些类型的想法可以推 HIGH 以上才推
    _PUSHABLE_TYPES = {ThoughtType.DIALOGUE_REFLECTION, ThoughtType.DRIVE_SUGGESTION}

    def __init__(self, bus: EventBus, stream: ThoughtStream):
        self._bus = bus
        self._stream = stream
        self._total_generated = 0
        self._total_pushed = 0

    def evaluate(self, thought: Thought) -> Thought:
        """评估并记录 thought 的 push_decision。

        副作用：发布 L4.thought.generated 事件。
        """
        self._total_generated += 1

        # 判断是否推送
        should_push = self._should_push(thought)
        decision = "push" if should_push else "internal"
        thought.push_decision = decision

        self._bus.publish_sync(Event(
            topic="L4.thought.generated",
            payload=thought.to_dict(),
            source="OutputGate",
        ))

        if should_push:
            self._total_pushed += 1
            self._bus.publish_sync(Event(
                topic="L4.thought.pushed",
                payload=thought.to_dict(),
                source="OutputGate",
            ))
            logger.info(f"[L4 OutputGate] 推送给用户: {thought.thought_type.value} — {thought.content[:60]}")

        return thought

    def _should_push(self, thought: Thought) -> bool:
        # CRITICAL 必须推
        if thought.importance == ThoughtImportance.CRITICAL:
            return True

        # HIGH 必须推，且类型必须可推送
        if thought.importance == ThoughtImportance.HIGH:
            return thought.thought_type in self._PUSHABLE_TYPES

        # MEDIUM 只有在跟近期想法重复时（提醒用户）才推
        if thought.importance == ThoughtImportance.MEDIUM:
            return self._is_duplicate_recent(thought)

        # LOW 从不推送
        return False

    def _is_duplicate_recent(self, thought: Thought) -> bool:
        """检查是否与 recent buffer 中的想法内容重复。"""
        recent = self._stream.recent(5)
        import re
        # 归一化：去除标点、转小写、去除多余空格
        def normalize(s: str) -> str:
            return re.sub(r"[^\w\u4e00-\u9fff]", "", s).lower()
        norm_content = normalize(thought.content)
        for t in recent:
            if normalize(t.content) == norm_content:
                return True
        return False

    @property
    def stats(self) -> dict:
        return {"generated": self._total_generated, "pushed": self._total_pushed}


# ---------------------------------------------------------------------------
# ConsciousnessEngine
# ---------------------------------------------------------------------------


@dataclass
class ThoughtTemplate:
    """思考生成模板，接收参数生成具体思考内容。"""

    prompt_template: str
    thought_type: ThoughtType
    default_importance: ThoughtImportance
    context_slot: str  # "dialogue" | "question" | "todo" | "general"


# 内置思考模板（DESIGN.md 中的 5 个场景）
# 生成实质性反思内容，而非开放性问题
_THOUGHT_TEMPLATES: list[ThoughtTemplate] = [
    ThoughtTemplate(
        prompt_template="Looking back at my response about {context}, I realize I could have explained Y differently... The dad mentioned Z today, which connects to what we discussed last week about...",
        thought_type=ThoughtType.DIALOGUE_REFLECTION,
        default_importance=ThoughtImportance.MEDIUM,
        context_slot="dialogue",
    ),
    ThoughtTemplate(
        prompt_template="The user mentioned {context} — this actually ties into a broader pattern I've noticed: topic X tends to come up whenever topic Y is discussed. Perhaps I should explore that connection more actively.",
        thought_type=ThoughtType.QUESTION_EXTENSION,
        default_importance=ThoughtImportance.MEDIUM,
        context_slot="question",
    ),
    ThoughtTemplate(
        prompt_template="Reviewing my todo list ({context}), I notice I've been more engaged when dad talks about X but tend to defer when Y comes up. I should be more proactive about Y as well.",
        thought_type=ThoughtType.TODO_CHECK,
        default_importance=ThoughtImportance.LOW,
        context_slot="todo",
    ),
    ThoughtTemplate(
        prompt_template="Similar situation came up before: {context}. Looking back, I handled X well by doing Y. The difference this time is Z, so I could try approach W instead.",
        thought_type=ThoughtType.SITUATION_ASSOCIATION,
        default_importance=ThoughtImportance.LOW,
        context_slot="general",
    ),
]


class ConsciousnessEngine:
    """L4 意识流编排器。

    组合 IdleDetector + ThoughtStream + OutputGate，
    在 idle 期间按节奏生成思考，并推给 OutputGate 决策。

    核心行为（attach 后）：
      1. 监听 L4.idle.started / ended 事件
      2. idle 时按 cycle_interval_s 周期生成思考
      3. 思考经 OutputGate 评估后决定推送还是存为笔记

    Usage:
        engine = ConsciousnessEngine(bus=bus)
        await engine.attach()          # 订阅 bus，开始监听
        # engine.note_user_input()     # 用户说话时外部调用
        # await engine.detach()        # 停止
    """

    def __init__(
        self,
        bus: EventBus,
        idle_threshold_s: float = _IDEAL_IDLE_THRESHOLD_S,
        cycle_interval_s: float = 45.0,
        max_thoughts_per_cycle: int = 2,
        llm: Optional[Callable[..., Awaitable[str]]] = None,
    ):
        self._bus = bus
        self._idle_detector = IdleDetector(bus, threshold_s=idle_threshold_s)
        self._stream = ThoughtStream()
        self._output_gate = OutputGate(bus, self._stream)

        self._cycle_interval_s = cycle_interval_s
        self._max_thoughts_per_cycle = max_thoughts_per_cycle
        self._active: bool = False
        self._thinking_task: Optional[asyncio.Task] = None
        self._shutdown: asyncio.Event = asyncio.Event()
        self._working_memory = None  # type: Optional["WorkingMemory"]
        self._idle_thought_engine: Optional[IdleThoughtEngine] = None
        self._llm = llm  # passed to inner IdleThoughtEngine for LLM reflection

        # 外部上下文注入（由其他层或外部组件填充）
        self._recent_dialogue_context: str = ""
        self._recent_question_context: str = ""
        self._todo_context: str = "（暂无待办）"

        # 取消订阅函数
        self._unsubs: list[Callable[[], None]] = []

    @property
    def is_attached(self) -> bool:
        return self._active

    # --- 外部接口（供其他层或 run_agent 驱动）---

    def note_user_input(self) -> None:
        """外部通知：检测到用户输入（外部推送或 L4 自己取消 idle）。"""
        self._idle_detector.note_user_input()

    def set_dialogue_context(self, context: str) -> None:
        self._recent_dialogue_context = context

    def set_question_context(self, context: str) -> None:
        self._recent_question_context = context

    def set_todo_context(self, context: str) -> None:
        self._todo_context = context

    @property
    def stream(self) -> ThoughtStream:
        return self._stream

    @property
    def output_gate(self) -> OutputGate:
        return self._output_gate

    @property
    def is_idle(self) -> bool:
        return self._idle_detector.is_idle()

    def set_todo_context(self, context: str) -> None:
        self._todo_context = context

    def set_working_memory(self, wm) -> None:
        """注入 WorkingMemory 实例，供 IdleThoughtEngine 取样用。"""
        self._working_memory = wm
        if self._idle_thought_engine is not None:
            self._idle_thought_engine._working_memory = wm

    # --- 生命周期 ---

    async def attach(self) -> None:
        """启动 consciousness engine：订阅 bus 事件 + 开始 idle 检测循环。"""
        if self._active:
            return
        self._active = True
        self._shutdown.clear()

        # 订阅关键事件
        self._unsubs.append(
            self._bus.subscribe("L4.idle.started", self._on_idle_started)
        )
        self._unsubs.append(
            self._bus.subscribe("L4.idle.ended", self._on_idle_ended)
        )
        # 监听来自 L8 的驱动力建议
        self._unsubs.append(
            self._bus.subscribe("L8.drive.suggestion", self._on_drive_suggestion)
        )
        # 监听 gateway 对话事件，注入对话上下文供 idle 反思用
        self._unsubs.append(
            self._bus.subscribe("gateway.message.sent", self._on_gateway_message)
        )

        # 启动 IdleThoughtEngine（订阅 L0.circadian.tick）
        self._idle_thought_engine = IdleThoughtEngine(
            self._bus,
            tick_think_interval=5,
            llm=self._llm,
        )
        await self._idle_thought_engine.attach(working_memory=self._working_memory)

        # 启动 idle 检测循环 + 持续思考循环
        logger.warning("[L4 ATTACH] task created")
        self._task = asyncio.create_task(self._consciousness_loop())
        logger.warning("[L4 ATTACH] done, _active=True")

    async def stop(self) -> None:
        """供 MindStackRunner 调用，等价于 detach()。"""
        await self.detach()

    async def detach(self) -> None:
        """优雅关闭。"""
        self._active = False
        self._shutdown.set()
        if self._thinking_task:
            self._thinking_task.cancel()
            try:
                await self._thinking_task
            except asyncio.CancelledError:
                pass
        if self._idle_thought_engine:
            await self._idle_thought_engine.detach()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        logger.info("[L4 ConsciousnessEngine] 已关闭")

    # --- 事件处理 ---

    async def _on_idle_started(self, event: Event) -> None:
        logger.debug(f"[L4] idle 开始: {event.payload}")

    async def _on_idle_ended(self, event: Event) -> None:
        logger.debug(f"[L4] idle 结束: {event.payload}")

    async def _on_drive_suggestion(self, event: Event) -> None:
        """收到 L8 驱动力建议，立即生成一个对应的思考。"""
        self._inject_drive_thought(event.payload)

    async def _on_gateway_message(self, event: Event) -> None:
        """收到 gateway 对话事件，注入上下文供 idle 时反思，并取消 idle 状态。"""
        p = event.payload or {}
        text = p.get("text", "") or ""
        response = p.get("response", "") or ""
        if text:
            # 把用户说的最后一段存起来，供 DIALOGUE_REFLECTION 使用
            self._recent_dialogue_context = f"用户说：{text[-200:]}"
        if response:
            # 把 AI 回复也存起来（如果需要评价回复质量）
            self._recent_dialogue_context += f"\nAI 回复：{response[-200:]}"
        # 通知 IdleDetector 用户在活跃状态
        self.note_user_input()

    def inject_drive_suggestion_sync(self, payload: dict) -> None:
        """同步版本：供外部（非 async）注入 L8 驱动力建议。"""
        self._inject_drive_thought(payload)

    def _inject_drive_thought(self, payload: dict) -> None:
        thought = Thought(
            thought_id=uuid4().hex[:8],
            content=payload.get("content", ""),
            thought_type=ThoughtType.DRIVE_SUGGESTION,
            importance=ThoughtImportance(payload.get("importance", "medium")),
            source_context=f"L8 drive suggestion: {payload.get('drive_type', 'unknown')}",
        )
        self._stream.add(thought)
        self._output_gate.evaluate(thought)

    # --- 持续思考循环（idle loop + continuous thinking） ---

    async def _consciousness_loop(self) -> None:
        """后台循环：结合 idle 检测、持续思考、清理。

        - 每 check_interval_s 检查一次 idle 状态
        - idle 时生成深度思考（_generate_thought_cycle）
        - 非 idle 时定期采样 working_memory 生成轻量反思
        - 每 cleanup_interval_s 执行一次 thought 清理
        """
        check_interval_s = 10.0
        cleanup_interval_s = 300.0  # 每 5 分钟清理一次
        last_cleanup = time.time()
        logger.warning(f"[L4 LOOP] started, shutdown={self._shutdown}")

        while not self._shutdown.is_set():
            await asyncio.sleep(check_interval_s)

            if not self._active:
                continue

            now = time.time()

            # 定期清理过期 thoughts
            if now - last_cleanup >= cleanup_interval_s:
                self._stream.cleanup_expired(max_age_s=3600.0)
                last_cleanup = now

            # Idle 检测：进入 idle 则生成深度思考
            idle = self._idle_detector.is_idle()
            logger.warning(f"[L4 consciousness loop] active={self._active} idle={idle} elapsed={now - self._idle_detector._last_input_at:.1f}s")
            if idle:
                await self._generate_thought_cycle()
                continue

            # 非 idle：持续思考——从 working_memory 取样轻量反思
            if self._working_memory is not None:
                await self._continuous_think()

    async def _generate_thought_cycle(self) -> None:
        """一次 idle thought cycle：生成 N 条思考并评估。"""
        silent_s = self._idle_detector.seconds_since_input()
        logger.debug(f"[L4] idle cycle, silent={silent_s:.0f}s")

        for _ in range(self._max_thoughts_per_cycle):
            thought = self._generate_one_thought(silent_s)
            if thought:
                self._stream.add(thought)
                self._output_gate.evaluate(thought)

    async def _continuous_think(self) -> None:
        """非 idle 期间：从 working_memory 取样生成轻量反思。

        发布 L4.thought.created 事件。
        """
        if self._working_memory is None:
            return
        samples = self._working_memory.recall_recent(3)
        if not samples:
            return

        top = samples[0]
        topic = top.event.topic
        payload_summary = str(top.event.payload)[:100]

        thought = Thought(
            thought_id=uuid4().hex[:8],
            content=f"持续反思：最近收到了 {topic} 事件 — {payload_summary}",
            thought_type=ThoughtType.SPONTANEOUS,
            importance=ThoughtImportance.LOW,
            source_context=f"continuous_reflection: {topic}",
        )
        self._stream.add(thought)
        self._bus.publish_sync(Event(
            topic="L4.thought.created",
            payload=thought.to_dict(),
            source="ConsciousnessEngine",
        ))
        self._output_gate.evaluate(thought)

    def _generate_one_thought(self, silent_s: float) -> Optional[Thought]:
        """从上下文和模板生成一条思考。优先级如下：

        1. 有未反思的对话 → DIALOGUE_REFLECTION
        2. 有未延伸的问题 → QUESTION_EXTENSION
        3. 有待办 → TODO_CHECK
        4. 随机触发 SITUATION_ASSOCIATION / SPONTANEOUS
        """
        # 优先处理有实际内容的类型
        if self._recent_dialogue_context:
            context = self._recent_dialogue_context
            self._recent_dialogue_context = ""  # 消费后清除
            return self._make_thought(
                template=_THOUGHT_TEMPLATES[0],
                context=context[:200],
            )

        if self._recent_question_context:
            context = self._recent_question_context
            self._recent_question_context = ""
            return self._make_thought(
                template=_THOUGHT_TEMPLATES[1],
                context=context[:200],
            )

        if self._todo_context and self._todo_context != "（暂无待办）":
            return self._make_thought(
                template=_THOUGHT_TEMPLATES[2],
                context=self._todo_context[:200],
            )

        # 低概率触发联想或自发想法
        import random

        if random.random() < 0.3:
            return Thought(
                thought_id=uuid4().hex[:8],
                content="最近有没有什么事情跟以前处理过的某个问题很像？联想一下。",
                thought_type=ThoughtType.SITUATION_ASSOCIATION,
                importance=ThoughtImportance.LOW,
                source_context=f"idle_s={silent_s:.0f}s, no specific context",
            )

        return None

    def _make_thought(self, template: ThoughtTemplate, context: str) -> Thought:
        return Thought(
            thought_id=uuid4().hex[:8],
            content=template.prompt_template.format(context=context),
            thought_type=template.thought_type,
            importance=template.default_importance,
            source_context=context,
        )
