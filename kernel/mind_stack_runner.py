"""
MindStack Runner — 九层总发动引擎
==================================

把九层 Mind Stack 启动起来的关键组件：

  Gateway Hook (gateway/builtin_hooks/mind_stack.py)
        ↓ 启动
  MindStackRunner
        ↓ 创建
    EventBus (全局单例)
        ↓
    CircadianLoop (L0 节律 → 触发 L1-L9 运转)
        ↓
    所有 Layer (L1-L9) 订阅 EventBus，各自运转

核心事件流：
  gateway:agent:end  →  gateway.message.sent  →  L5 PatternMiner 挖掘
  L0.circadian.tick  →  所有层各自更新状态
  L0.circadian.bedtime →  L1 Sleep + L5 PatternMiner.mine_now()
  L1.sleep.consolidated →  L2 Memory + L9 SelfModel 更新

为什么这样设计：
  - CircadianLoop 驱动 L0.tick，让九层有节奏地运转
  - Gateway 事件（用户消息、agent回复）注入 event bus，L5 能感知真实对话
  - 各层通过 event bus 解耦，互不直接调用
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from kernel.event_bus import Event, EventBus, get_bus
from kernel.circadian import CircadianConfig, CircadianLoop
from kernel.idle_detector import IdleDetector

logger = logging.getLogger("anan.kernel.mind_stack")


# ---------------------------------------------------------------------------
# Layer stubs — 各层入口（延迟导入避免循环）
# ---------------------------------------------------------------------------


class _LayerStub:
    """没有真实层实现时，记录期望 topic 的存根。"""

    def __init__(self, name: str, topics: list[str]):
        self.name = name
        self.topics = topics

    async def start(self, bus: EventBus) -> None:
        logger.debug("[%s] stub started, watching %s", self.name, self.topics)

    async def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# MindStackRunner — 九层总调度
# ---------------------------------------------------------------------------


class MindStackRunner:
    """
    九层 Mind Stack 的总发动引擎。

    负责：
    1. 创建全局 EventBus（如果还没有）
    2. 启动 CircadianLoop（L0 节律）
    3. 启动所有 Layer（L1-L9）
    4. 把 Gateway 事件转发到 EventBus

    Usage::

        runner = MindStackRunner(
            circadian_config=CircadianConfig(
                tick_interval_s=10.0,      # 每10秒一次心跳
                fatigue_per_tick=0.5,
                sleep_threshold=10.0,       # 20个tick后进入睡眠
            ),
            gateway_events=True,           # 把 gateway 事件注入 bus
        )
        await runner.start()
        # 九层在后台运转
        await runner.stop()
    """

    def __init__(
        self,
        *,
        circadian_config: Optional[CircadianConfig] = None,
        gateway_events: bool = True,
        idle_threshold_s: float = 30.0,
    ):
        self._circadian_cfg = circadian_config or CircadianConfig(
            tick_interval_s=10.0,
            fatigue_per_tick=0.5,
            sleep_threshold=10.0,
        )
        self._gateway_events = gateway_events
        self._idle_threshold_s = idle_threshold_s

        self._bus: Optional[EventBus] = None
        self._circadian_loop: Optional[CircadianLoop] = None
        self._idle_detector: Optional[IdleDetector] = None
        self._layers: list = []
        self._gateway_unsub: Optional[callable] = None
        self._running = False
        self._tasks: list[asyncio.Task] = []

        # L5 PatternMiner 实例
        self._pattern_miner = None
        # L1 DreamingPlugin 实例（供 sleep_fn 调用）
        self._dreaming_plugin = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def bus(self) -> EventBus:
        """全局 event bus。启动后可用。"""
        if self._bus is None:
            raise RuntimeError("MindStackRunner not started yet")
        return self._bus

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """启动九层。启动后 CircadianLoop 和各层在后台运行。"""
        if self._running:
            logger.warning("MindStack already running")
            return

        logger.info("🚼 启动九层 Mind Stack...")

        # 1. EventBus
        self._bus = EventBus()
        logger.info("  ✓ EventBus 就绪")

        # 2. CircadianLoop（L0 节律）
        sleep_fn = self._make_sleep_fn()
        self._circadian_loop = CircadianLoop(
            sleep_fn=sleep_fn,
            config=self._circadian_cfg,
            bus=self._bus,
        )
        logger.info("  ✓ CircadianLoop 就绪 (tick=%ss, threshold=%s)",
                     self._circadian_cfg.tick_interval_s,
                     self._circadian_cfg.sleep_threshold)

        # 3. 各层初始化
        await self._start_layers()
        logger.info("  ✓ %d 个层启动完成", len(self._layers))

        # 4. 统一调用所有层的 attach()（订阅事件总线）
        for layer in self._layers:
            if hasattr(layer, 'attach') and callable(getattr(layer, 'attach')):
                try:
                    await layer.attach()
                except Exception as exc:
                    logger.warning("  层 %s.attach() 失败: %s", type(layer).__name__, exc)

        # 5. Gateway 事件 → EventBus
        if self._gateway_events:
            self._wire_gateway_events()
            logger.info("  ✓ Gateway 事件注入就绪")

        # 6. 启动 CircadianLoop（非阻塞，在后台运转）
        loop_task = asyncio.create_task(self._circadian_loop.run())
        loop_task.add_done_callback(lambda t: self._tasks.remove(loop_task) if loop_task in self._tasks else None)
        self._tasks.append(loop_task)

        self._running = True
        logger.info("🚼 九层 Mind Stack 已启动，后台运转中")

    async def stop(self) -> None:
        """优雅停止九层。"""
        if not self._running:
            return

        logger.info("🛑 停止九层 Mind Stack...")
        self._running = False

        # 取消所有后台任务
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

        # 断开 gateway 事件
        if self._gateway_unsub:
            self._gateway_unsub()
            self._gateway_unsub = None

        # 停止各层
        for layer in reversed(self._layers):
            try:
                await layer.stop()
            except Exception:
                pass
        self._layers.clear()

        self._bus = None
        logger.info("🛑 九层 Mind Stack 已停止")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _start_layers(self) -> None:
        """启动所有认知层。优先真实实现，fallback 到 stub。"""

        # L9 SelfModel — 先启动（其他层会引用）
        try:
            from layers.L9_self.self_model import SelfModel
            self_model = SelfModel()
            self._layers.append(self_model)
            logger.info("  ✓ L9 SelfModel 就绪")
        except Exception as exc:
            logger.warning("  ✗ L9 SelfModel 启动失败: %s (使用 stub)", exc)
            self_model = None

        # L5 PatternMiner + PredictiveReasoner（依赖 L9 SelfModel）
        # PatternMiner 负责挖掘因果规则，PredictiveReasoner 负责执行预测
        try:
            from layers.L5_reasoning.pattern_miner import PatternMiner
            self._pattern_miner = PatternMiner(
                bus=self._bus,
                mine_on_event="L0.circadian.bedtime",
                self_model=self_model,
            )
            self._layers.append(self._pattern_miner)
            logger.info("  ✓ L5 PatternMiner 就绪")
        except Exception as exc:
            logger.warning("  ✗ L5 PatternMiner 启动失败: %s", exc)

        # L5 PredictiveReasoner — 基于 PatternMiner 发现的链路做预测
        try:
            from layers.L5_prediction.predictor import PredictiveReasoner
            causal_fn = None
            if hasattr(self, '_pattern_miner') and self._pattern_miner is not None:
                pm = self._pattern_miner
                causal_fn = lambda: list(pm.causal_reasoner.discovered_links.values()) \
                    if hasattr(pm, 'causal_reasoner') else []
            self._predictor = PredictiveReasoner(
                bus=self._bus,
                causal_links_fn=causal_fn or (lambda: []),
                self_model=self_model,
            )
            self._layers.append(self._predictor)
            logger.info("  ✓ L5 PredictiveReasoner 就绪")
        except Exception as exc:
            logger.warning("  ✗ L5 PredictiveReasoner 启动失败: %s", exc)

        # L1 Sleep
        try:
            from layers.L1_sleep.sleep_plugin import DreamingPlugin
            self._dreaming_plugin = DreamingPlugin(config={"enabled": True})
            self._layers.append(self._dreaming_plugin)
            logger.info("  ✓ L1 Sleep 就绪")
        except Exception as exc:
            logger.warning("  ✗ L1 Sleep 启动失败: %s", exc)

        # L2 Memory
        try:
            from layers.L2_memory.memory_tier import MemoryTier
            self._layers.append(MemoryTier(bus=self._bus))
            logger.info("  ✓ L2 Memory 就绪")
        except Exception as exc:
            logger.warning("  ✗ L2 Memory 启动失败: %s", exc)

        # L3 Attention — AttentionQueue and VigilanceMonitor
        # Keep _attention_queue as instance var so DriveSystem can call boost()
        try:
            from layers.L3_attention.attention import AttentionQueue, VigilanceMonitor
            self._attention_queue = AttentionQueue()
            self._layers.append(self._attention_queue)
            self._layers.append(VigilanceMonitor())
            logger.info("  ✓ L3 Attention (AttentionQueue + VigilanceMonitor) 就绪")
        except Exception as exc:
            logger.warning("  ✗ L3 Attention 启动失败: %s", exc)
            self._attention_queue = None

        # L4 Consciousness — 用 ConsciousnessEngine（完整引擎），不是 ThoughtStream（数据容器）
        try:
            from layers.L4_consciousness.consciousness import ConsciousnessEngine
            self._layers.append(ConsciousnessEngine(bus=self._bus, idle_threshold_s=120.0))
            logger.info("  ✓ L4 Consciousness 就绪")
        except Exception as exc:
            logger.warning("  ✗ L4 Consciousness 启动失败: %s", exc)

        # L6 Metacognition：PredictionMonitor + SelfTuner + Mirror
        # PredictionMonitor 监控 L5 预测准确率并触发链路衰减
        try:
            from layers.L6_metacognition.prediction_monitor import PredictionMonitor
            pm = PredictionMonitor(
                bus=self._bus,
                predictor=self._predictor if hasattr(self, '_predictor') else None,
            )
            # 注意：不手动 attach()，由下面的统一 attach() 循环处理
            self._layers.append(pm)
            logger.info("  ✓ L6 PredictionMonitor 就绪")
        except Exception as exc:
            logger.warning("  ✗ L6 PredictionMonitor 启动失败: %s", exc)

        # SelfTuner 订阅 L6.metacognition.warn 做元认知调参
        try:
            from layers.L6_metacognition.self_tuner import SelfTuner
            self._layers.append(SelfTuner(
                bus=self._bus,
                predictor=self._predictor if hasattr(self, '_predictor') else None,
                pattern_miner=self._pattern_miner if hasattr(self, '_pattern_miner') else None,
            ))
            logger.info("  ✓ L6 SelfTuner 就绪")
        except Exception as exc:
            logger.warning("  ✗ L6 SelfTuner 启动失败: %s", exc)

        # Mirror — L6 元认知镜子，发 HealthReport 事件供 L7 Goals 消费
        # 依赖 self_model (L9)，所以在 L9 启动后加入
        try:
            from layers.L6_metacognition.mirror import Mirror
            mirror = Mirror(
                bus=self._bus,
                self_model=self_model if hasattr(self, 'self_model') else None,
                # working_memory 暂不传，Mirror 会跳过该项指标
            )
            self._layers.append(mirror)
            logger.info("  ✓ L6 Mirror 就绪")
        except Exception as exc:
            logger.warning("  ✗ L6 Mirror 启动失败: %s", exc)

        # L7 Goals
        try:
            from layers.L7_goals.goal_engine import GoalGenerator
            goal_generator = GoalGenerator(bus=self._bus, self_model=self_model)
            self._layers.append(goal_generator)
            logger.info("  ✓ L7 Goals 就绪")
        except Exception as exc:
            logger.warning("  ✗ L7 Goals 启动失败: %s", exc)
            goal_generator = None

        # L7 Will — SelfRegulator 监听 L6.warn 并调节
        # 同时监听 L7.goal.achieved / .abandoned（GoalGenerator 发出的）
        try:
            from layers.L7_will.regulator import SelfRegulator
            self._layers.append(SelfRegulator(
                bus=self._bus,
                intent_stack=self._intent_stack if hasattr(self, '_intent_stack') else None,
            ))
            logger.info("  ✓ L7 Will 就绪")
        except Exception as exc:
            logger.warning("  ✗ L7 Will 启动失败: %s", exc)

        # L8 Drives
        try:
            from layers.L8_drives.drive_system import DriveSystem
            self._drive_system = DriveSystem(bus=self._bus)
            self._layers.append(self._drive_system)
            logger.info("  ✓ L8 Drives 就绪")
        except Exception as exc:
            logger.warning("  ✗ L8 Drives 启动失败: %s", exc)
            self._drive_system = None

        # L8 Intent — anan 持续在意的渴望
        # decay_tick() 每 tick 衰减，snapshot() 在 luciddream 时触发
        try:
            from layers.L8_intent.intent_stack import IntentStack
            self._intent_stack = IntentStack(bus=self._bus)
            self._layers.append(self._intent_stack)
            logger.info("  ✓ L8 Intent 就绪")
        except Exception as exc:
            logger.warning("  ✗ L8 Intent 启动失败: %s", exc)
            self._intent_stack = None

        # AttentionBridge — 连接 DriveSystem 和 AttentionQueue
        try:
            from layers.L8_drives.attention_bridge import AttentionBridge
            bridge = AttentionBridge(
                bus=self._bus,
                attention_q=self._attention_queue if hasattr(self, '_attention_queue') else None,
                drive_system=self._drive_system if hasattr(self, '_drive_system') else None,
            )
            self._layers.append(bridge)
            logger.info("  ✓ AttentionBridge 就绪")
        except Exception as exc:
            logger.warning("  ✗ AttentionBridge 启动失败: %s", exc)

        # ---- 层间事件连线（必须在各层 attach() 之后） ----
        # L0.circadian.tick → L8 IntentStack.decay_tick() (每 tick 自然衰减)
        # L0.circadian.tick → L8 DriveSystem.decay() (驱动力衰减)
        # L1.lucid_dream.ended → L8 IntentStack.snapshot() (顶层意图快照供梦境用)
        await self._wire_layer_events()

    def _wire_gateway_events(self) -> None:
        """
        把 Gateway hook 事件注入 EventBus。

        gateway:agent:end (context has platform, user, text, response)
          → gateway.message.sent (所有层都能感知到一次完整对话)
        """
        from gateway.hooks import get_global_registry
        registry = get_global_registry()

        async def _on_agent_end(event_type: str, context: dict) -> None:
            """把 gateway:agent:end 转换为内部事件。"""
            if self._bus is None:
                return
            payload = {
                "platform": context.get("platform", "unknown"),
                "user": context.get("user", "unknown"),
                "text": context.get("text", ""),
                "response": context.get("response", ""),
                "session_id": context.get("session_id", ""),
                "ts": time.time(),
            }
            await self._bus.publish(Event(
                topic="gateway.message.sent",
                source="gateway",
                payload=payload,
            ))
            # 发 L0.circadian.tick 让 PatternMiner 有数据可挖
            await self._bus.publish(Event(
                topic="L0.circadian.tick",
                source="gateway",
                payload={"reason": "message", **payload},
            ))

        if registry is None:
            logger.warning("Gateway HookRegistry not yet initialized, will retry on next startup")
            return

        try:
            registry._handlers.setdefault("agent:end", []).append(_on_agent_end)
            logger.info("  ✓ Gateway agent:end → EventBus 注入已连接")
        except Exception as exc:
            logger.warning("  ✗ 无法注册 gateway 事件注入: %s", exc)

    async def _wire_layer_events(self) -> None:
        """
        层间事件连线 — 让各层真正联动起来。

        L0.circadian.tick → L8 IntentStack.decay_tick()   (意图自然衰减)
        L0.circadian.tick → L8 DriveSystem.decay()         (驱动力自然衰减)
        L1.lucid_dream.ended → L8 IntentStack.snapshot()  (快照供梦境规划)
        """
        intent_stack = getattr(self, '_intent_stack', None)
        drive_system = getattr(self, '_drive_system', None)

        # L0.circadian.tick → decay
        async def _on_tick_for_decay(event: Event):
            if intent_stack is not None:
                await intent_stack.decay_tick()
            if drive_system is not None:
                drive_system.decay()

        self._bus.subscribe("L0.circadian.tick", _on_tick_for_decay)
        logger.info("  ✓ L0.tick → L8 IntentStack/DriveSystem decay 已连接")

        # L1.lucid_dream.ended → snapshot
        if intent_stack is not None:

            async def _on_lucid_dream_ended(event: Event):
                await intent_stack.snapshot()

            self._bus.subscribe("L1.lucid_dream.ended", _on_lucid_dream_ended)
            logger.info("  ✓ L1.lucid_dream.ended → L8 IntentStack.snapshot() 已连接")

    def _make_sleep_fn(self):
        """
        制造 sleep_fn 传给 CircadianLoop。
        在睡前触发 L1 Sleep，唤醒后触发 L5 PatternMiner 挖掘。
        """
        # Find MemoryTier from already-populated self._layers
        def _find_memory_tier():
            for layer in self._layers:
                from layers.L2_memory.memory_tier import MemoryTier
                if isinstance(layer, MemoryTier):
                    return layer
            return None

        async def sleep_fn(day: str, bus: EventBus, cycle: int) -> int:
            logger.info("🌙 [Cycle %d] 进入睡眠阶段...", cycle)
            try:
                # 1. 触发 L1 DreamingPlugin — 先 deep 再 light
                if self._dreaming_plugin is not None:
                    try:
                        import os
                        workspace = os.path.expanduser("~/.anan")
                        os.makedirs(workspace, exist_ok=True)
                        # Deep Sleep: promote short-term → mid-term → long-term
                        await self._dreaming_plugin.run_dreaming_sweep(
                            workspace_dir=workspace,
                            phase="deep",
                        )
                        # 同时手动调用 MemoryTier promote_all_short_to_mid()
                        memory_tier = _find_memory_tier()
                        if memory_tier is not None:
                            await memory_tier.promote_all_short_to_mid()
                        # Light Sleep: ingest daily/sessions/recall signals
                        await self._dreaming_plugin.run_dreaming_sweep(
                            workspace_dir=workspace,
                            phase="light",
                        )
                    except Exception as exc:
                        logger.warning("  L1 DreamingPlugin 执行失败: %s", exc)
                # 发 bedtime 事件（PatternMiner 订阅了这个）
                await bus.publish(Event(
                    topic="L0.circadian.bedtime",
                    source="circadian",
                    payload={"day": day, "cycle": cycle},
                ))
                logger.info("🌙 [Cycle %d] 睡眠阶段完成", cycle)
                return 0
            except Exception as exc:
                logger.warning("  L1 Sleep 执行失败: %s", exc)
                return 0

        return sleep_fn
