"""L6 Tuning 持久化 — 追加写入 ~/.anan/layers/L6_metacognition/tuning-log.jsonl"""
from __future__ import annotations
import json, logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LAYER_DIR = Path("~/.anan/layers/L6_metacognition").expanduser()
TUNING_FILE = LAYER_DIR / "tuning-log.jsonl"

def _ensure_dir():
    LAYER_DIR.mkdir(parents=True, exist_ok=True)

def persist_tuning(
    action_id: str,
    layer: str,
    target: str,
    old_value: float,
    new_value: float,
    reason: str,
    status: str,
    evaluation: str | None = None,
    rollback: bool = False,
) -> None:
    """追加一条 Tuning 日志到 JSONL 文件。"""
    _ensure_dir()
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action_id": action_id,
        "layer": layer,
        "target": target,
        "old_value": old_value,
        "new_value": new_value,
        "reason": reason,
        "status": status,
        "evaluation": evaluation,
        "rollback": rollback,
    }
    try:
        with open(TUNING_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.debug("Tuning persisted: [%s] %s %s %s→%s (%s)",
                     status, layer, target, old_value, new_value, "ROLLBACK" if rollback else "applied")
    except Exception as exc:
        logger.warning("Failed to persist tuning: %s", exc)
