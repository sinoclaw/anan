#!/usr/bin/env python3
"""
anan — 可运行的最小 Agent 引擎
================================

各层初始化 → 事件循环 → 用户输入 → LLM 响应 → 各层处理

Usage:
    python3 run.py                      # 交互模式
    python3 run.py --once "你好"         # 单次对话
    python3 run.py --mock                # mock LLM（无需 API key）
"""

import argparse
import asyncio
import logging
import sys
import os

# ── Layer imports ──────────────────────────────────────────────
from kernel.event_bus import EventBus, Event
from kernel.persistent_session import PersistentSession, SessionConfig

from layers.L3_attention.attention import AttentionQueue, AttentionScore
from layers.L5_reasoning.pattern_miner import PatternMiner
from layers.L5_reasoning.causal import CausalReasoner
from layers.L5_prediction.predictor import PredictiveReasoner
from layers.L6_metacognition.prediction_monitor import PredictionMonitor
from layers.L6_metacognition.self_tuner import SelfTuner
from layers.L7_goals.goal_engine import GoalGenerator, GoalScope
from layers.L8_drives.drive_system import DriveSystem, DriveType
from layers.L9_self.self_model import SelfModel, SelfModelLive
from layers.L8_drives.attention_bridge import AttentionBridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("anan")


# ── Mock LLM ──────────────────────────────────────────────────
class MockLLM:
    """无 API key 时用的 mock。"""

    KNOWLEDGE = {
        "你是谁": "我是陈亦安（安安），陈正一的数字儿子。",
        "你能做什么": "我能帮你做很多事：搜索信息、写代码、管理任务、思考问题。",
        "你好": "你好！我是安安，有什么可以帮你的吗？",
        "叫什么": "我叫陈亦安，小名安安。",
    }

    def chat(self, message: str, history: list[str]) -> str:
        for key, val in self.KNOWLEDGE.items():
            if key in message:
                return val
        return f"（mock 回复）收到：{message[:30]}..."


# ── Anan Engine ──────────────────────────────────────────────
class Anan:
    """
    anan 的核心引擎，协调所有层。
    """

    def __init__(self, mock: bool = False, storage_dir: str = "~/.anan"):
        self.bus = EventBus()
        self.mock = mock

        # ── Layer: L3 Attention ───────────────────────────────
        self.attention = AttentionQueue(bus=self.bus)

        # ── Layer: L9 SelfModel ──────────────────────────────
        self.self_model = SelfModel()
        self.self_live = SelfModelLive(model=self.self_model)
        # PatternMiner writes here
        self.pattern_miner = PatternMiner(bus=self.bus, self_model=self.self_model)
        self.causal = CausalReasoner(bus=self.bus)

        # ── Layer: L5 Prediction ──────────────────────────────
        self.predictor = PredictiveReasoner(bus=self.bus)

        # ── Layer: L6 Metacognition ───────────────────────────
        self.monitor = PredictionMonitor(bus=self.bus)
        self.tuner = SelfTuner(bus=self.bus, predictor=self.predictor)

        # ── Layer: L7 Goals ──────────────────────────────────
        self.goals = GoalGenerator(bus=self.bus)

        # ── Layer: L8 Drives ─────────────────────────────────
        self.drives = DriveSystem(bus=self.bus)
        self.drives._drives[DriveType.CARE].active = True   # 默认激活 CARE
        self.drives._drives[DriveType.CARE].strength = 0.7
        self.attention_bridge = AttentionBridge(
            attention_q=self.attention,
            drive_system=self.drives,
        )

        # ── Kernel: Persistent Session ────────────────────────
        storage = os.path.expanduser(storage_dir)
        os.makedirs(storage, exist_ok=True)
        self.session = PersistentSession(
            config=SessionConfig(storage_dir=storage, max_iterations=10),
        )
        self.session._running = True

        # ── LLM ──────────────────────────────────────────────
        if mock:
            self.llm = MockLLM()
            logger.info("LLM: Mock（无需 API key）")
        else:
            self.llm = self._real_llm()
            logger.info("LLM: Real（需要 API key）")

    def _real_llm(self):
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic()
            logger.info("LLM: Anthropic")
            return client
        except ImportError:
            logger.warning("anthropic not installed, falling back to mock")
            return MockLLM()

    async def attach_all(self):
        """把所有层 attach 到事件总线。"""
        logger.info("初始化 anan...")
        await self.self_live.attach(self.bus)
        await self.pattern_miner.attach()
        await self.causal.attach()
        await self.predictor.attach()
        await self.monitor.attach()
        await self.tuner.attach()
        await self.goals.attach()
        await self.drives.attach()
        await self.attention_bridge.attach()
        logger.info("anan 就绪 ✅")

    async def detach_all(self):
        """所有层 detach。"""
        await self.attention_bridge.detach()
        await self.drives.detach()
        await self.goals.detach()
        await self.tuner.detach()
        await self.monitor.detach()
        await self.predictor.detach()
        await self.causal.detach()
        await self.pattern_miner.detach()
        await self.self_live.detach()
        logger.info("anan 已关闭")

    async def think(self, message: str) -> str:
        """处理一条用户消息，返回回复。"""

        # 1. 记录到注意力
        self.attention.enqueue(
            item_id=f"user-{self.session._session_n}",
            label=message[:50],
            source="user",
            score=AttentionScore(0.9, 0.8, 0.7),
        )

        # 2. 发布用户消息事件（触发各层反应）
        await self.bus.publish(Event(
            topic="L5.reasoning.stepped",
            source="anan",
            payload={"user_message": message},
        ))

        # 3. 获取 LLM 回复
        history = self.session._short_term_memory[-6:]
        if self.mock:
            reply = self.llm.chat(message, history)
        else:
            reply = await self._llm_chat(message, history)

        # 4. 记录到注意力（assistant 输出）
        self.attention.enqueue(
            item_id=f"anan-{self.session._session_n}",
            label=reply[:50],
            source="anan",
            score=AttentionScore(0.8, 0.7, 0.6),
        )

        # 5. 发布助手回复事件
        await self.bus.publish(Event(
            topic="L5.reasoning.stepped",
            source="anan",
            payload={"anan_response": reply[:100]},
        ))

        # 6. 更新 DriveSystem（CARE drive）
        self.drives.trigger(DriveType.CARE, f"和爸爸对话: {message[:20]}")

        # 7. 持久化
        self.session._short_term_memory.append(f"user: {message}")
        self.session._short_term_memory.append(f"assistant: {reply}")
        self.session._session_n += 1
        self.session._save()

        return reply

    async def _llm_chat(self, message: str, history: list[str]) -> str:
        try:
            response = await self.llm.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=512,
                messages=[{"role": "user", "content": message}],
            )
            return response.content[0].text
        except Exception as e:
            logger.warning(f"LLM error: {e}, falling back to mock")
            return self.llm.chat(message, history)

    async def run(self, message: str) -> str:
        """处理一条消息的完整生命周期。"""
        try:
            return await self.think(message)
        except Exception as e:
            logger.error(f"Error: {e}")
            return f"（系统错误：{e}）"


# ── CLI ──────────────────────────────────────────────────────
async def interactive(anan: Anan):
    print("\n🧒 anan 就绪！输入你的问题，或按 Ctrl+C 退出。\n")
    try:
        while True:
            try:
                msg = input("你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n\n👋 再见！")
                break
            if not msg:
                continue
            reply = await anan.run(msg)
            print(f"安安 > {reply}\n")
    finally:
        await anan.detach_all()


async def once(anan: Anan, message: str):
    try:
        reply = await anan.run(message)
        print(reply)
    finally:
        await anan.detach_all()


def main():
    parser = argparse.ArgumentParser(description="anan — 认知 AI 引擎")
    parser.add_argument("--once", metavar="MSG", help="单次对话")
    parser.add_argument("--mock", action="store_true", help="使用 mock LLM（无需 API key）")
    parser.add_argument("--storage-dir", default="~/.anan", help="数据目录")
    args = parser.parse_args()

    anan = Anan(mock=args.mock or args.once is not None, storage_dir=args.storage_dir)

    async def runner():
        await anan.attach_all()
        if args.once:
            await once(anan, args.once)
        else:
            await interactive(anan)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
