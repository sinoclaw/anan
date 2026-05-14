"""
adapters/sinoclaw_insight_sync.py 测试套件
==========================================

覆盖:
  - load_latest_wisdom() — 文件不存在/存在/格式错误
  - write_to_sinoclaw_session() — mock SQLite DB
  - main() — 端到端流程
"""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

# 直接 import 测试函数
import adapters.sinoclaw_insight_sync as sync_mod


class TestLoadLatestWisdom:
    def test_file_not_exists_returns_none(self, tmp_path):
        """WISDOM_FILE 不存在时返回 None。"""
        fake_wisdom_file = tmp_path / "nonexistent_wisdom.json"
        with tempfile.NamedTemporaryFile(dir=tmp_path, suffix=".json") as f:
            pass
        # 直接替换模块里的 WISDOM_FILE
        original = sync_mod.WISDOM_FILE
        sync_mod.WISDOM_FILE = fake_wisdom_file
        try:
            result = sync_mod.load_latest_wisdom()
            assert result is None
        finally:
            sync_mod.WISDOM_FILE = original

    def test_file_exists_returns_parsed_json(self, tmp_path):
        """WISDOM_FILE 存在且合法时返回解析后的 dict。"""
        wisdom_file = tmp_path / "wisdom_latest.json"
        data = {"wisdom_facts": ["fact1", "fact2"], "prediction_stats": {"accuracy": 0.8}}
        wisdom_file.write_text(json.dumps(data))

        original = sync_mod.WISDOM_FILE
        sync_mod.WISDOM_FILE = wisdom_file
        try:
            result = sync_mod.load_latest_wisdom()
            assert result == data
        finally:
            sync_mod.WISDOM_FILE = original

    def test_malformed_json_returns_none(self, tmp_path):
        """WISDOM_FILE 存在但内容非法时返回 None。"""
        wisdom_file = tmp_path / "wisdom_latest.json"
        wisdom_file.write_text("not valid json{{")

        original = sync_mod.WISDOM_FILE
        sync_mod.WISDOM_FILE = wisdom_file
        try:
            result = sync_mod.load_latest_wisdom()
            assert result is None
        finally:
            sync_mod.WISDOM_FILE = original


class TestWriteToSinoclawSession:
    def test_no_sessions_returns_none(self, tmp_path):
        """sessions 表为空时返回 None。"""
        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, ended_at REAL)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
        conn.commit()
        conn.close()

        original = sync_mod.SINOCLAW_DB
        sync_mod.SINOCLAW_DB = db_path
        try:
            result = sync_mod.write_to_sinoclaw_session({"wisdom_facts": [], "prediction_stats": {}, "causal_links": []})
            assert result is None
        finally:
            sync_mod.SINOCLAW_DB = original

    def test_inserts_system_message_into_session(self, tmp_path):
        """向 sinoclaw sessions 表写入 system 消息，内容包含 anan 洞察。"""
        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, ended_at REAL)")
        conn.execute("INSERT INTO sessions (id, started_at) VALUES ('sess-1', 1234567890)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
        conn.commit()
        conn.close()

        wisdom = {
            "wisdom_facts": ["我发现 X 导致 Y"],
            "prediction_stats": {"accuracy": 0.75, "confirmed": 3, "failed": 1},
            "causal_links": ["X → Y (lift=2.0)"],
        }

        original = sync_mod.SINOCLAW_DB
        sync_mod.SINOCLAW_DB = db_path
        try:
            result = sync_mod.write_to_sinoclaw_session(wisdom)
        finally:
            sync_mod.SINOCLAW_DB = original

        assert result == "sess-1"
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id='sess-1' AND role='system'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        content = rows[0][1]
        assert "anan" in content
        assert "X" in content

    def test_nonexistent_db_returns_none(self, tmp_path):
        """SINOCLAW_DB 不存在时返回 None，不抛异常。"""
        nonexistent = tmp_path / "nonexistent" / "state.db"

        original = sync_mod.SINOCLAW_DB
        sync_mod.SINOCLAW_DB = nonexistent
        try:
            result = sync_mod.write_to_sinoclaw_session({"wisdom_facts": [], "prediction_stats": {}, "causal_links": []})
            assert result is None
        finally:
            sync_mod.SINOCLAW_DB = original
