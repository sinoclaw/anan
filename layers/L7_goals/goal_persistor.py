"""L7 Goal 持久化 — 追加写入 ~/.anan/layers/L7_goals/goal-state.jsonl"""
from __future__ import annotations
import json, logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LAYER_DIR = Path("~/.anan/layers/L7_goals").expanduser()
GOAL_FILE = LAYER_DIR / "goal-state.jsonl"

def _ensure_dir():
    LAYER_DIR.mkdir(parents=True, exist_ok=True)

def persist_goal_event(
    event: str,
    goal_id: str,
    description: str,
    completed: bool = False,
    completed_at: str | None = None,
    progress: int | None = None,
    reason: str | None = None,
    scope: str | None = None,
    tags: list[str] | None = None,
    sub_goals: list | None = None,
) -> None:
    """追加一条 Goal 生命周期事件到 JSONL 文件。"""
    _ensure_dir()
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "goal_id": goal_id,
        "description": description,
        "completed": completed,
        "completed_at": completed_at,
        "progress": progress,
        "reason": reason,
        "scope": scope,
        "tags": tags or [],
        "sub_goals": sub_goals,
    }
    try:
        with open(GOAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.debug("Goal event persisted: [%s] %s — %s", event, goal_id, description)
    except Exception as exc:
        logger.warning("Failed to persist goal event: %s", exc)
