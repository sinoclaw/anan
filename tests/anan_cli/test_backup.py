"""Tests for sinoclaw backup and import commands."""

import json
import os
import sqlite3
import zipfile
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sinoclaw_tree(root: Path) -> None:
    """Create a realistic ~/.sinoclaw directory structure for testing."""
    (root / "config.yaml").write_text("model:\n  provider: openrouter\n")
    (root / ".env").write_text("OPENROUTER_API_KEY=sk-test-123\n")
    (root / "memory_store.db").write_bytes(b"fake-sqlite")
    (root / "sinoclaw_state.db").write_bytes(b"fake-state")

    # Sessions
    (root / "sessions").mkdir(exist_ok=True)
    (root / "sessions" / "abc123.json").write_text("{}")

    # Skills
    (root / "skills").mkdir(exist_ok=True)
    (root / "skills" / "my-skill").mkdir()
    (root / "skills" / "my-skill" / "SKILL.md").write_text("# My Skill\n")

    # Skins
    (root / "skins").mkdir(exist_ok=True)
    (root / "skins" / "cyber.yaml").write_text("name: cyber\n")

    # Cron
    (root / "cron").mkdir(exist_ok=True)
    (root / "cron" / "jobs.json").write_text("[]")

    # Memories
    (root / "memories").mkdir(exist_ok=True)
    (root / "memories" / "notes.json").write_text("{}")

    # Profiles
    (root / "profiles").mkdir(exist_ok=True)
    (root / "profiles" / "coder").mkdir()
    (root / "profiles" / "coder" / "config.yaml").write_text("model:\n  provider: anthropic\n")
    (root / "profiles" / "coder" / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-123\n")

    # anan repo (should be EXCLUDED)
    (root / "anan").mkdir(exist_ok=True)
    (root / "anan" / "run_agent.py").write_text("# big file\n")
    (root / "anan" / ".git").mkdir()
    (root / "anan" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    # __pycache__ (should be EXCLUDED)
    (root / "plugins").mkdir(exist_ok=True)
    (root / "plugins" / "__pycache__").mkdir()
    (root / "plugins" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00")

    # PID files (should be EXCLUDED)
    (root / "gateway.pid").write_text("12345")

    # Logs (should be included)
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / "agent.log").write_text("log line\n")


# ---------------------------------------------------------------------------
# _should_exclude tests
# ---------------------------------------------------------------------------

class TestShouldExclude:
    def test_excludes_sinoclaw_agent(self):
        from anan_cli.backup import _should_exclude
        assert _should_exclude(Path("anan/run_agent.py"))
        assert _should_exclude(Path("anan/.git/HEAD"))

    def test_excludes_pycache(self):
        from anan_cli.backup import _should_exclude
        assert _should_exclude(Path("plugins/__pycache__/mod.cpython-312.pyc"))

    def test_excludes_pyc_files(self):
        from anan_cli.backup import _should_exclude
        assert _should_exclude(Path("some/module.pyc"))

    def test_excludes_pid_files(self):
        from anan_cli.backup import _should_exclude
        assert _should_exclude(Path("gateway.pid"))
        assert _should_exclude(Path("cron.pid"))

    def test_excludes_checkpoints(self):
        """checkpoints/ is session-local trajectory cache — hash-keyed,
        regenerated per-session, won't port to another machine anyway."""
        from anan_cli.backup import _should_exclude
        assert _should_exclude(Path("checkpoints/abc123/trajectory.json"))
        assert _should_exclude(Path("checkpoints/deadbeef/step_0001.json"))

    def test_excludes_backups_dir(self):
        """backups/ is excluded so pre-update backups don't nest exponentially."""
        from anan_cli.backup import _should_exclude
        assert _should_exclude(Path("backups/pre-update-2026-04-27-063400.zip"))

    def test_excludes_sqlite_sidecars(self):
        """SQLite WAL/SHM/journal sidecars must not ship alongside the
        safe-copied .db — pairing a fresh snapshot with stale sidecar state
        produces a torn restore."""
        from anan_cli.backup import _should_exclude
        assert _should_exclude(Path("state.db-wal"))
        assert _should_exclude(Path("state.db-shm"))
        assert _should_exclude(Path("state.db-journal"))
        assert _should_exclude(Path("memory_store.db-wal"))
        # The .db itself is still included (and safe-copied separately)
        assert not _should_exclude(Path("state.db"))

    def test_includes_config(self):
        from anan_cli.backup import _should_exclude
        assert not _should_exclude(Path("config.yaml"))

    def test_includes_env(self):
        from anan_cli.backup import _should_exclude
        assert not _should_exclude(Path(".env"))

    def test_includes_skills(self):
        from anan_cli.backup import _should_exclude
        assert not _should_exclude(Path("skills/my-skill/SKILL.md"))

    def test_includes_profiles(self):
        from anan_cli.backup import _should_exclude
        assert not _should_exclude(Path("profiles/coder/config.yaml"))

    def test_includes_sessions(self):
        from anan_cli.backup import _should_exclude
        assert not _should_exclude(Path("sessions/abc.json"))

    def test_includes_logs(self):
        from anan_cli.backup import _should_exclude
        assert not _should_exclude(Path("logs/agent.log"))


# ---------------------------------------------------------------------------
# Backup tests
# ---------------------------------------------------------------------------

class TestBackup:
    def test_creates_zip(self, tmp_path, monkeypatch):
        """Backup creates a valid zip containing expected files."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        _make_sinoclaw_tree(anan_home)

        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        # get_default_sinoclaw_root needs this
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out_zip = tmp_path / "backup.zip"
        args = Namespace(output=str(out_zip))

        from anan_cli.backup import run_backup
        run_backup(args)

        assert out_zip.exists()
        with zipfile.ZipFile(out_zip, "r") as zf:
            names = zf.namelist()
            # Config should be present
            assert "config.yaml" in names
            assert ".env" in names
            # Skills
            assert "skills/my-skill/SKILL.md" in names
            # Profiles
            assert "profiles/coder/config.yaml" in names
            assert "profiles/coder/.env" in names
            # Sessions
            assert "sessions/abc123.json" in names
            # Logs
            assert "logs/agent.log" in names
            # Skins
            assert "skins/cyber.yaml" in names

    def test_excludes_sinoclaw_agent(self, tmp_path, monkeypatch):
        """Backup does NOT include anan/ directory."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        _make_sinoclaw_tree(anan_home)

        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out_zip = tmp_path / "backup.zip"
        args = Namespace(output=str(out_zip))

        from anan_cli.backup import run_backup
        run_backup(args)

        with zipfile.ZipFile(out_zip, "r") as zf:
            names = zf.namelist()
            agent_files = [n for n in names if "anan" in n]
            assert agent_files == [], f"anan files leaked into backup: {agent_files}"

    def test_excludes_pycache(self, tmp_path, monkeypatch):
        """Backup does NOT include __pycache__ dirs."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        _make_sinoclaw_tree(anan_home)

        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out_zip = tmp_path / "backup.zip"
        args = Namespace(output=str(out_zip))

        from anan_cli.backup import run_backup
        run_backup(args)

        with zipfile.ZipFile(out_zip, "r") as zf:
            names = zf.namelist()
            pycache_files = [n for n in names if "__pycache__" in n]
            assert pycache_files == []

    def test_excludes_pid_files(self, tmp_path, monkeypatch):
        """Backup does NOT include PID files."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        _make_sinoclaw_tree(anan_home)

        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out_zip = tmp_path / "backup.zip"
        args = Namespace(output=str(out_zip))

        from anan_cli.backup import run_backup
        run_backup(args)

        with zipfile.ZipFile(out_zip, "r") as zf:
            names = zf.namelist()
            pid_files = [n for n in names if n.endswith(".pid")]
            assert pid_files == []

    def test_default_output_path(self, tmp_path, monkeypatch):
        """When no output path given, zip goes to ~/sinoclaw-backup-*.zip."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        (anan_home / "config.yaml").write_text("model: test\n")

        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        args = Namespace(output=None)

        from anan_cli.backup import run_backup
        run_backup(args)

        # Should exist in home dir
        zips = list(tmp_path.glob("anan-backup-*.zip"))
        assert len(zips) == 1


# ---------------------------------------------------------------------------
# _validate_backup_zip tests
# ---------------------------------------------------------------------------

class TestValidateBackupZip:
    def _make_zip(self, zip_path: Path, filenames: list[str]) -> None:
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name in filenames:
                zf.writestr(name, "dummy")

    def test_state_db_passes(self, tmp_path):
        """A zip containing state.db is accepted as a valid Sinoclaw backup."""
        from anan_cli.backup import _validate_backup_zip
        zip_path = tmp_path / "backup.zip"
        self._make_zip(zip_path, ["state.db", "sessions/abc.json"])
        with zipfile.ZipFile(zip_path, "r") as zf:
            ok, reason = _validate_backup_zip(zf)
        assert ok, reason

    def test_old_wrong_db_name_fails(self, tmp_path):
        """A zip with only sinoclaw_state.db (old wrong name) is rejected."""
        from anan_cli.backup import _validate_backup_zip
        zip_path = tmp_path / "old.zip"
        self._make_zip(zip_path, ["sinoclaw_state.db", "memory_store.db"])
        with zipfile.ZipFile(zip_path, "r") as zf:
            ok, reason = _validate_backup_zip(zf)
        assert not ok

    def test_config_yaml_passes(self, tmp_path):
        """A zip containing config.yaml is accepted (existing behaviour preserved)."""
        from anan_cli.backup import _validate_backup_zip
        zip_path = tmp_path / "backup.zip"
        self._make_zip(zip_path, ["config.yaml", "skills/x/SKILL.md"])
        with zipfile.ZipFile(zip_path, "r") as zf:
            ok, reason = _validate_backup_zip(zf)
        assert ok, reason


# ---------------------------------------------------------------------------
# Import tests
# ---------------------------------------------------------------------------

class TestImport:
    def _make_backup_zip(self, zip_path: Path, files: dict[str, str | bytes]) -> None:
        """Create a test zip with given files."""
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, content in files.items():
                if isinstance(content, bytes):
                    zf.writestr(name, content)
                else:
                    zf.writestr(name, content)

    def test_restores_files(self, tmp_path, monkeypatch):
        """Import extracts files into sinoclaw home."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "backup.zip"
        self._make_backup_zip(zip_path, {
            "config.yaml": "model:\n  provider: openrouter\n",
            ".env": "OPENROUTER_API_KEY=sk-test\n",
            "skills/my-skill/SKILL.md": "# My Skill\n",
            "profiles/coder/config.yaml": "model:\n  provider: anthropic\n",
        })

        args = Namespace(zipfile=str(zip_path), force=True)

        from anan_cli.backup import run_import
        run_import(args)

        assert (anan_home / "config.yaml").read_text() == "model:\n  provider: openrouter\n"
        assert (anan_home / ".env").read_text() == "OPENROUTER_API_KEY=sk-test\n"
        assert (anan_home / "skills" / "my-skill" / "SKILL.md").read_text() == "# My Skill\n"
        assert (anan_home / "profiles" / "coder" / "config.yaml").exists()

    def test_strips_sinoclaw_prefix(self, tmp_path, monkeypatch):
        """Import strips .hermes/ prefix if all entries share it."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "backup.zip"
        self._make_backup_zip(zip_path, {
            ".sinoclaw/config.yaml": "model: test\n",
            ".sinoclaw/skills/a/SKILL.md": "# A\n",
        })

        args = Namespace(zipfile=str(zip_path), force=True)

        from anan_cli.backup import run_import
        run_import(args)

        assert (anan_home / "config.yaml").read_text() == "model: test\n"
        assert (anan_home / "skills" / "a" / "SKILL.md").read_text() == "# A\n"

    def test_rejects_empty_zip(self, tmp_path, monkeypatch):
        """Import rejects an empty zip."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "empty.zip"
        with zipfile.ZipFile(zip_path, "w"):
            pass  # empty

        args = Namespace(zipfile=str(zip_path), force=True)

        from anan_cli.backup import run_import
        with pytest.raises(SystemExit):
            run_import(args)

    def test_rejects_non_sinoclaw_zip(self, tmp_path, monkeypatch):
        """Import rejects a zip that doesn't look like a sinoclaw backup."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "random.zip"
        self._make_backup_zip(zip_path, {
            "some/random/file.txt": "hello",
            "another/thing.json": "{}",
        })

        args = Namespace(zipfile=str(zip_path), force=True)

        from anan_cli.backup import run_import
        with pytest.raises(SystemExit):
            run_import(args)

    def test_blocks_path_traversal(self, tmp_path, monkeypatch):
        """Import blocks zip entries with path traversal."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "evil.zip"
        # Include a marker file so validation passes
        self._make_backup_zip(zip_path, {
            "config.yaml": "model: test\n",
            "../../etc/passwd": "root:x:0:0\n",
        })

        args = Namespace(zipfile=str(zip_path), force=True)

        from anan_cli.backup import run_import
        run_import(args)

        # config.yaml should be restored
        assert (anan_home / "config.yaml").exists()
        # traversal file should NOT exist outside sinoclaw home
        assert not (tmp_path / "etc" / "passwd").exists()

    def test_confirmation_prompt_abort(self, tmp_path, monkeypatch):
        """Import aborts when user says no to confirmation."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        # Pre-existing config triggers the confirmation
        (anan_home / "config.yaml").write_text("existing: true\n")
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "backup.zip"
        self._make_backup_zip(zip_path, {
            "config.yaml": "model: restored\n",
        })

        args = Namespace(zipfile=str(zip_path), force=False)

        from anan_cli.backup import run_import
        with patch("builtins.input", return_value="n"):
            run_import(args)

        # Original config should be unchanged
        assert (anan_home / "config.yaml").read_text() == "existing: true\n"

    def test_force_skips_confirmation(self, tmp_path, monkeypatch):
        """Import with --force skips confirmation and overwrites."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        (anan_home / "config.yaml").write_text("existing: true\n")
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "backup.zip"
        self._make_backup_zip(zip_path, {
            "config.yaml": "model: restored\n",
        })

        args = Namespace(zipfile=str(zip_path), force=True)

        from anan_cli.backup import run_import
        run_import(args)

        assert (anan_home / "config.yaml").read_text() == "model: restored\n"

    def test_missing_file_exits(self, tmp_path, monkeypatch):
        """Import exits with error for nonexistent file."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        monkeypatch.setenv("ANAN_HOME", str(anan_home))

        args = Namespace(zipfile=str(tmp_path / "nonexistent.zip"), force=True)

        from anan_cli.backup import run_import
        with pytest.raises(SystemExit):
            run_import(args)

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions only")
    def test_restores_secret_files_with_0600_perms(self, tmp_path, monkeypatch):
        """Secret files must end up at 0600 after restore (zipfile drops mode bits)."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "backup.zip"
        self._make_backup_zip(zip_path, {
            "config.yaml": "model: openrouter\n",
            ".env": "OPENROUTER_API_KEY=sk-secret\n",
            "auth.json": '{"providers": {"nous": "token"}}',
            "state.db": b"SQLite format 3\x00",
            "profiles/coder/.env": "ANTHROPIC_API_KEY=sk-ant-secret\n",
        })

        args = Namespace(zipfile=str(zip_path), force=True)

        from anan_cli.backup import run_import
        run_import(args)

        for rel in (".env", "auth.json", "state.db", "profiles/coder/.env"):
            mode = (anan_home / rel).stat().st_mode & 0o777
            assert mode == 0o600, f"{rel} restored with mode {oct(mode)}, expected 0o600"


# ---------------------------------------------------------------------------
# Round-trip test
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_backup_then_import(self, tmp_path, monkeypatch):
        """Full round-trip: backup -> import to a new location -> verify."""
        # Source
        src_home = tmp_path / "source" / ".sinoclaw"
        src_home.mkdir(parents=True)
        _make_sinoclaw_tree(src_home)

        monkeypatch.setenv("ANAN_HOME", str(src_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "source")

        # Backup
        out_zip = tmp_path / "roundtrip.zip"
        from anan_cli.backup import run_backup, run_import

        run_backup(Namespace(output=str(out_zip)))
        assert out_zip.exists()

        # Import into a different location
        dst_home = tmp_path / "dest" / ".sinoclaw"
        dst_home.mkdir(parents=True)
        monkeypatch.setenv("ANAN_HOME", str(dst_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "dest")

        run_import(Namespace(zipfile=str(out_zip), force=True))

        # Verify key files
        assert (dst_home / "config.yaml").read_text() == "model:\n  provider: openrouter\n"
        assert (dst_home / ".env").read_text() == "OPENROUTER_API_KEY=sk-test-123\n"
        assert (dst_home / "skills" / "my-skill" / "SKILL.md").exists()
        assert (dst_home / "profiles" / "coder" / "config.yaml").exists()
        assert (dst_home / "sessions" / "abc123.json").exists()
        assert (dst_home / "logs" / "agent.log").exists()

        # anan should NOT be present
        assert not (dst_home / "anan").exists()
        # __pycache__ should NOT be present
        assert not (dst_home / "plugins" / "__pycache__").exists()
        # PID files should NOT be present
        assert not (dst_home / "gateway.pid").exists()


# ---------------------------------------------------------------------------
# Validate / detect-prefix unit tests
# ---------------------------------------------------------------------------

class TestFormatSize:
    def test_bytes(self):
        from anan_cli.backup import _format_size
        assert _format_size(512) == "512 B"

    def test_kilobytes(self):
        from anan_cli.backup import _format_size
        assert "KB" in _format_size(2048)

    def test_megabytes(self):
        from anan_cli.backup import _format_size
        assert "MB" in _format_size(5 * 1024 * 1024)

    def test_gigabytes(self):
        from anan_cli.backup import _format_size
        assert "GB" in _format_size(3 * 1024 ** 3)

    def test_terabytes(self):
        from anan_cli.backup import _format_size
        assert "TB" in _format_size(2 * 1024 ** 4)


class TestValidation:
    def test_validate_with_config(self):
        """Zip with config.yaml passes validation."""
        import io
        from anan_cli.backup import _validate_backup_zip

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("config.yaml", "test")
        buf.seek(0)
        with zipfile.ZipFile(buf, "r") as zf:
            ok, reason = _validate_backup_zip(zf)
        assert ok

    def test_validate_with_env(self):
        """Zip with .env passes validation."""
        import io
        from anan_cli.backup import _validate_backup_zip

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(".env", "KEY=val")
        buf.seek(0)
        with zipfile.ZipFile(buf, "r") as zf:
            ok, reason = _validate_backup_zip(zf)
        assert ok

    def test_validate_rejects_random(self):
        """Zip without sinoclaw markers fails validation."""
        import io
        from anan_cli.backup import _validate_backup_zip

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("random/file.txt", "hello")
        buf.seek(0)
        with zipfile.ZipFile(buf, "r") as zf:
            ok, reason = _validate_backup_zip(zf)
        assert not ok

    def test_detect_prefix_hermes(self):
        """Detects .hermes/ prefix wrapping all entries."""
        import io
        from anan_cli.backup import _detect_prefix

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(".sinoclaw/config.yaml", "test")
            zf.writestr(".sinoclaw/skills/a/SKILL.md", "skill")
        buf.seek(0)
        with zipfile.ZipFile(buf, "r") as zf:
            assert _detect_prefix(zf) == ".sinoclaw/"

    def test_detect_prefix_none(self):
        """No prefix when entries are at root."""
        import io
        from anan_cli.backup import _detect_prefix

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("config.yaml", "test")
            zf.writestr("skills/a/SKILL.md", "skill")
        buf.seek(0)
        with zipfile.ZipFile(buf, "r") as zf:
            assert _detect_prefix(zf) == ""

    def test_detect_prefix_only_dirs(self):
        """Prefix detection returns empty for zip with only directory entries."""
        import io
        from anan_cli.backup import _detect_prefix

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            # Only directory entries (trailing slash)
            zf.writestr(".sinoclaw/", "")
            zf.writestr(".sinoclaw/skills/", "")
        buf.seek(0)
        with zipfile.ZipFile(buf, "r") as zf:
            assert _detect_prefix(zf) == ""


# ---------------------------------------------------------------------------
# Edge case tests for uncovered paths
# ---------------------------------------------------------------------------

class TestBackupEdgeCases:
    def test_nonexistent_anan_home(self, tmp_path, monkeypatch):
        """Backup exits when sinoclaw home doesn't exist."""
        fake_home = tmp_path / "nonexistent" / ".hermes"
        monkeypatch.setenv("ANAN_HOME", str(fake_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "nonexistent")

        args = Namespace(output=str(tmp_path / "out.zip"))

        from anan_cli.backup import run_backup
        with pytest.raises(SystemExit):
            run_backup(args)

    def test_output_is_directory(self, tmp_path, monkeypatch):
        """When output path is a directory, zip is created inside it."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        (anan_home / "config.yaml").write_text("model: test\n")

        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out_dir = tmp_path / "backups"
        out_dir.mkdir()

        args = Namespace(output=str(out_dir))

        from anan_cli.backup import run_backup
        run_backup(args)

        zips = list(out_dir.glob("anan-backup-*.zip"))
        assert len(zips) == 1

    def test_output_without_zip_suffix(self, tmp_path, monkeypatch):
        """Output path without .zip gets suffix appended."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        (anan_home / "config.yaml").write_text("model: test\n")

        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out_path = tmp_path / "mybackup.tar"
        args = Namespace(output=str(out_path))

        from anan_cli.backup import run_backup
        run_backup(args)

        # Should have .tar.zip suffix
        assert (tmp_path / "mybackup.tar.zip").exists()

    def test_empty_anan_home(self, tmp_path, monkeypatch):
        """Backup handles empty sinoclaw home (no files to back up)."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        # Only excluded dirs, no actual files
        (anan_home / "__pycache__").mkdir()
        (anan_home / "__pycache__" / "foo.pyc").write_bytes(b"\x00")

        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        args = Namespace(output=str(tmp_path / "out.zip"))

        from anan_cli.backup import run_backup
        run_backup(args)

        # No zip should be created
        assert not (tmp_path / "out.zip").exists()

    def test_permission_error_during_backup(self, tmp_path, monkeypatch):
        """Backup handles permission errors gracefully."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        (anan_home / "config.yaml").write_text("model: test\n")

        # Create an unreadable file
        bad_file = anan_home / "secret.db"
        bad_file.write_text("data")
        bad_file.chmod(0o000)

        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out_zip = tmp_path / "out.zip"
        args = Namespace(output=str(out_zip))

        from anan_cli.backup import run_backup
        try:
            run_backup(args)
        finally:
            # Restore permissions for cleanup
            bad_file.chmod(0o644)

        # Zip should still be created with the readable files
        assert out_zip.exists()

    def test_pre1980_timestamp_skipped(self, tmp_path, monkeypatch):
        """Backup skips files with pre-1980 timestamps (ZIP limitation)."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        (anan_home / "config.yaml").write_text("model: test\n")

        # Create a file with epoch timestamp (1970-01-01)
        old_file = anan_home / "ancient.txt"
        old_file.write_text("old data")
        os.utime(old_file, (0, 0))

        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out_zip = tmp_path / "out.zip"
        args = Namespace(output=str(out_zip))

        from anan_cli.backup import run_backup
        run_backup(args)

        # Zip should still be created with the valid files
        assert out_zip.exists()
        with zipfile.ZipFile(out_zip, "r") as zf:
            names = zf.namelist()
            assert "config.yaml" in names
            # The pre-1980 file should be skipped, not crash the backup
            assert "ancient.txt" not in names

    def test_skips_output_zip_inside_hermes(self, tmp_path, monkeypatch):
        """Backup skips its own output zip if it's inside sinoclaw root."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        (anan_home / "config.yaml").write_text("model: test\n")

        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Output inside sinoclaw home
        out_zip = anan_home / "backup.zip"
        args = Namespace(output=str(out_zip))

        from anan_cli.backup import run_backup
        run_backup(args)

        # The zip should exist but not contain itself
        assert out_zip.exists()
        with zipfile.ZipFile(out_zip, "r") as zf:
            assert "backup.zip" not in zf.namelist()


class TestImportEdgeCases:
    def _make_backup_zip(self, zip_path: Path, files: dict[str, str | bytes]) -> None:
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)

    def test_not_a_zip(self, tmp_path, monkeypatch):
        """Import rejects a non-zip file."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        monkeypatch.setenv("ANAN_HOME", str(anan_home))

        not_zip = tmp_path / "fake.zip"
        not_zip.write_text("this is not a zip")

        args = Namespace(zipfile=str(not_zip), force=True)

        from anan_cli.backup import run_import
        with pytest.raises(SystemExit):
            run_import(args)

    def test_eof_during_confirmation(self, tmp_path, monkeypatch):
        """Import handles EOFError during confirmation prompt."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        (anan_home / "config.yaml").write_text("existing\n")
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "backup.zip"
        self._make_backup_zip(zip_path, {"config.yaml": "new\n"})

        args = Namespace(zipfile=str(zip_path), force=False)

        from anan_cli.backup import run_import
        with patch("builtins.input", side_effect=EOFError):
            with pytest.raises(SystemExit):
                run_import(args)

    def test_keyboard_interrupt_during_confirmation(self, tmp_path, monkeypatch):
        """Import handles KeyboardInterrupt during confirmation prompt."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        (anan_home / ".env").write_text("KEY=val\n")
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "backup.zip"
        self._make_backup_zip(zip_path, {"config.yaml": "new\n"})

        args = Namespace(zipfile=str(zip_path), force=False)

        from anan_cli.backup import run_import
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit):
                run_import(args)

    def test_permission_error_during_import(self, tmp_path, monkeypatch):
        """Import handles permission errors during extraction."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Create a read-only directory so extraction fails
        locked_dir = anan_home / "locked"
        locked_dir.mkdir()
        locked_dir.chmod(0o555)

        zip_path = tmp_path / "backup.zip"
        self._make_backup_zip(zip_path, {
            "config.yaml": "model: test\n",
            "locked/secret.txt": "data",
        })

        args = Namespace(zipfile=str(zip_path), force=True)

        from anan_cli.backup import run_import
        try:
            run_import(args)
        finally:
            locked_dir.chmod(0o755)

        # config.yaml should still be restored despite the error
        assert (anan_home / "config.yaml").exists()

    def test_progress_with_many_files(self, tmp_path, monkeypatch):
        """Import shows progress with 500+ files."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "big.zip"
        files = {"config.yaml": "model: test\n"}
        for i in range(600):
            files[f"sessions/s{i:04d}.json"] = "{}"

        self._make_backup_zip(zip_path, files)

        args = Namespace(zipfile=str(zip_path), force=True)

        from anan_cli.backup import run_import
        run_import(args)

        assert (anan_home / "config.yaml").exists()
        assert (anan_home / "sessions" / "s0599.json").exists()


# ---------------------------------------------------------------------------
# Profile restoration tests
# ---------------------------------------------------------------------------

class TestProfileRestoration:
    def _make_backup_zip(self, zip_path: Path, files: dict[str, str | bytes]) -> None:
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)

    def test_import_creates_profile_wrappers(self, tmp_path, monkeypatch):
        """Import auto-creates wrapper scripts for restored profiles."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Mock the wrapper dir to be inside tmp_path
        wrapper_dir = tmp_path / ".local" / "bin"
        wrapper_dir.mkdir(parents=True)

        zip_path = tmp_path / "backup.zip"
        self._make_backup_zip(zip_path, {
            "config.yaml": "model:\n  provider: openrouter\n",
            "profiles/coder/config.yaml": "model:\n  provider: anthropic\n",
            "profiles/coder/.env": "ANTHROPIC_API_KEY=sk-test\n",
            "profiles/researcher/config.yaml": "model:\n  provider: deepseek\n",
        })

        args = Namespace(zipfile=str(zip_path), force=True)

        from anan_cli.backup import run_import
        run_import(args)

        # Profile directories should exist
        assert (anan_home / "profiles" / "coder" / "config.yaml").exists()
        assert (anan_home / "profiles" / "researcher" / "config.yaml").exists()

        # Wrapper scripts should be created
        assert (wrapper_dir / "coder").exists()
        assert (wrapper_dir / "researcher").exists()

        # Wrappers should contain the right content
        coder_wrapper = (wrapper_dir / "coder").read_text()
        assert "sinoclaw -p coder" in coder_wrapper

    def test_import_skips_profile_dirs_without_config(self, tmp_path, monkeypatch):
        """Import doesn't create wrappers for profile dirs without config."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        wrapper_dir = tmp_path / ".local" / "bin"
        wrapper_dir.mkdir(parents=True)

        zip_path = tmp_path / "backup.zip"
        self._make_backup_zip(zip_path, {
            "config.yaml": "model: test\n",
            "profiles/valid/config.yaml": "model: test\n",
            "profiles/empty/readme.txt": "nothing here\n",
        })

        args = Namespace(zipfile=str(zip_path), force=True)

        from anan_cli.backup import run_import
        run_import(args)

        # Only valid profile should get a wrapper
        assert (wrapper_dir / "valid").exists()
        assert not (wrapper_dir / "empty").exists()

    def test_import_without_profiles_module(self, tmp_path, monkeypatch):
        """Import gracefully handles missing profiles module (fresh install)."""
        anan_home = tmp_path / ".sinoclaw"
        anan_home.mkdir()
        monkeypatch.setenv("ANAN_HOME", str(anan_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "backup.zip"
        self._make_backup_zip(zip_path, {
            "config.yaml": "model: test\n",
            "profiles/coder/config.yaml": "model: test\n",
        })

        args = Namespace(zipfile=str(zip_path), force=True)

        # Simulate profiles module not being available
        import anan_cli.backup as backup_mod
        original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def fake_import(name, *a, **kw):
            if name == "anan_cli.profiles":
                raise ImportError("no profiles module")
            return original_import(name, *a, **kw)

        from anan_cli.backup import run_import
        with patch("builtins.__import__", side_effect=fake_import):
            run_import(args)

        # Files should still be restored even if wrappers can't be created
        assert (anan_home / "profiles" / "coder" / "config.yaml").exists()


# ---------------------------------------------------------------------------
# SQLite safe copy tests
# ---------------------------------------------------------------------------

class TestSafeCopyDb:
    def test_copies_valid_database(self, tmp_path):
        from anan_cli.backup import _safe_copy_db
        src = tmp_path / "test.db"
        dst = tmp_path / "copy.db"

        conn = sqlite3.connect(str(src))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
        conn.close()

        result = _safe_copy_db(src, dst)
        assert result is True

        conn = sqlite3.connect(str(dst))
        rows = conn.execute("SELECT x FROM t").fetchall()
        conn.close()
        assert rows == [(42,)]

    def test_copies_wal_mode_database(self, tmp_path):
        from anan_cli.backup import _safe_copy_db
        src = tmp_path / "wal.db"
        dst = tmp_path / "copy.db"

        conn = sqlite3.connect(str(src))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.execute("INSERT INTO t VALUES ('wal-test')")
        conn.commit()
        conn.close()

        result = _safe_copy_db(src, dst)
        assert result is True

        conn = sqlite3.connect(str(dst))
        rows = conn.execute("SELECT x FROM t").fetchall()
        conn.close()
        assert rows == [("wal-test",)]


# ---------------------------------------------------------------------------
# Quick state snapshot tests
# ---------------------------------------------------------------------------

class TestQuickSnapshot:
    @pytest.fixture
    def anan_home(self, tmp_path):
        """Create a fake ANAN_HOME with critical state files."""
        home = tmp_path / ".sinoclaw"
        home.mkdir()
        (home / "config.yaml").write_text("model:\n  provider: openrouter\n")
        (home / ".env").write_text("OPENROUTER_API_KEY=test-key-123\n")
        (home / "auth.json").write_text('{"providers": {}}\n')
        (home / "cron").mkdir()
        (home / "cron" / "jobs.json").write_text('{"jobs": []}\n')

        # Real SQLite database
        db_path = home / "state.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('s1', 'hello world')")
        conn.commit()
        conn.close()
        return home

    def test_creates_snapshot(self, anan_home):
        from anan_cli.backup import create_quick_snapshot
        snap_id = create_quick_snapshot(anan_home=anan_home)
        assert snap_id is not None
        snap_dir = anan_home / "state-snapshots" / snap_id
        assert snap_dir.is_dir()
        assert (snap_dir / "manifest.json").exists()

    def test_label_in_id(self, anan_home):
        from anan_cli.backup import create_quick_snapshot
        snap_id = create_quick_snapshot(label="before-upgrade", anan_home=anan_home)
        assert "before-upgrade" in snap_id

    def test_state_db_safely_copied(self, anan_home):
        from anan_cli.backup import create_quick_snapshot
        snap_id = create_quick_snapshot(anan_home=anan_home)
        db_copy = anan_home / "state-snapshots" / snap_id / "state.db"
        assert db_copy.exists()

        conn = sqlite3.connect(str(db_copy))
        rows = conn.execute("SELECT * FROM sessions").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0] == ("s1", "hello world")

    def test_copies_nested_files(self, anan_home):
        from anan_cli.backup import create_quick_snapshot
        snap_id = create_quick_snapshot(anan_home=anan_home)
        assert (anan_home / "state-snapshots" / snap_id / "cron" / "jobs.json").exists()

    def test_missing_files_skipped(self, anan_home):
        from anan_cli.backup import create_quick_snapshot
        snap_id = create_quick_snapshot(anan_home=anan_home)
        with open(anan_home / "state-snapshots" / snap_id / "manifest.json") as f:
            meta = json.load(f)
        # gateway_state.json etc. don't exist in fixture
        assert "gateway_state.json" not in meta["files"]

    def test_empty_home_returns_none(self, tmp_path):
        from anan_cli.backup import create_quick_snapshot
        empty = tmp_path / "empty"
        empty.mkdir()
        assert create_quick_snapshot(anan_home=empty) is None

    def test_list_snapshots(self, anan_home):
        from anan_cli.backup import create_quick_snapshot, list_quick_snapshots
        id1 = create_quick_snapshot(label="first", anan_home=anan_home)
        id2 = create_quick_snapshot(label="second", anan_home=anan_home)

        snaps = list_quick_snapshots(anan_home=anan_home)
        assert len(snaps) == 2
        assert snaps[0]["id"] == id2  # most recent first
        assert snaps[1]["id"] == id1

    def test_list_limit(self, anan_home):
        from anan_cli.backup import create_quick_snapshot, list_quick_snapshots
        for i in range(5):
            create_quick_snapshot(label=f"s{i}", anan_home=anan_home)
        snaps = list_quick_snapshots(limit=3, anan_home=anan_home)
        assert len(snaps) == 3

    def test_restore_config(self, anan_home):
        from anan_cli.backup import create_quick_snapshot, restore_quick_snapshot
        snap_id = create_quick_snapshot(anan_home=anan_home)

        (anan_home / "config.yaml").write_text("model:\n  provider: anthropic\n")
        assert "anthropic" in (anan_home / "config.yaml").read_text()

        result = restore_quick_snapshot(snap_id, anan_home=anan_home)
        assert result is True
        assert "openrouter" in (anan_home / "config.yaml").read_text()

    def test_restore_state_db(self, anan_home):
        from anan_cli.backup import create_quick_snapshot, restore_quick_snapshot
        snap_id = create_quick_snapshot(anan_home=anan_home)

        conn = sqlite3.connect(str(anan_home / "state.db"))
        conn.execute("INSERT INTO sessions VALUES ('s2', 'new')")
        conn.commit()
        conn.close()

        restore_quick_snapshot(snap_id, anan_home=anan_home)

        conn = sqlite3.connect(str(anan_home / "state.db"))
        rows = conn.execute("SELECT * FROM sessions").fetchall()
        conn.close()
        assert len(rows) == 1

    def test_restore_nonexistent(self, anan_home):
        from anan_cli.backup import restore_quick_snapshot
        assert restore_quick_snapshot("nonexistent", anan_home=anan_home) is False

    def test_auto_prune(self, anan_home):
        from anan_cli.backup import create_quick_snapshot, list_quick_snapshots, _QUICK_DEFAULT_KEEP
        for i in range(_QUICK_DEFAULT_KEEP + 5):
            create_quick_snapshot(label=f"snap-{i:03d}", anan_home=anan_home)
        snaps = list_quick_snapshots(limit=100, anan_home=anan_home)
        assert len(snaps) <= _QUICK_DEFAULT_KEEP

    def test_manual_prune(self, anan_home):
        from anan_cli.backup import create_quick_snapshot, prune_quick_snapshots, list_quick_snapshots
        for i in range(10):
            create_quick_snapshot(label=f"s{i}", anan_home=anan_home)
        deleted = prune_quick_snapshots(keep=3, anan_home=anan_home)
        assert deleted == 7
        assert len(list_quick_snapshots(anan_home=anan_home)) == 3

    def test_snapshot_includes_pairing_directories(self, anan_home):
        """Pairing JSONs live outside state.db — snapshot must capture them
        recursively (generic + per-platform) so approved-user lists survive
        disasters like #15733."""
        from anan_cli.backup import create_quick_snapshot

        # Generic pairing store (new location)
        (anan_home / "platforms" / "pairing").mkdir(parents=True)
        (anan_home / "platforms" / "pairing" / "telegram-approved.json").write_text(
            '{"12345": {"user_name": "alice"}}'
        )
        (anan_home / "platforms" / "pairing" / "discord-approved.json").write_text(
            '{"67890": {"user_name": "bob"}}'
        )
        # Legacy pairing store (old location)
        (anan_home / "pairing").mkdir()
        (anan_home / "pairing" / "matrix-approved.json").write_text(
            '{"@charlie:server": {"user_name": "charlie"}}'
        )
        # Feishu's separate JSON
        (anan_home / "feishu_comment_pairing.json").write_text(
            '{"doc_abc": {"allow_from": ["user_xyz"]}}'
        )

        snap_id = create_quick_snapshot(anan_home=anan_home)
        assert snap_id is not None

        snap_dir = anan_home / "state-snapshots" / snap_id
        assert (snap_dir / "platforms" / "pairing" / "telegram-approved.json").exists()
        assert (snap_dir / "platforms" / "pairing" / "discord-approved.json").exists()
        assert (snap_dir / "pairing" / "matrix-approved.json").exists()
        assert (snap_dir / "feishu_comment_pairing.json").exists()

        with open(snap_dir / "manifest.json") as f:
            meta = json.load(f)
        files = meta["files"]
        assert "platforms/pairing/telegram-approved.json" in files
        assert "platforms/pairing/discord-approved.json" in files
        assert "pairing/matrix-approved.json" in files
        assert "feishu_comment_pairing.json" in files

    def test_restore_recovers_pairing_data(self, anan_home):
        """After restore, deleted pairing files reappear with original content."""
        from anan_cli.backup import create_quick_snapshot, restore_quick_snapshot

        pairing_dir = anan_home / "platforms" / "pairing"
        pairing_dir.mkdir(parents=True)
        approved = pairing_dir / "telegram-approved.json"
        approved.write_text('{"12345": {"user_name": "alice"}}')
        feishu = anan_home / "feishu_comment_pairing.json"
        feishu.write_text('{"doc_abc": {"allow_from": ["user_xyz"]}}')

        snap_id = create_quick_snapshot(anan_home=anan_home)
        assert snap_id is not None

        # Simulate the disaster — user loses both pairing files.
        approved.unlink()
        feishu.unlink()
        assert not approved.exists()
        assert not feishu.exists()

        assert restore_quick_snapshot(snap_id, anan_home=anan_home) is True
        assert approved.exists()
        assert '"alice"' in approved.read_text()
        assert feishu.exists()
        assert '"user_xyz"' in feishu.read_text()

    def test_empty_pairing_dir_does_not_fail(self, anan_home):
        """An empty pairing directory should be silently skipped."""
        from anan_cli.backup import create_quick_snapshot

        (anan_home / "platforms" / "pairing").mkdir(parents=True)
        # Directory exists but contains no files.
        snap_id = create_quick_snapshot(anan_home=anan_home)
        # Other state still present → snapshot succeeds.
        assert snap_id is not None

# ---------------------------------------------------------------------------
# Pre-update backup (anan update safety net)
# ---------------------------------------------------------------------------

class TestPreUpdateBackup:
    """Tests for create_pre_update_backup — the auto-backup ``anan update``
    runs before touching anything."""

    @pytest.fixture
    def anan_home(self, tmp_path):
        root = tmp_path / ".sinoclaw"
        root.mkdir()
        _make_sinoclaw_tree(root)
        return root

    def test_creates_backup_under_backups_dir(self, anan_home):
        from anan_cli.backup import create_pre_update_backup
        out = create_pre_update_backup(anan_home=anan_home)
        assert out is not None
        assert out.exists()
        assert out.parent == anan_home / "backups"
        assert out.name.startswith("pre-update-")
        assert out.suffix == ".zip"

    def test_backup_contents_match_full_backup(self, anan_home):
        """Pre-update backup should include the same user data that
        ``sinoclaw backup`` would, and should exclude the same directories."""
        from anan_cli.backup import create_pre_update_backup
        out = create_pre_update_backup(anan_home=anan_home)
        assert out is not None
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        # User data present
        assert "config.yaml" in names
        assert ".env" in names
        assert "sessions/abc123.json" in names
        assert "skills/my-skill/SKILL.md" in names
        assert "profiles/coder/config.yaml" in names
        # anan repo excluded
        assert not any(n.startswith("anan/") for n in names)
        # __pycache__ excluded
        assert not any("__pycache__" in n for n in names)
        # pid files excluded
        assert "gateway.pid" not in names

    def test_does_not_recurse_into_prior_backups(self, anan_home):
        """The ``backups/`` directory must be excluded so that each backup
        doesn't grow exponentially by including all prior backups."""
        from anan_cli.backup import create_pre_update_backup
        # First backup
        out1 = create_pre_update_backup(anan_home=anan_home)
        assert out1 is not None
        # Second backup — must not include the first
        out2 = create_pre_update_backup(anan_home=anan_home)
        assert out2 is not None
        with zipfile.ZipFile(out2) as zf:
            names = zf.namelist()
        assert not any(n.startswith("backups/") for n in names), (
            f"Pre-update backup recursed into backups/ — leaked: "
            f"{[n for n in names if n.startswith('backups/')]}"
        )

    def test_rotation_keeps_only_n(self, anan_home):
        """After more than ``keep`` backups are created, older ones are
        pruned automatically."""
        import time as _t
        from anan_cli.backup import create_pre_update_backup

        created = []
        for _ in range(5):
            out = create_pre_update_backup(anan_home=anan_home, keep=3)
            created.append(out)
            _t.sleep(1.05)  # ensure distinct seconds in timestamp

        remaining = sorted(
            p.name for p in (anan_home / "backups").iterdir()
            if p.name.startswith("pre-update-")
        )
        assert len(remaining) == 3
        # Oldest two should have been pruned
        assert created[0].name not in remaining
        assert created[1].name not in remaining
        # Newest three should remain
        assert created[4].name in remaining

    def test_rotation_preserves_manual_files(self, anan_home):
        """Hand-dropped zips in ``backups/`` must not be touched by
        rotation — it only prunes files matching ``pre-update-*.zip``."""
        import time as _t
        from anan_cli.backup import create_pre_update_backup

        (anan_home / "backups").mkdir(exist_ok=True)
        manual = anan_home / "backups" / "my-manual.zip"
        manual.write_bytes(b"manual backup")

        for _ in range(5):
            create_pre_update_backup(anan_home=anan_home, keep=2)
            _t.sleep(1.05)

        assert manual.exists(), "Manual backup zip was incorrectly pruned"

    def test_returns_none_if_root_missing(self, tmp_path):
        from anan_cli.backup import create_pre_update_backup
        assert create_pre_update_backup(anan_home=tmp_path / "does-not-exist") is None

    def test_keep_zero_does_not_delete_freshly_created_backup(self, anan_home):
        """Regression: ``backup_keep: 0`` previously triggered ``backups[0:]``
        in the pruner — wiping the just-created zip and leaving the user
        with no recovery point.  The floor (keep>=1) preserves the new file
        regardless of misconfiguration; users who don't want backups should
        set ``pre_update_backup: false`` instead.
        """
        from anan_cli.backup import create_pre_update_backup
        out = create_pre_update_backup(anan_home=anan_home, keep=0)
        assert out is not None
        assert out.exists(), (
            "keep=0 silently deleted the freshly-created backup; floor "
            "should preserve the just-written file."
        )

    def test_keep_negative_does_not_delete_freshly_created_backup(self, anan_home):
        """Mirror coverage: any value <1 should be floored, not literally
        applied as a slice index."""
        from anan_cli.backup import create_pre_update_backup
        out = create_pre_update_backup(anan_home=anan_home, keep=-3)
        assert out is not None
        assert out.exists()

    def test_keep_zero_still_prunes_older_backups(self, anan_home):
        """The floor preserves the new backup but should NOT regress the
        rotation behaviour for older zips: a third call with keep=0 must
        still remove pre-existing backups beyond the (floored) limit of 1.
        """
        import time as _t
        from anan_cli.backup import create_pre_update_backup

        first = create_pre_update_backup(anan_home=anan_home, keep=5)
        _t.sleep(1.05)
        second = create_pre_update_backup(anan_home=anan_home, keep=5)
        _t.sleep(1.05)
        third = create_pre_update_backup(anan_home=anan_home, keep=0)

        remaining = {
            p.name for p in (anan_home / "backups").iterdir()
            if p.name.startswith("pre-update-")
        }
        assert third.name in remaining, "Floor must preserve the new backup"
        assert first.name not in remaining and second.name not in remaining, (
            f"keep=0 floor of 1 should still prune older backups; "
            f"remaining={remaining}"
        )


class TestRunPreUpdateBackup:
    """Tests for the ``_run_pre_update_backup`` wrapper in main.py —
    covers config gate, ``--no-backup`` flag, and user-facing output."""

    @pytest.fixture
    def anan_home(self, tmp_path, monkeypatch):
        root = tmp_path / ".sinoclaw"
        root.mkdir()
        _make_sinoclaw_tree(root)
        # Point ANAN_HOME at the temp dir so config + backup paths resolve here
        monkeypatch.setenv("ANAN_HOME", str(root))
        # Make Path.home() point at tmp_path for anything that uses it
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Bust caches for anan_cli.config + sinoclaw_constants so they pick up ANAN_HOME
        for mod in list(__import__("sys").modules.keys()):
            if mod.startswith("anan_cli.config") or mod == "sinoclaw_constants":
                del __import__("sys").modules[mod]
        return root

    def test_backup_flag_creates_backup(self, anan_home, capsys):
        """--backup forces the pre-update backup for one run even when config is off."""
        from anan_cli.main import _run_pre_update_backup
        _run_pre_update_backup(Namespace(no_backup=False, backup=True))
        out = capsys.readouterr().out
        assert "Creating pre-update backup" in out
        assert "Saved:" in out
        assert "Restore:" in out
        assert "anan import" in out
        assert "Disable:" in out
        # Actual backup was created
        backups = list((anan_home / "backups").glob("pre-update-*.zip"))
        assert len(backups) == 1

    def test_default_disabled_is_silent(self, anan_home, capsys):
        """With the default-off config and no --backup flag, the hook is silent
        and creates no backup.  This is the common case for every update."""
        from anan_cli.main import _run_pre_update_backup
        _run_pre_update_backup(Namespace(no_backup=False, backup=False))
        out = capsys.readouterr().out
        assert out == ""
        assert not (anan_home / "backups").exists() or not list(
            (anan_home / "backups").glob("pre-update-*.zip")
        )

    def test_no_backup_flag_skips(self, anan_home, capsys):
        from anan_cli.main import _run_pre_update_backup
        _run_pre_update_backup(Namespace(no_backup=True, backup=False))
        out = capsys.readouterr().out
        assert "skipped (--no-backup)" in out
        assert "Creating pre-update backup" not in out
        # No backup written
        assert not (anan_home / "backups").exists() or not list(
            (anan_home / "backups").glob("pre-update-*.zip")
        )

    def test_config_enabled_creates_backup(self, anan_home, capsys):
        """Users who explicitly set updates.pre_update_backup: true still get
        a backup on every update — this is the opt-in legacy behavior."""
        import yaml
        (anan_home / "config.yaml").write_text(yaml.safe_dump({
            "_config_version": 22,
            "updates": {"pre_update_backup": True},
        }))
        import sys as _sys
        for mod in list(_sys.modules.keys()):
            if mod.startswith("anan_cli.config"):
                del _sys.modules[mod]

        from anan_cli.main import _run_pre_update_backup
        _run_pre_update_backup(Namespace(no_backup=False, backup=False))
        out = capsys.readouterr().out
        assert "Creating pre-update backup" in out
        assert "Saved:" in out
        backups = list((anan_home / "backups").glob("pre-update-*.zip"))
        assert len(backups) == 1

    def test_config_disabled_is_silent(self, anan_home, capsys):
        """Explicit pre_update_backup: false behaves the same as the default —
        silent no-op, no message spam."""
        import yaml
        (anan_home / "config.yaml").write_text(yaml.safe_dump({
            "_config_version": 22,
            "updates": {"pre_update_backup": False},
        }))
        # Ensure config module re-reads
        import sys as _sys
        for mod in list(_sys.modules.keys()):
            if mod.startswith("anan_cli.config"):
                del _sys.modules[mod]

        from anan_cli.main import _run_pre_update_backup
        _run_pre_update_backup(Namespace(no_backup=False, backup=False))
        out = capsys.readouterr().out
        assert out == ""
        assert not list((anan_home / "backups").glob("pre-update-*.zip")) \
            if (anan_home / "backups").exists() else True

    def test_cli_flag_overrides_enabled_config(self, anan_home, capsys):
        """--no-backup wins even when config says pre_update_backup: true."""
        import yaml
        (anan_home / "config.yaml").write_text(yaml.safe_dump({
            "_config_version": 22,
            "updates": {"pre_update_backup": True},
        }))
        import sys as _sys
        for mod in list(_sys.modules.keys()):
            if mod.startswith("anan_cli.config"):
                del _sys.modules[mod]

        from anan_cli.main import _run_pre_update_backup
        _run_pre_update_backup(Namespace(no_backup=True, backup=False))
        out = capsys.readouterr().out
        assert "skipped (--no-backup)" in out


# ---------------------------------------------------------------------------
# Pre-migration backup (hermes claw migrate safety net)
# ---------------------------------------------------------------------------

class TestPreMigrationBackup:
    """Tests for create_pre_migration_backup — the auto-backup
    ``sinoclaw claw migrate`` runs before mutating ~/.anan/."""

    @pytest.fixture
    def anan_home(self, tmp_path):
        root = tmp_path / ".sinoclaw"
        root.mkdir()
        _make_sinoclaw_tree(root)
        return root

    def test_creates_backup_under_backups_dir(self, anan_home):
        from anan_cli.backup import create_pre_migration_backup
        out = create_pre_migration_backup(anan_home=anan_home)
        assert out is not None
        assert out.exists()
        # Shares the backups/ directory with pre-update backups so `hermes
        # import` and the update-backup listing both pick them up.
        assert out.parent == anan_home / "backups"
        assert out.name.startswith("pre-migration-")
        assert out.suffix == ".zip"

    def test_backup_uses_shared_exclusion_rules(self, anan_home):
        """Pre-migration backup reuses the same exclusion rules as
        ``sinoclaw backup`` / ``create_pre_update_backup`` — no drift."""
        from anan_cli.backup import create_pre_migration_backup
        out = create_pre_migration_backup(anan_home=anan_home)
        assert out is not None
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        # User data present
        assert "config.yaml" in names
        assert ".env" in names
        assert "skills/my-skill/SKILL.md" in names
        # Same exclusions as the shared helper
        assert not any(n.startswith("anan/") for n in names)
        assert not any("__pycache__" in n for n in names)
        assert "gateway.pid" not in names

    def test_restorable_with_sinoclaw_import(self, anan_home, tmp_path):
        """The zip produced by pre-migration backup must be a valid Sinoclaw
        backup — `sinoclaw import` should accept it."""
        from anan_cli.backup import create_pre_migration_backup, _validate_backup_zip
        out = create_pre_migration_backup(anan_home=anan_home)
        assert out is not None
        with zipfile.ZipFile(out) as zf:
            valid, _reason = _validate_backup_zip(zf)
        assert valid, "pre-migration zip failed _validate_backup_zip"

    def test_does_not_recurse_into_prior_backups(self, anan_home):
        from anan_cli.backup import create_pre_migration_backup
        out1 = create_pre_migration_backup(anan_home=anan_home)
        assert out1 is not None
        out2 = create_pre_migration_backup(anan_home=anan_home)
        assert out2 is not None
        with zipfile.ZipFile(out2) as zf:
            names = zf.namelist()
        assert not any(n.startswith("backups/") for n in names)

    def test_rotation_keeps_only_n(self, anan_home):
        import time as _t
        from anan_cli.backup import create_pre_migration_backup

        created = []
        for _ in range(7):
            out = create_pre_migration_backup(anan_home=anan_home, keep=3)
            if out is not None:
                created.append(out)
            _t.sleep(1.05)  # timestamp resolution

        remaining = sorted((anan_home / "backups").glob("pre-migration-*.zip"))
        assert len(remaining) <= 3, f"expected <=3 backups retained, got {len(remaining)}"

    def test_missing_anan_home_returns_none(self, tmp_path):
        """Fresh install with no ~/.sinoclaw yet — nothing to back up."""
        from anan_cli.backup import create_pre_migration_backup
        missing = tmp_path / "does-not-exist"
        out = create_pre_migration_backup(anan_home=missing)
        assert out is None

    def test_does_not_touch_pre_update_backups(self, anan_home):
        """Pre-migration rotation must only prune pre-migration-*.zip files,
        leaving pre-update-*.zip backups untouched."""
        from anan_cli.backup import create_pre_update_backup, create_pre_migration_backup
        update_backup = create_pre_update_backup(anan_home=anan_home, keep=5)
        assert update_backup is not None and update_backup.exists()
        # Spin up a lot of migration backups with keep=1
        import time as _t
        for _ in range(3):
            out = create_pre_migration_backup(anan_home=anan_home, keep=1)
            assert out is not None
            _t.sleep(1.05)
        # Update backup must still be there
        assert update_backup.exists(), "pre-migration rotation wrongly pruned the pre-update backup"
