#!/usr/bin/env python3
"""
Rename anan → anan in repository content.
Safe: skips blacklist patterns, processes files in-place.
"""
from pathlib import Path
import re

ROOT = Path("/data/anan")

# Blacklist: patterns that should NOT be renamed
BLACKLIST_RE = re.compile(
    r"anan-[345](?:\.\d+)?|anan_[345]|"
    r"anan-\d+|anan_\d+|"
    r"anan-brain|anan-honcho|"
    r"anan_agent|anan_llm|"
    r"nous-anan|nous_anan",
    re.IGNORECASE
)

# Extensions to process
EXTENSIONS = {".py", ".yaml", ".yml", ".json", ".md", ".sh", ".toml", ".txt", ".nix", ".cfg"}

# Directories to skip
SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "__pycache__", ".docusaurus", "build", "_site", ".pytest_cache"}

def is_blacklisted(text: str) -> bool:
    """Check if text contains only blacklisted anan references."""
    # Remove all non-blacklisted anan references
    cleaned = BLACKLIST_RE.sub("", text)
    # If what remains is just spaces/punctuation, the whole thing was blacklisted
    return not cleaned.strip().replace(" ", "").replace("\n", "").replace("\t", "").replace("_", "").replace("-", "")

def safe_replace(content: str) -> str:
    """
    Replace anan → anan in content, protecting blacklist.
    Uses word-boundary-aware replacement to avoid corrupting identifiers.
    """
    lines = content.splitlines(keepends=True)
    result = []

    for line in lines:
        # Skip if the entire line is blacklisted
        if is_blacklisted(line):
            result.append(line)
            continue

        # Pattern: word-boundary replacements
        # We want to replace anan/Anan/ANAN as:
        # - identifiers (anan_xxx, AnanXxx, SINOCLAW_XXX)
        # - standalone words
        # - path components (~/.anan/)

        new_line = line

        # Replace word-boundary patterns (protected by lookahead/lookbehind)
        # anan as identifier part or word: replace with anan
        # Use negative lookbehind/lookahead to avoid touching blacklisted strings

        # Pattern 1: anan-word-boundary (not followed by - or _ digit pattern like -3, -4)
        # Replace anan when it's a word, identifier, or path component

        # Replace anan → anan (lowercase identifiers and words)
        # anan (not followed by -digit or _digit)
        new_line = re.sub(
            r'anan(?![-\d])',
            'anan',
            new_line
        )

        # Replace Anan → Anan (class names, titles)
        new_line = re.sub(r'Anan', 'Anan', new_line)

        # Replace SINOCLAW_ or ANAN$ → ANAN_ or ANAN (constants)
        new_line = re.sub(r'\bSINOCLAW\b', 'ANAN', new_line)

        # Replace path ~/.anan/ → ~/.anan/
        new_line = new_line.replace("/.anan/", "/.anan/")
        new_line = new_line.replace(".anan/", ".anan/")

        result.append(new_line)

    return "".join(result)

def process_file(fp: Path) -> bool:
    """Process a single file. Returns True if modified."""
    try:
        content = fp.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            content = fp.read_text(encoding="latin-1")
        except Exception:
            return False

    new_content = safe_replace(content)
    if new_content != content:
        fp.write_text(new_content, encoding="utf-8")
        return True
    return False

def main():
    count = 0
    modified = 0

    for fp in ROOT.rglob("*"):
        if not fp.is_file():
            continue
        if any(d in fp.parts for d in SKIP_DIRS):
            continue
        if fp.suffix not in EXTENSIONS:
            continue

        count += 1
        if process_file(fp):
            print(f"  MODIFIED: {fp.relative_to(ROOT)}")
            modified += 1

    print(f"\nTotal files scanned: {count}, modified: {modified}")

if __name__ == "__main__":
    main()
