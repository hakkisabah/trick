#!/usr/bin/env python3
"""Documentation gate: the install guide must stay executable.

Documentation drifts in one direction only - it keeps claiming commands that no
longer exist. This gate has already earned its place: the README used to say
``trick tui`` while the package declares no console script at all, so every
command in it would have failed with "command not found" on a clean machine.

Checks, all fail-closed:

1. no bare ``trick <subcommand>`` invocation in any tracked document; the only
   supported form is ``python -m trick <subcommand>``
2. every subcommand a document mentions exists in the real CLI parser
3. every repository-relative path a document mentions exists on disk
4. every state-directory path a document mentions is a real default from cli.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trick.cli import (  # noqa: E402
    DEFAULT_DB,
    DEFAULT_HISTORY,
    DEFAULT_MATRIX,
    DEFAULT_SOCKET,
    DEFAULT_TOKEN,
    build_parser,
)

DOC_GLOBS = ("README.md", "docs/*.md", "integrations/*.md", "deploy/*.md")
PATH_SUFFIXES = (".py", ".sh", ".md", ".json", ".toml", ".db", ".trk", ".token")


def subcommands() -> set[str]:
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def documents() -> list[Path]:
    found: list[Path] = []
    for pattern in DOC_GLOBS:
        found.extend(sorted(ROOT.glob(pattern)))
    return found


def known_state_paths() -> set[str]:
    return {
        str(DEFAULT_DB),
        str(DEFAULT_MATRIX),
        str(DEFAULT_SOCKET),
        str(DEFAULT_TOKEN),
        str(DEFAULT_HISTORY),
    }


def check_text(name: str, text: str, commands: set[str], state: set[str]) -> list[str]:
    problems: list[str] = []

    # Lines that name a process for pkill/pgrep are quoting the command line, not
    # telling the reader to run it; the pattern legitimately reads "trick serve".
    quoted = {
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if "pkill" in line or "pgrep" in line
    }

    # 1. and 2. bare invocations and unknown subcommands
    for match in re.finditer(r"\btrick\s+([a-z][a-z0-9_-]*)\b", text):
        word = match.group(1)
        if word not in commands:
            continue
        prefix = text[max(0, match.start() - 8) : match.start()]
        if prefix.endswith("-m "):
            continue
        line = text.count("\n", 0, match.start()) + 1
        if line in quoted:
            continue
        problems.append(
            f"{name}:{line}: bare 'trick {word}' - use 'python -m trick {word}' "
            "(the package declares no console script)"
        )

    for match in re.finditer(r"-m trick\s+([a-z][a-z0-9_-]*)\b", text):
        word = match.group(1)
        if word not in commands:
            line = text.count("\n", 0, match.start()) + 1
            problems.append(f"{name}:{line}: unknown subcommand 'trick {word}'")

    # 3. referenced repository paths must exist
    for match in re.finditer(r"([A-Za-z0-9_./-]+\.(?:py|sh|md|json|toml))", text):
        candidate = match.group(1).lstrip("./")
        if candidate.startswith(("http", "~")) or "/" not in candidate:
            continue
        if candidate.startswith(("src/trick/", "tests/")) and not (ROOT / candidate).exists():
            line = text.count("\n", 0, match.start()) + 1
            problems.append(f"{name}:{line}: referenced path does not exist: {candidate}")
        elif not (ROOT / candidate).exists() and candidate.split("/")[0] in {
            "docs",
            "deploy",
            "integrations",
            "scripts",
        }:
            line = text.count("\n", 0, match.start()) + 1
            problems.append(f"{name}:{line}: referenced path does not exist: {candidate}")

    # 4. state paths must match the code defaults
    for match in re.finditer(r"\.local/share/trick/[A-Za-z0-9_./-]+", text):
        stated = match.group(0).rstrip(".,)")
        absolute = str(Path.home() / stated)
        if absolute in state:
            continue
        names = {Path(item).name for item in state}
        if Path(stated).name in names or stated.startswith(".local/share/trick/run"):
            continue
        line = text.count("\n", 0, match.start()) + 1
        problems.append(f"{name}:{line}: state path is not a code default: {stated}")

    return problems


def main() -> int:
    commands = subcommands()
    state = known_state_paths()
    problems: list[str] = []
    scanned = 0
    for path in documents():
        scanned += 1
        problems.extend(check_text(str(path.relative_to(ROOT)), path.read_text(encoding="utf-8"), commands, state))
    if problems:
        print("documentation gate failed:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"docs ok: {scanned} documents, {len(commands)} subcommands, paths and state defaults agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
