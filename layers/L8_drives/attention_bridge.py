"""
L8 DriveSystem → L3 AttentionQueue 桥接
========================================

愿景：驱动力实时影响注意力优先级。

流程：
  DriveSystem 检测到驱动力激活
      → L8.drive.updated 事件
          → AttentionBridge 接收
              → 查匹配的 AttentionItem（通过 goal_tags）
                  → AttentionQueue.boost() 加分 + 升级优先级

使用方式：
    bridge = AttentionBridge(bus, attention_q, drive_system)
    await bridge.attach()

    # 或手动：
    bridge.on_drive_updated({"drive": "CARE", "active": True, "goal_tags": ["爸爸"]})
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional

from kernel.event_bus import EventBus, Event, get_bus

if TYPE_CHECKING:
    from layers.L3_attention.attention import AttentionQueue, AttentionScore
    from layers.L8_drives.drive_system import DriveSystem

logger = logging.getLogger("anan.l8_bridge")


class AttentionBridge:
    """L8 DriveSystem ↔ L3 AttentionQueue 桥接器。"""

    def __init__(
        self,
        bus: Optional[EventBus] = None,
        attention_q: Optional["AttentionQueue"] = None,
        drive_system: Optional["DriveSystem"] = None,
    ):
        self._bus = bus or get_bus()
        self._q = attention_q
        self._ds = drive_system
        self._unsubs: list = []

    def set_attention_queue(self, q: "AttentionQueue") -> None:
        self._q = q

    def set_drive_system(self, ds: "DriveSystem") -> None:
        self._ds = ds

    async def attach(self) -> None:
        """监听 L8.drive.updated，驱动激活时 boost 匹配的注意力项。"""
        if self._q is None:
            logger.warning("AttentionBridge.attach: no attention queue, not subscribing")
            return

        self._unsubs.append(
            self._bus.subscribe("L8.drive.updated", self._on_drive_updated)
        )
        logger.info("AttentionBridge attached (drive→attention)")

    async def stop(self) -> None:
        """供 MindStackRunner 调用。"""
        await self.detach()

    async def detach(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()

    # -------------------------------------------------------------------------
    # Drive signal → Attention boost
    # -------------------------------------------------------------------------

    def _on_drive_updated(self, event: Event) -> None:
        """L8.drive.updated → boost 对应的注意力项，或新建一个。

        完整链路：L8 drive 激活 → AttentionBridge → L3.attention.queued → L4 反思
        """
        if self._q is None:
            return

        payload = event.payload or {}
        active = payload.get("active", False)
        drive_name = payload.get("drive", "UNKNOWN")
        drive_type = payload.get("drive_type", "UNKNOWN")
        goal_tags = payload.get("goal_tags", [])
        reason = payload.get("reason", "")

        if not active:
            # 驱动抑制：不做降级（boost 只升不降，维持简单性）
            logger.debug("Drive %s deactivated, no attention change", drive_name)
            return

        # 计算该驱动的 priority_boost
        boost_amount = 0.0
        if self._ds is not None:
            boost_amount = self._ds.priority_boost(goal_tags)
        else:
            # 无 DriveSystem 时，用默认加成
            boost_amount = 0.15

        # 遍历注意力队列，找 source 或 label 包含 goal_tag 的项
        boosted = 0
        for item in self._q._items:  # noqa: SLF801 — internal access for bridge
            if any(tag.lower() in (item.source.lower() + item.label.lower()) for tag in goal_tags):
                self._q.boost(item.id, extra_score=boost_amount)
                boosted += 1

        # 如果没有匹配项，则新建一个注意力项（形成 L8→L3 联动）
        if boosted == 0 and goal_tags:
            item_id = f"drive-{drive_name.lower()}-{int(time.time())}"
            label = f"驱动力激活：{drive_type}（{reason[:40] if reason else '自动'}）"
            # urgency=boost_amount*2 让 boost 大的 drive 有更高 urgency
            score = AttentionScore(
                urgency=min(boost_amount * 2.0, 1.0),
                importance=min(boost_amount * 1.5, 1.0),
                interest=0.4,
            )
            self._q.enqueue(
                item_id=item_id,
                label=label,
                source=f"L8:{drive_name}",
                score=score,
                ttl_s=120.0,
            )
            logger.info(
                "Drive %s activated → enqueued new attention item '%s' (boost=%.2f)",
                drive_name, item_id, boost_amount
            )
            boosted = 1

        if boosted > 0:
            self._bus.publish_sync(Event(
                topic="L3.attention.drive_boost",
                source="L8.bridge",
                payload={
                    "drive": drive_name,
                    "boosted_count": boosted,
                    "boost": boost_amount,
                },
            ))
