"""L8 Drive 持久化 — 追加写入 ~/.anan/layers/L8_drives/drive-state.jsonl"""
from __future__ import annotations
import json, logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LAYER_DIR = Path("~/.anan/layers/L8_drives").expanduser()
DRIVE_FILE = LAYER_DIR / "drive-state.jsonl"

def _ensure_dir():
    LAYER_DIR.mkdir(parents=True, exist_ok=True)

def persist_drive_event(
    event: str,
    drive_type: str,
    strength: float,
    active: bool,
    reason: str | None = None,
    event_count: int | None = None,
    satisfaction_rate: float | None = None,
) -> None:
    """追加一条 Drive 状态事件到 JSONL 文件。"""
    _ensure_dir()
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "drive_type": drive_type,
        "strength": round(strength, 4),
        "active": active,
        "reason": reason,
        "event_count": event_count,
        "satisfaction_rate": round(satisfaction_rate, 4) if satisfaction_rate is not None else None,
    }
    try:
        with open(DRIVE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.debug("Drive event persisted: [%s] %s (strength=%.3f active=%s)",
                     event, drive_type, strength, active)
    except Exception as exc:
        logger.warning("Failed to persist drive event: %s", exc)
