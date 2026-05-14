"""Tests for kernel/idle_detector.py."""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from kernel.event_bus import EventBus
from kernel.idle_detector import IdleConfig, IdleDetector


@pytest.fixture
def fresh_bus():
    return EventBus()


@pytest.fixture
def temp_db():
    """Create a temp sqlite DB with a known message pattern."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            started_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            timestamp REAL NOT NULL,
            content TEXT
        )
    """)
    conn.commit()
    conn.close()

    yield db_path
    db_path.unlink(missing_ok=True)


def _insert_message(db_path: Path, role: str, age_s: float) -> None:
    ts = time.time() - age_s
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO sessions VALUES ('test-session', 'weixin', ?)", (ts,))
    conn.execute(
        "INSERT INTO messages VALUES (1, 'test-session', ?, ?, '')",
        (role, ts),
    )
    conn.commit()
    conn.close()


def _update_message_age(db_path: Path, age_s: float) -> None:
    ts = time.time() - age_s
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE messages SET timestamp = ? WHERE id = 1", (ts,))
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_idle_detector_fires_on_silence(fresh_bus, temp_db):
    """User silent > threshold → L0.circadian.idle fires."""
    _insert_message(temp_db, "user", age_s=300)  # 5 minutes ago

    config = IdleConfig(
        state_db_path=temp_db,
        idle_threshold_s=120.0,
        poll_interval_s=0.1,
    )
    detector = IdleDetector(config=config, bus=fresh_bus)

    events = []
    fresh_bus.subscribe("L0.circadian.idle", lambda e: events.append(e))

    await detector.attach()
    await asyncio.sleep(0.4)  # let it poll once
    await detector.detach()

    assert len(events) == 1, f"Expected 1 idle event, got {len(events)}"
    assert events[0].payload["idle_s"] >= 300


@pytest.mark.asyncio
async def test_idle_detector_no_fire_when_active(fresh_bus, temp_db):
    """User messaged recently → no idle event."""
    _insert_message(temp_db, "user", age_s=5)  # 5 seconds ago

    config = IdleConfig(
        state_db_path=temp_db,
        idle_threshold_s=120.0,
        poll_interval_s=0.1,
    )
    detector = IdleDetector(config=config, bus=fresh_bus)

    events = []
    fresh_bus.subscribe("L0.circadian.idle", lambda e: events.append(e))

    await detector.attach()
    await asyncio.sleep(0.4)
    await detector.detach()

    assert len(events) == 0, "Should not fire when user is active"


@pytest.mark.asyncio
async def test_idle_detector_stats(fresh_bus, temp_db):
    config = IdleConfig(state_db_path=temp_db, idle_threshold_s=60.0, poll_interval_s=10.0)
    detector = IdleDetector(config=config, bus=fresh_bus)
    stats = detector.stats()
    assert stats["idle_threshold_s"] == 60.0
    assert stats["poll_interval_s"] == 10.0
    assert stats["running"] is False
