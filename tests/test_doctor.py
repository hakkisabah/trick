"""``trick doctor`` answers "why is this not working", so it is tested like a UI.

Two properties matter and both have been violated by a tool like this before:

* it must not change what it measures - a diagnosis that creates the store it
  was asked to look for makes the second run lie about the first;
* every failing line must carry the command that fixes it, otherwise the output
  is a complaint rather than a diagnosis.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from trick.cli import main
from trick.matrix import MatrixWriter
from trick.textindex import DocumentStore


def run(args: list[str], capsys) -> tuple[int, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def make_store(home: Path, docs: int = 3) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    db = home / "trick.db"
    store = DocumentStore(db)
    store.upsert_many(
        [
            {"source": "cran", "key": f"pkg{i}", "title": f"pkg{i}: Mixed models", "summary": "slopes"}
            for i in range(docs)
        ]
    )
    store.close()
    return db


def make_matrix(home: Path, records: int = 3) -> Path:
    path = home / "scores.trk"
    with MatrixWriter(path) as writer:
        for i in range(records):
            vec = [0.0] * 16
            vec[i % 16] = 1.0
            writer.add(i + 1, vec, 0.5, 1)
    return path


def test_missing_store_fails_and_creates_nothing(tmp_path: Path, capsys) -> None:
    home = tmp_path / "state"
    home.mkdir()
    code, out = run(["doctor", "--home", str(home)], capsys)
    assert code == 1
    assert "no store at" in out
    assert "fix: python -m trick ingest" in out
    # The diagnosis must not have created the thing it reported missing.
    assert not (home / "trick.db").exists()
    code_again, out_again = run(["doctor", "--home", str(home)], capsys)
    assert code_again == 1
    assert "no store at" in out_again


def test_healthy_install_exits_zero(tmp_path: Path, capsys) -> None:
    home = tmp_path / "state"
    make_store(home)
    make_matrix(home)
    os.chmod(home, 0o700)
    code, out = run(["doctor", "--home", str(home), "--socket", str(home / "run" / "nope.sock")], capsys)
    assert code == 0, out
    assert "summary:" in out
    assert "0 fail" in out


def test_loose_home_permissions_are_a_warning_with_a_fix(tmp_path: Path, capsys) -> None:
    home = tmp_path / "state"
    make_store(home)
    os.chmod(home, 0o755)
    code, out = run(["doctor", "--home", str(home), "--socket", str(home / "x.sock")], capsys)
    assert code == 0, "a world-readable state dir is a warning, not a failure"
    assert "chmod 700" in out


def test_strict_token_permissions_are_a_failure(tmp_path: Path, capsys) -> None:
    home = tmp_path / "state"
    make_store(home)
    token = home / "bridge.token"
    token.write_text("a" * 64 + "\n", encoding="utf-8")
    os.chmod(token, 0o644)
    code, out = run(["doctor", "--home", str(home), "--socket", str(home / "x.sock")], capsys)
    assert code == 1
    assert "bridge-token" in out
    assert "chmod 600" in out


def test_corrupt_matrix_is_reported_with_a_rebuild_command(tmp_path: Path, capsys) -> None:
    home = tmp_path / "state"
    make_store(home)
    matrix = make_matrix(home)
    matrix.write_bytes(matrix.read_bytes()[:-5])
    code, out = run(["doctor", "--home", str(home), "--socket", str(home / "x.sock")], capsys)
    assert code == 1
    assert "matrix" in out
    assert "trick matrix build" in out


def test_json_output_is_machine_readable(tmp_path: Path, capsys) -> None:
    home = tmp_path / "state"
    make_store(home)
    make_matrix(home)
    os.chmod(home, 0o700)
    code, out = run(["doctor", "--home", str(home), "--socket", str(home / "x.sock"), "--json"], capsys)
    payload = json.loads(out)
    assert code == 0
    assert {"checks", "summary", "engine"} <= set(payload)
    names = {entry["name"] for entry in payload["checks"]}
    assert {"home", "store", "documents", "matrix"} <= names
    assert payload["summary"]["fail"] == 0
    documents = next(entry for entry in payload["checks"] if entry["name"] == "documents")
    # The installer reads this number; it must be a number, not prose to parse.
    assert documents["docs"] == 3
    assert isinstance(documents["decisions"], int)
