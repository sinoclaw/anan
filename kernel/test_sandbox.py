"""Tests for kernel/sandbox.py."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kernel.event_bus import EventBus
from kernel.sandbox import SafetyLevel, Sandbox, SandboxConfig


@pytest.fixture
def fresh_bus():
    return EventBus()


@pytest.fixture
def temp_repo(tmp_path):
    """Create a temp git repo simulating the anan repo."""
    (tmp_path / "kernel").mkdir()
    (tmp_path / "layers").mkdir()
    (tmp_path / "SOUL.md").write_text("Original soul.")

    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path, capture_output=True, env={**__import__("os").environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test.com", "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test.com"}
    )
    return tmp_path


@pytest.mark.asyncio
async def test_sandbox_safety_levels():
    """Files map to correct safety levels."""
    s = Sandbox()
    assert s.get_safety_level("SOUL.md") == SafetyLevel.CRITICAL
    assert s.get_safety_level("USER.md") == SafetyLevel.CRITICAL
    assert s.get_safety_level("skills/memory.md") == SafetyLevel.HIGH
    assert s.get_safety_level("layers/L5/md") == SafetyLevel.HIGH
    assert s.get_safety_level("kernel/event_bus.py") == SafetyLevel.NORMAL


@pytest.mark.asyncio
async def test_sandbox_propose_normal_auto_applies(fresh_bus, temp_repo):
    """NORMAL file changes auto-apply when dry-run passes."""
    config = SandboxConfig(
        repo_root=temp_repo,
        require_approval=False,
        auto_apply_normal=True,
        test_command="",  # skip test (no kernel/layers tests to run)
    )
    s = Sandbox(config=config, bus=fresh_bus)
    await s.attach()

    record = await s.propose_change(
        file="kernel/test_file.py",
        new_content="# new content",
        reason="test",
    )

    assert record.applied is True
    assert (temp_repo / "kernel" / "test_file.py").read_text() == "# new content"
    await s.detach()


@pytest.mark.asyncio
async def test_sandbox_propose_critical_pending_approval(fresh_bus, temp_repo):
    """CRITICAL file changes require approval."""
    config = SandboxConfig(
        repo_root=temp_repo,
        require_approval=True,
        test_command="",
    )
    s = Sandbox(config=config, bus=fresh_bus)
    await s.attach()

    record = await s.propose_change(
        file="SOUL.md",
        new_content="New soul content",
        reason="test update",
    )

    assert record.applied is False
    assert record.approved is False
    assert (temp_repo / "SOUL.md").read_text() == "Original soul."  # unchanged
    await s.detach()


@pytest.mark.asyncio
async def test_sandbox_revert_on_dry_run_fail(fresh_bus, temp_repo):
    """Dry-run failure triggers revert."""
    config = SandboxConfig(
        repo_root=temp_repo,
        require_approval=False,
        auto_apply_normal=True,
        test_command="exit 1",  # always fail
    )
    s = Sandbox(config=config, bus=fresh_bus)
    await s.attach()

    record = await s.propose_change(
        file="kernel/bad.py",
        new_content="bad code",
        reason="test",
    )

    assert record.applied is False
    assert record.reverted is True
    assert (temp_repo / "kernel" / "bad.py").exists() is False  # not created
    await s.detach()


@pytest.mark.asyncio
async def test_sandbox_stats(fresh_bus, temp_repo):
    config = SandboxConfig(repo_root=temp_repo, require_approval=False)
    s = Sandbox(config=config, bus=fresh_bus)
    stats = s.stats()
    assert stats["applied"] == 0
    assert stats["reverted"] == 0
    assert stats["pending_approval"] == 0
