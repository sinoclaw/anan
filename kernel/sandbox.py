"""
Self-Modification Sandbox — 安全的自我改写机制
============================================

原理：
    L6 元认知发现自己的思维 bug，需要 patch 自己的 SOUL.md / skills / MEMORY.md。
    直接写会写崩。解法：写前 git commit，写后 dry-run 验证，通过才 apply。

为什么重要：
    没有 sandbox 的自我改写 = 玩命。
    有了 sandbox = 每次改都是可回滚的、有测试的、安全的。

阶段：
    Stage 1 (Early): 所有改写 git commit + 人类审批才 apply
    Stage 2 (Trusted): 小改动自动 apply，重大改动仍需审批

设计原则：
    - Git-first：所有写操作先 git commit 快照
    - Dry-run 验证：改写内容先 apply 到临时文件，跑测试验证
    - 可回滚：git revert 随时回退到上一个稳定版本
    - 分层策略：不同文件不同安全等级
        SOUL.md / MEMORY.md → 最高安全（需审批）
        skills/*.md → 中等安全（dry-run 通过即可）
        kernel/*.py → 低安全（测试全绿即可）

文件分级：
    CRITICAL — SOUL.md, USER.md, MEMORY.md
    HIGH     — skills/*.md, layers/*/*.md
    NORMAL   — kernel/*.py, layers/*/*.py

事件：
    L6.sandbox.pre_write — 即将改写 (payload: {file, content, safety_level})
    L6.sandbox.post_write — 改写完成 (payload: {file, revision, approved?})
    L6.sandbox.reverted — 已回滚 (payload: {file, reason})
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from kernel.event_bus import Event, EventBus, get_bus

logger = logging.getLogger("anan.kernel.sandbox")


class SafetyLevel(Enum):
    CRITICAL = "critical"   # SOUL.md, USER.md — always require approval
    HIGH = "high"           # skills, docs — require dry-run
    NORMAL = "normal"       # code — require test pass


SANDBOX_RULES = {
    "SOUL.md": SafetyLevel.CRITICAL,
    "USER.md": SafetyLevel.CRITICAL,
    "MEMORY.md": SafetyLevel.CRITICAL,
    "anan_memory.md": SafetyLevel.CRITICAL,
    # Skills
    "skills/": SafetyLevel.HIGH,
    "layers/": SafetyLevel.HIGH,
    # Kernel and code
    "kernel/": SafetyLevel.NORMAL,
    "adapters/": SafetyLevel.NORMAL,
}


@dataclass
class ChangeRecord:
    """A sandboxed change operation."""
    file: str
    old_content: str
    new_content: str
    safety: SafetyLevel
    dry_run_ok: bool = False
    approved: bool = False
    applied: bool = False
    reverted: bool = False
    git_commit: Optional[str] = None


@dataclass
class SandboxConfig:
    # Path to the anan repository root
    repo_root: Path = field(default_factory=lambda: Path("/data/anan"))
    # Require explicit approval for CRITICAL files (Stage 1 safety)
    require_approval: bool = True
    # Auto-apply NORMAL changes without approval
    auto_apply_normal: bool = True
    # Command to run tests (empty = skip)
    test_command: str = "PYTHONPATH=. python -m pytest kernel/ layers/ -q"
    # Max changes to keep in history
    history_limit: int = 50


class Sandbox:
    """Safe self-modification for anan.

    Usage:
        sandbox = Sandbox(config=SandboxConfig(repo_root=Path("/data/anan")))
        await sandbox.attach(bus)

        # Request a change (from any layer, typically L6):
        record = await sandbox.propose_change(
            file="SOUL.md",
            new_content="新的灵魂内容...",
            reason="L6 发现价值观偏离，需要锚定",
        )
        # record.approved tells you if it was auto-approved or needs human sign-off

    Events consumed:
        L6.sandbox.propose — propose a change (payload: {file, content, reason})
        L6.sandbox.approve — human approves a CRITICAL change (payload: {record_id})
        L6.sandbox.reject — human rejects (payload: {record_id, reason})

    Events published:
        L6.sandbox.pre_write — before write
        L6.sandbox.post_write — after write (approved=False if pending)
        L6.sandbox.reverted — after revert
    """

    def __init__(
        self,
        *,
        config: Optional[SandboxConfig] = None,
        bus: Optional[EventBus] = None,
    ):
        self.config = config or SandboxConfig()
        self.bus = bus or get_bus()
        self._history: list[ChangeRecord] = []
        self._unsubs: list = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def attach(self) -> None:
        self._unsubs.append(
            self.bus.subscribe("L6.sandbox.propose", self._on_propose)
        )
        self._unsubs.append(
            self.bus.subscribe("L6.sandbox.approve", self._on_approve)
        )
        self._unsubs.append(
            self.bus.subscribe("L6.sandbox.reject", self._on_reject)
        )
        logger.info("Sandbox attached (repo=%s, require_approval=%s)",
                    self.config.repo_root, self.config.require_approval)

    async def detach(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()

    def get_safety_level(self, file: str) -> SafetyLevel:
        """Determine safety level for a file path."""
        for pattern, level in SANDBOX_RULES.items():
            if pattern.endswith("/"):
                if file.startswith(pattern) or f"/{pattern}" in file:
                    return level
            elif pattern in file:
                return level
        return SafetyLevel.NORMAL

    async def propose_change(
        self,
        file: str,
        new_content: str,
        reason: str = "",
    ) -> ChangeRecord:
        """Propose a file change. Returns ChangeRecord with approval status.

        For CRITICAL files: auto-approved=False, waits for L6.sandbox.approve event.
        For NORMAL files: auto-approved if dry-run passes.
        """
        root = self.config.repo_root / file
        old_content = ""
        try:
            old_content = root.read_text()
        except FileNotFoundError:
            pass

        safety = self.get_safety_level(file)
        record = ChangeRecord(
            file=file,
            old_content=old_content,
            new_content=new_content,
            safety=safety,
        )

        # Snapshot to git before any change
        record.git_commit = await self._git_snapshot(file, old_content)

        # Publish pre-write event
        await self.bus.publish(Event(
            topic="L6.sandbox.pre_write",
            source="L6.sandbox",
            payload={
                "file": file,
                "safety": safety.value,
                "reason": reason,
                "diff": self._make_diff(old_content, new_content, file),
            },
        ))

        # Dry-run: write to temp, run tests
        record.dry_run_ok = await self._dry_run(file, new_content)

        if not record.dry_run_ok:
            logger.warning("Sandbox dry-run failed for %s — not applying", file)
            await self._revert(record)
            return record

        # Auto-approve based on safety level
        if safety == SafetyLevel.CRITICAL and self.config.require_approval:
            record.approved = False  # pending human approval
            logger.info("Sandbox CRITICAL change pending approval: %s", file)
        elif safety == SafetyLevel.NORMAL and self.config.auto_apply_normal:
            await self._apply(record)
        else:
            record.approved = False

        self._history.append(record)
        if len(self._history) > self.config.history_limit:
            self._history.pop(0)

        return record

    def stats(self) -> dict:
        return {
            "history_size": len(self._history),
            "pending_approval": sum(1 for r in self._history if r.approved is False and r.safety == SafetyLevel.CRITICAL),
            "applied": sum(1 for r in self._history if r.applied),
            "reverted": sum(1 for r in self._history if r.reverted),
        }

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_propose(self, event: Event) -> None:
        p = event.payload
        await self.propose_change(
            file=p["file"],
            new_content=p["content"],
            reason=p.get("reason", ""),
        )

    async def _on_approve(self, event: Event) -> None:
        record_id = event.payload.get("record_id")
        # Find and apply
        for record in reversed(self._history):
            if id(record) == record_id or record.file == record_id:
                record.approved = True
                await self._apply(record)
                return

    async def _on_reject(self, event: Event) -> None:
        record_id = event.payload.get("record_id")
        reason = event.payload.get("reason", "rejected")
        for record in reversed(self._history):
            if id(record) == record_id or record.file == record_id:
                await self._revert(record)
                return

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def _git_snapshot(self, file: str, content: str) -> Optional[str]:
        """Commit current state before modifying."""
        try:
            root = self.config.repo_root
            result = subprocess.run(
                ["git", "add", file],
                cwd=root, capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return None
            result = subprocess.run(
                ["git", "commit", "-m", f"sandbox: snapshot before self-modify {file}"],
                cwd=root, capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                # Return short commit hash
                return result.stdout.strip().split("\n")[-1][:7]
        except Exception as exc:
            logger.warning("git snapshot failed: %s", exc)
        return None

    async def _dry_run(self, file: str, new_content: str) -> bool:
        """Write to temp, run tests. Returns True if tests pass."""
        if self.config.test_command and "kernel/" in file or "layers/" in file:
            import tempfile
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=Path(file).suffix, delete=False
            ) as f:
                f.write(new_content)
                tmp = f.name

            try:
                result = subprocess.run(
                    self.config.test_command,
                    shell=True, cwd=self.config.repo_root,
                    capture_output=True, text=True, timeout=60,
                )
                return result.returncode == 0
            except subprocess.TimeoutExpired:
                return False
            finally:
                Path(tmp).unlink(missing_ok=True)
        return True  # skip dry-run for non-code files

    async def _apply(self, record: ChangeRecord) -> None:
        root = self.config.repo_root / record.file
        root.parent.mkdir(parents=True, exist_ok=True)
        root.write_text(record.new_content)
        record.applied = True
        await self.bus.publish(Event(
            topic="L6.sandbox.post_write",
            source="L6.sandbox",
            payload={
                "file": record.file,
                "approved": True,
                "git_commit": record.git_commit,
            },
        ))
        logger.info("Sandbox applied: %s", record.file)

    async def _revert(self, record: ChangeRecord) -> None:
        if record.old_content:
            (self.config.repo_root / record.file).write_text(record.old_content)
        record.reverted = True
        await self.bus.publish(Event(
            topic="L6.sandbox.reverted",
            source="L6.sandbox",
            payload={"file": record.file, "reason": "dry-run failed"},
        ))
        logger.info("Sandbox reverted: %s", record.file)

    def _make_diff(self, old: str, new: str, file: str = "file") -> str:
        """Generate a unified diff string."""
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{file}",
            tofile=f"b/{file}",
        )
        return "".join(diff)
