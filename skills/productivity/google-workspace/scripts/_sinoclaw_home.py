"""Resolve ANAN_HOME for standalone skill scripts.

Skill scripts may run outside the Sinoclaw process (e.g. system Python,
nix env, CI) where ``sinoclaw_constants`` is not importable.  This module
provides the same ``get_anan_home()`` and ``display_anan_home()``
contracts as ``sinoclaw_constants`` without requiring it on ``sys.path``.

When ``sinoclaw_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``sinoclaw_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``ANAN_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from anan_constants import display_anan_home as display_anan_home
    from anan_constants import get_anan_home as get_anan_home
except (ModuleNotFoundError, ImportError):

    def get_anan_home() -> Path:
        """Return the Sinoclaw home directory (default: ~/.sinoclaw).

        Mirrors ``sinoclaw_constants.get_anan_home()``."""
        val = os.environ.get("ANAN_HOME", "").strip()
        return Path(val) if val else Path.home() / ".anan"

    def display_anan_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``sinoclaw_constants.display_anan_home()``."""
        home = get_anan_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
