"""Language gate: the repository must stay English-only.

Scans tracked files for Turkish-specific letters. The character set is written
with escapes on purpose: putting the literal letters in this file would make the
gate flag itself.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TR_FROM = "\u00e7\u011f\u0131\u00f6\u015f\u00fc\u00c7\u011e\u0130\u00d6\u015e\u00dc"
TR_TO = "cgiosuCGIOSU"
TABLE = str.maketrans(TR_FROM, TR_TO)
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".woff", ".woff2"}
SKIP_NAMES = {".git"}


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [p for p in Path(".").rglob("*") if p.is_file() and ".git" not in p.parts and ".venv" not in p.parts]
    return [Path(p) for p in out.split("\0") if p]


def main() -> int:
    problems: list[str] = []
    for path in tracked_files():
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if any(part in SKIP_NAMES for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if text.translate(TABLE) == text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.translate(TABLE) != line:
                problems.append(f"{path}:{lineno}: {line.strip()[:120]}")

    if problems:
        print("gate: Turkish characters found in tracked files:", file=sys.stderr)
        for item in problems:
            print("  " + item, file=sys.stderr)
        return 1
    print("language ok: no Turkish characters in tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
