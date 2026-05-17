"""
kernel/persistent_session.py 测试套件
=====================================

覆盖:
  - SessionConfig 默认值
  - PersistentSession._load() — 空目录/有文件/格式错误
  - PersistentSession._save() — JSONL 写入
  - _expand_path — ~ 展开
  - 跨实例重建（模拟进程重启后恢复）
"""

import json
import os
import pytest
import tempfile
from kernel.event_bus import EventBus
from kernel.persistent_session import PersistentSession, SessionConfig


class TestExpandPath:
    def test_expand_tilde(self):
        from kernel.persistent_session import _expand_path
        result = _expand_path("~/.anan/test")
        assert result.startswith("/root")
        assert "~" not in result


class TestSessionConfig:
    def test_default_storage_dir(self):
        cfg = SessionConfig()
        assert cfg.storage_dir == "~/.anan/sessions"


class TestPersistentSessionLoad:
    def test_load_empty_dir_creates_no_memory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = SessionConfig(storage_dir=tmpdir)
            session = PersistentSession(config=cfg)
            # No file exists yet — memory should stay empty
            assert session._short_term_memory == []
            assert session._session_n == 0

    def test_load_with_conversation_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Pre-write a JSONL conversation file
            conv_file = os.path.join(tmpdir, "conversation.jsonl")
            with open(conv_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({"role": "user", "content": "爸爸在吗？"}) + "\n")
                f.write(json.dumps({"role": "assistant", "content": "在的！"}) + "\n")
                f.write(json.dumps({"role": "user", "content": "今天做什么？"}) + "\n")
                f.write(json.dumps({"role": "assistant", "content": "写代码。"}) + "\n")

            cfg = SessionConfig(storage_dir=tmpdir)
            session = PersistentSession(config=cfg)

            assert len(session._short_term_memory) == 4
            assert session._short_term_memory[0] == "user: 爸爸在吗？"
            assert session._short_term_memory[1] == "assistant: 在的！"
            assert session._session_n == 2

    def test_load_malformed_jsonl_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conv_file = os.path.join(tmpdir, "conversation.jsonl")
            with open(conv_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({"role": "user", "content": "Hello"}) + "\n")
                f.write("not valid json\n")
                f.write(json.dumps({"role": "assistant", "content": "Hi"}) + "\n")

            cfg = SessionConfig(storage_dir=tmpdir)
            session = PersistentSession(config=cfg)

            assert session._short_term_memory[0] == "user: Hello"
            assert session._short_term_memory[1] == "assistant: Hi"
            assert len(session._short_term_memory) == 2


class TestPersistentSessionSave:
    def test_save_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = SessionConfig(storage_dir=tmpdir, max_iterations=1)
            # Mock agent so we don't need real AI
            session = PersistentSession(config=cfg)
            session._agent = _MockChatAgent()
            session._running = True

            # Manually add entries to memory and save
            session._short_term_memory = ["user: 你好", "assistant: 你好呀"]
            session._session_n = 1
            session._save()

            conv_file = os.path.join(tmpdir, "conversation.jsonl")
            assert os.path.exists(conv_file)

            with open(conv_file, encoding="utf-8") as f:
                lines = [json.loads(l) for l in f if l.strip()]
            assert len(lines) == 2
            assert lines[0] == {"role": "user", "content": "你好"}
            assert lines[1] == {"role": "assistant", "content": "你好呀"}

    def test_save_none_dir_does_nothing(self):
        cfg = SessionConfig(storage_dir=None)
        session = PersistentSession(config=cfg)
        session._save()  # Should not raise


class TestPersistentSessionCrossRestart:
    def test_reload_restores_memory_after_mock_chat(self):
        """
        模拟场景：进程A chat了2次，写入JSONL；进程B重启，从JSONL恢复。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            # Process A: chat once
            cfg = SessionConfig(storage_dir=tmpdir, max_iterations=1)
            session_a = PersistentSession(config=cfg)
            session_a._agent = _MockChatAgent()
            session_a._running = True
            session_a._short_term_memory = ["user: 你好", "assistant: 你好"]
            session_a._session_n = 1
            session_a._save()

            # Process B: restart and reload
            cfg2 = SessionConfig(storage_dir=tmpdir)
            session_b = PersistentSession(config=cfg2)

            assert len(session_b._short_term_memory) == 2
            assert session_b._short_term_memory[0] == "user: 你好"
            assert session_b._session_n == 1


class _MockChatAgent:
    def chat(self, message: str) -> str:
        return f"mock response to: {message}"
