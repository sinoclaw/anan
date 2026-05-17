"""Shared helpers for direct xAI HTTP integrations."""

from __future__ import annotations


def anan_xai_user_agent() -> str:
    """Return a stable Anan-specific User-Agent for xAI HTTP calls."""
    try:
        from anan_cli import __version__
    except Exception:
        __version__ = "unknown"
    return f"anan-agent/{__version__}"
