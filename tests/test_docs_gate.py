"""The documentation gate must fail on the error class it was written for.

A gate nobody has seen fail is decoration. Each test here injects the exact
mistake the gate exists to catch, so the day the gate rots the suite says so.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "docs_gate.py"


def load_gate():
    spec = importlib.util.spec_from_file_location("docs_gate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load_gate()
COMMANDS = gate.subcommands()
STATE = gate.known_state_paths()


def test_real_documents_pass():
    assert gate.check_text("doc.md", (ROOT / "docs/INSTALL.md").read_text(encoding="utf-8"), COMMANDS, STATE) == []


def test_bare_invocation_is_rejected():
    problems = gate.check_text("doc.md", "run `trick serve` now\n", COMMANDS, STATE)
    assert problems and "no console script" in problems[0]


def test_python_dash_m_form_is_accepted():
    assert gate.check_text("doc.md", "run `.venv/bin/python -m trick serve`\n", COMMANDS, STATE) == []


def test_unknown_subcommand_is_rejected():
    problems = gate.check_text("doc.md", "run `python -m trick teleport`\n", COMMANDS, STATE)
    assert problems and "unknown subcommand" in problems[0]


def test_missing_referenced_path_is_rejected():
    problems = gate.check_text("doc.md", "see `docs/INSTALL-MISSING.md`\n", COMMANDS, STATE)
    assert problems and "does not exist" in problems[0]


def test_state_path_must_match_code_default():
    problems = gate.check_text("doc.md", "store lives at `.local/share/trick/nonsense.db`\n", COMMANDS, STATE)
    assert problems and "not a code default" in problems[0]


def test_real_state_paths_pass():
    text = "`.local/share/trick/trick.db`, `.local/share/trick/bridge.token`, `.local/share/trick/run/trick.sock`\n"
    assert gate.check_text("doc.md", text, COMMANDS, STATE) == []


def test_pkill_line_is_not_a_bare_invocation():
    assert gate.check_text("doc.md", "stop it: `pkill -f 'trick serve'`\n", COMMANDS, STATE) == []


def test_every_documented_subcommand_exists():
    assert {"query", "tui", "serve", "mcp", "ingest", "doctor", "probe"} <= COMMANDS


def test_documents_exist():
    assert (ROOT / "docs/INSTALL.md").exists()
    documents = gate.documents()
    assert any(path.name == "INSTALL.md" for path in documents)
    assert len(documents) >= 3
