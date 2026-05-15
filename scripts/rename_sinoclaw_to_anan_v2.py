#!/usr/bin/env python3
"""
Second pass: fix remaining anan residuals after first bulk pass.
"""
from pathlib import Path
import re

ROOT = Path("/data/anan")

# Patterns that should NOT be renamed (Nous model names, npm packages, etc.)
PROTECTED = [
    re.compile(r'anan-[345](?:\.\d+)?(?:[-_]|$)', re.IGNORECASE),
    re.compile(r'anan-[a-z]+-[a-z]+(?:\.\d+)?'),  # anan-local-stt, anan-yuanbao
]

def is_protected(text: str) -> bool:
    return any(p.search(text) for p in PROTECTED)

def replace_in_file(fp: Path, patterns: list) -> bool:
    """Apply list of (old, new) replacements to file."""
    try:
        content = fp.read_text(encoding="utf-8")
    except Exception:
        return False
    new_content = content
    for old, new in patterns:
        new_content = new_content.replace(old, new)
    if new_content != content:
        fp.write_text(new_content, encoding="utf-8")
        return True
    return False

def main():
    patterns = [
        # Platform/tool identifiers (these ARE brand, should change)
        ("anan-gateway", "anan-gateway"),
        ("anan", "anan"),
        ("anan-index", "anan-index"),
        ("anan-audit@", "anan-audit@"),
        ("anan-dialog-bridge", "anan-dialog-bridge"),
        ("anan-weixin-", "anan-weixin-"),
        ("anan-skills-safe-", "anan-skills-safe-"),
        ("anan-local-stt-", "anan-local-stt-"),
        ("anan-ssh", "anan-ssh"),
        ("anan-overlays", "anan-overlays"),
        ("anan-bot", "anan-bot"),
        ("anan-irc", "anan-irc"),
        ("anan-whatsapp-bridge", "anan-whatsapp-bridge"),
        ("anan-tui", "anan-tui"),
        ("anan-ink", "anan-ink"),
        ("anan-achievements", "anan-achievements"),
        ("anan-cli", "anan-cli"),
        ("anan_cli", "anan_cli"),
        # Constants
        ("ANAN_INDEX_CACHE_FILE", "ANAN_INDEX_CACHE_FILE"),
        # Module references in strings/docstrings
        ("anan_cli", "anan_cli"),
        # Class references
        ("AnanCLI", "AnanCLI"),
        ("AnanCLIConfig", "AnanCLIConfig"),
        # Gateway kind
        ("anan-gateway", "anan-gateway"),
        # x-exa header
        ("x-exa-integration\", \"anan", "x-exa-integration\", \"anan"),
        ("x-exa-integration', 'anan", "x-exa-integration', 'anan"),
        # Model names that should stay as-is
        ("nous-sinoclaw-3", "nous-sinoclaw-3"),  # no change
    ]

    count = 0
    modified = 0
    for fp in ROOT.rglob("*"):
        if not fp.is_file():
            continue
        if fp.suffix not in {".py", ".yaml", ".yml", ".json", ".sh", ".toml", ".md", ".txt"}:
            continue
        if ".git" in fp.parts or "node_modules" in fp.parts or "__pycache__" in fp.parts:
            continue

        count += 1
        if replace_in_file(fp, patterns):
            print(f"  MODIFIED: {fp.relative_to(ROOT)}")
            modified += 1

    print(f"\nFiles scanned: {count}, modified: {modified}")

if __name__ == "__main__":
    main()
