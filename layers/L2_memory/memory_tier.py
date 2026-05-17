"""
L2 Memory Hierarchy — 记忆分层
================================

三层老化 + promote 链路：

  Working Memory (L3)   ← 当前对话，上下文直读
       ↓ promote (小时级或 L1 Light Sleep 触发)
  Short-term (recall-store.json)   ← 几天内可召回
       ↓ promote (L1 Deep Sleep 触发)
  Mid-term (周记/月记)   ← 事件摘要，可RAG召回
       ↓ promote (显式重要标记)
  Long-term (MEMORY.md) ← 极长期知识

设计原则：
  - 增量 promote，不做全量重写
  - 每条记忆带 access_count / importance / last_accessed
  - promotion 前做摘要压缩（保留语义，删细节）
  - 不覆盖 /root/.anan/MEMORY.md，只 append（原子写）
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from kernel.event_bus import Event, EventBus
from layers.L2_memory.recall_signal_advisor import RecallSignalAdvisor, RecallSignal

logger = logging.getLogger("anan.L2")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MemoryItem:
    content: str
    importance: float          # 0-1, 主观重要度
    access_count: int = 0
    created_at: float = 0.0
    last_accessed: float = 0.0
    tags: list[str] = None     # ["user", "lesson", "fact"]
    source: str = "unknown"    # "conversation" | "insight" | "event"

    def __post_init__(self):
        if self.tags is None:
            self.tags = []
        if self.created_at == 0.0:
            self.created_at = time.time()
        if self.last_accessed == 0.0:
            self.last_accessed = time.time()

    def touch(self):
        self.access_count += 1
        self.last_accessed = time.time()

    def summary(self, max_chars: int = 200) -> str:
        if len(self.content) <= max_chars:
            return self.content
        return self.content[:max_chars].rsplit(" ", 1)[0] + "…"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class MemoryStore:
    """One JSON file per tier — simple, diff-friendly, no DB dependency."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, MemoryItem] = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            with open(self.path) as f:
                raw = json.load(f)
            for k, v in raw.items():
                self._items[k] = MemoryItem(**v)
        except Exception as exc:
            logger.warning("MemoryStore load failed (starting fresh): %s", exc)

    def _save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump({k: asdict(v) for k, v in self._items.items()}, f, indent=2, ensure_ascii=False)
            tmp.rename(self.path)
        except Exception as exc:
            logger.error("MemoryStore save failed: %s", exc)

    # ---- CRUD ----

    def put(self, key: str, item: MemoryItem) -> None:
        self._items[key] = item
        self._save()

    def get(self, key: str) -> Optional[MemoryItem]:
        item = self._items.get(key)
        if item:
            item.touch()
            self._save()
        return item

    def remove(self, key: str) -> bool:
        if key in self._items:
            del self._items[key]
            self._save()
            return True
        return False

    def all(self) -> list[MemoryItem]:
        return list(self._items.values())

    def search(self, query: str, top_k: int = 5) -> list[tuple[MemoryItem, float]]:
        """Keyword substring matching + importance/recency scoring."""
        q_lower = query.lower()
        scored = []
        for item in self._items.values():
            content_lower = item.content.lower()
            # Use substring match for Chinese; fall back to word overlap for English
            if q_lower in content_lower:
                overlap = 1.0
            else:
                q_words = set(q_lower.split())
                content_words = set(content_lower.split())
                overlap = len(q_words & content_words)
                if overlap == 0:
                    continue

            # factor in importance and recency
            age_h = (time.time() - item.last_accessed) / 3600
            recency = 1.0 / (1.0 + age_h * 0.1)
            score = overlap * (0.5 + 0.3 * item.importance + 0.2 * recency)
            scored.append((item, score))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def size(self) -> int:
        return len(self._items)

    def cull(self, max_items: int) -> int:
        """Remove lowest-importance items if over max_items. Returns count removed."""
        if len(self._items) <= max_items:
            return 0
        # Remove oldest accessed, lowest importance items first
        items = sorted(self._items.items(), key=lambda kv: (kv[1].importance, kv[1].last_accessed))
        removed = 0
        for k, _ in items[:len(self._items) - max_items]:
            del self._items[k]
            removed += 1
        if removed:
            self._save()
        return removed


# ---------------------------------------------------------------------------
# Tier manager
# ---------------------------------------------------------------------------

RECALL_PATH = Path("/root/.anan/recall-store.json")
MIDTERM_DIR = Path("/root/.anan/midterm/")
LONGTERM_PATH = Path("/root/.anan/MEMORY.md")
SHORT_MAX = 200          # recall-store max items before culling
MIDTERM_WEEKS = 4       # consolidate after 4 weeks of entries


class MemoryTier:
    """Manage the three active tiers: short, mid, long.

    Paths can be overridden via constructor for testing.
    """

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        recall_path: Optional[Path] = None,
        midterm_dir: Optional[Path] = None,
        longterm_path: Optional[Path] = None,
    ):
        self._bus = bus
        self._unsubs: list[Callable] = []
        self._recall_advisor = RecallSignalAdvisor()
        self.short = MemoryStore(recall_path or RECALL_PATH)
        self.midterm_dir = (midterm_dir or MIDTERM_DIR)
        self.midterm_dir.mkdir(parents=True, exist_ok=True)
        self._long_path = longterm_path or LONGTERM_PATH

    def set_delegate(self, fn: callable) -> None:
        """Inject delegate_task for RecallSignalAdvisor subagent calls."""
        self._recall_advisor.set_delegate(fn)

    async def evaluate_memorization(
        self,
        content: str,
        current_importance: float = 0.5,
        access_count: int = 0,
        age_hours: float = 0.0,
        context_tags: Optional[list[str]] = None,
    ) -> RecallSignal:
        """Evaluate whether content should be memorized and promoted.

        Falls back to rule-based evaluation if no delegate is configured.
        """
        return await self._recall_advisor.evaluate(
            content=content,
            current_importance=current_importance,
            access_count=access_count,
            age_hours=age_hours,
            context_tags=context_tags,
        )

    async def attach(self, bus: Optional[EventBus] = None) -> None:
        """Subscribe to cognitive events from L5/L9 to drive memory recall."""
        b = bus or self._bus
        if b is None:
            logger.warning("MemoryTier.attach() called with no bus")
            return
        self._bus = b

        self._unsubs.append(b.subscribe("L5.prediction.confirmed", self._on_prediction_confirmed))
        self._unsubs.append(b.subscribe("L5.prediction.failed", self._on_prediction_failed))
        self._unsubs.append(b.subscribe("L5.causal.link_discovered", self._on_causal_link))
        self._unsubs.append(b.subscribe("L5.pattern.discovered", self._on_pattern_discovered))
        self._unsubs.append(b.subscribe("L9.self.updated", self._on_self_updated))

    async def detach(self) -> None:
        for r in self._unsubs:
            r()
        self._unsubs.clear()

    # ---- Cognitive event handlers ----

    async def _on_prediction_confirmed(self, event: Event) -> None:
        """When a causal prediction is confirmed, memorize the rule as insight."""
        p = event.payload or {}
        cause = p.get("cause", "?")
        effect = p.get("effect", "?")
        lift = p.get("lift", 0.0)
        key = f"causal_rule:{cause}:{effect}"
        content = f"因果确认：{cause} → {effect}（lift={lift}）"
        importance = min(1.0, 0.5 + lift * 0.1)
        self.memorize(key, content, importance=importance,
                      tags=["causal", "confirmed"], source="insight")
        logger.debug("Memorized confirmed causal rule: %s", key)

    async def _on_prediction_failed(self, event: Event) -> None:
        """When a prediction fails, store as a negative lesson."""
        p = event.payload or {}
        cause = p.get("cause", "?")
        effect = p.get("effect", "?")
        key = f"failed_prediction:{cause}:{effect}"
        content = f"预测失败：{cause} → {effect}（未发生）"
        self.memorize(key, content, importance=0.6,
                      tags=["causal", "failed"], source="insight")
        logger.debug("Memorized failed prediction: %s", key)

    async def _on_causal_link(self, event: Event) -> None:
        """When a new causal link is discovered, store as high-importance rule."""
        p = event.payload or {}
        cause = p.get("cause", "?")
        effect = p.get("effect", "?")
        lift = p.get("lift", 1.0)
        confidence = p.get("confidence", 0.0)
        key = f"causal_link:{cause}:{effect}"
        content = f"因果链路：{cause} → {effect}（lift={lift}, conf={confidence}）"
        importance = min(1.0, 0.4 + confidence * 0.4 + (lift - 1.0) * 0.1)
        self.memorize(key, content, importance=importance,
                      tags=["causal", "rule"], source="causal_discovery")
        logger.debug("Memorized causal link: %s", key)

    async def _on_pattern_discovered(self, event: Event) -> None:
        """When a pattern is mined from memory logs, store as a reflection."""
        p = event.payload or {}
        pattern = p.get("pattern", str(p))
        key = f"pattern:{pattern[:40]}"
        content = f"模式发现：{pattern}"
        self.memorize(key, content, importance=0.5,
                      tags=["pattern", "reflection"], source="insight")
        logger.debug("Memorized pattern: %s", key)

    async def _on_self_updated(self, event: Event) -> None:
        """When self-model updates, memorize identity/history changes."""
        p = event.payload or {}
        identity = p.get("identity_facts", [])
        history = p.get("history_facts", [])
        vision = p.get("vision_facts", [])

        for fact in identity:
            key = f"identity:{fact[:60]}"
            self.memorize(key, fact, importance=0.8,
                          tags=["identity"], source="self_model")
        for fact in history:
            key = f"history:{fact[:60]}"
            self.memorize(key, fact, importance=0.7,
                          tags=["history"], source="self_model")
        for fact in vision:
            key = f"vision:{fact[:60]}"
            self.memorize(key, fact, importance=0.7,
                          tags=["vision"], source="self_model")
        if identity or history or vision:
            logger.debug("Memorized %d self-model facts", len(identity) + len(history) + len(vision))

    # ---- Short-term ----

    def recall(self, query: str, top_k: int = 5) -> list[str]:
        """Search short-term recall store. Returns list of content strings."""
        results = self.short.search(query, top_k)
        return [item.content for item, _ in results]

    def remember(self, key: str) -> Optional[str]:
        item = self.short.get(key)
        return item.content if item else None

    def memorize(self, key: str, content: str, importance: float = 0.5,
                tags: Optional[list[str]] = None, source: str = "conversation") -> None:
        item = MemoryItem(content=content, importance=importance, tags=tags or [], source=source)
        self.short.put(key, item)

    # ---- Promotion: short → mid ----

    def _week_key(self, dt: datetime) -> str:
        year, week, _ = dt.isocalendar()
        return f"{year}-W{week:02d}"

    def _month_key(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m")

    def promote_short_to_mid(self, item: MemoryItem) -> None:
        """Write a single MemoryItem into the appropriate weekly file."""
        created = datetime.fromtimestamp(item.created_at)
        week_key = self._week_key(created)
        month_key = self._month_key(created)

        month_path = self.midterm_dir / f"{month_key}.json"
        entries: dict[str, dict] = {}
        if month_path.exists():
            try:
                entries = json.loads(month_path.read_text())
            except Exception:
                pass

        entry_key = f"{week_key}:{item.content[:60]}"
        if entry_key in entries:
            # bump importance if already present
            entries[entry_key]["importance"] = max(entries[entry_key]["importance"], item.importance)
            entries[entry_key]["access_count"] += item.access_count
        else:
            entries[entry_key] = asdict(item)

        month_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False))

    async def promote_all_short_to_mid(self) -> int:
        """Called by L1 Deep Sleep. Returns count of items promoted."""
        items = self.short.all()
        promoted = 0
        for item in items:
            self.promote_short_to_mid(item)
            promoted += 1
        self.short.cull(max_items=0)  # clear short-term after promotion
        logger.info("Promoted %d items to mid-term", promoted)
        if self._bus is not None:
            await self._bus.publish(Event(
                topic="L2.memory.persisted",
                source="L2.memory_tier",
                payload={"tier": "mid", "count": promoted},
            ))
        return promoted

    # ---- Promotion: mid → long (MEMORY.md append) ----

    async def append_longterm(self, insight: str, tags: Optional[list[str]] = None) -> None:
        """Append a fact/insight to the long-term MEMORY.md (never overwrites)."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        tag_str = " #" + " #".join(tags) if tags else ""
        entry = f"\n## {timestamp}{tag_str}\n{insight}\n"
        try:
            with open(self._long_path, "a") as f:
                f.write(entry)
            if self._bus is not None:
                await self._bus.publish(Event(
                    topic="L2.memory.persisted",
                    source="L2.memory_tier",
                    payload={"tier": "long", "count": 1},
                ))
        except Exception as exc:
            logger.error("Failed to append to MEMORY.md: %s", exc)

    async def promote_mid_to_long(self, week_filter: Optional[str] = None) -> int:
        """Summarize week's mid-term entries into long-term. Returns count appended."""
        count = 0
        for path in sorted(self.midterm_dir.glob("*.json")):
            if week_filter and path.stem != week_filter:
                continue
            try:
                entries: dict[str, dict] = json.loads(path.read_text())
                if not entries:
                    continue
                summaries = [v["content"][:120] for v in entries.values()]
                joined = "; ".join(summaries[:10])
                await self.append_longterm(
                    f"[周总结] {path.stem}: {joined}",
                    tags=["midterm", "summary"]
                )
                count += 1
            except Exception as exc:
                logger.warning("Failed to promote %s: %s", path, exc)
        return count

    # ---- Stats ----

    def stats(self) -> dict:
        return {
            "short_count": self.short.size(),
            "midterm_files": len(list(self.midterm_dir.glob("*.json"))),
            "longterm_exists": self._long_path.exists(),
        }
