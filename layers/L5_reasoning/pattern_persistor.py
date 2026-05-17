"""L5 Pattern 持久化 — 追加写入 ~/.anan/layers/L5_reasoning/patterns.jsonl"""
from __future__ import annotations
import json, logging, os
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LAYER_DIR = Path("~/.anan/layers/L5_reasoning").expanduser()
PATTERNS_FILE = LAYER_DIR / "patterns.jsonl"

def _ensure_dir():
    LAYER_DIR.mkdir(parents=True, exist_ok=True)

def persist_pattern(
    antecedent: str,
    consequent: str,
    support: int,
    confidence: float,
    lift: float,
    source: str = "pattern_miner",
) -> None:
    """追加一个 Pattern 到 JSONL 文件（幂等：冷却期内不重复写入已存在的 Pattern）。"""
    _ensure_dir()
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "antecedent": antecedent,
        "consequent": consequent,
        "support": support,
        "confidence": round(confidence, 4),
        "lift": round(lift, 3),
        "source": source,
    }
    try:
        with open(PATTERNS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.debug("Pattern persisted: %s → %s (lift=%.1f)", antecedent, consequent, lift)
    except Exception as exc:
        logger.warning("Failed to persist pattern: %s", exc)
