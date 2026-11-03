"""Encrypted memory: it must open or refuse, never half-open.

A backup is only worth having if a corrupted one fails loudly. These tests cover
the three ways an archive arrives damaged (wrong passphrase, a flipped byte, a
truncated file), the two ways it must never leak (a credential inside the
payload, the passphrase anywhere on disk), and the merge rule on restore.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("cryptography", reason="the optional backup extra is not installed")

from trick import vault  # noqa: E402
from trick.vault import (
    MAGIC,
    VaultError,
    build_payload,
    describe,
    export_archive,
    import_archive,
    open_sealed,
    read_archive,
    seal,
)

DDL = """
CREATE TABLE decisions (
    query_hash TEXT NOT NULL,
    scorer     TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (query_hash, scorer)
)
"""


def make_home(tmp_path: Path, rows: list[tuple[str, str, int, str]]) -> Path:
    home = tmp_path / "state"
    home.mkdir(parents=True)
    conn = sqlite3.connect(home / "trick.db")
    conn.execute(DDL)
    conn.executemany("INSERT INTO decisions VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return home


def rows_of(home: Path) -> list[tuple]:
    conn = sqlite3.connect(home / "trick.db")
    try:
        return sorted(conn.execute("SELECT query_hash, scorer, created_at FROM decisions"))
    finally:
        conn.close()


def test_an_archive_round_trips() -> None:
    payload = {"format": 1, "decisions": [], "settings": {"LIMIT": 8}}
    blob = seal(json.dumps(payload).encode(), "a long passphrase")
    assert blob.startswith(MAGIC)
    assert json.loads(open_sealed(blob, "a long passphrase")) == payload


def test_the_passphrase_is_not_in_the_file() -> None:
    blob = seal(b'{"format": 1}', "correct horse battery staple")
    assert b"correct horse" not in blob
    assert MAGIC not in blob[1:]


def test_a_wrong_passphrase_refuses() -> None:
    blob = seal(b'{"format": 1}', "right")
    with pytest.raises(VaultError, match="wrong passphrase"):
        open_sealed(blob, "wrong")


def test_a_flipped_byte_refuses() -> None:
    """GCM authenticates the whole payload; one bit is enough to reject it."""
    blob = bytearray(seal(b'{"format": 1}', "pass"))
    blob[-1] ^= 0x01
    with pytest.raises(VaultError, match="wrong passphrase"):
        open_sealed(bytes(blob), "pass")


def test_a_truncated_file_refuses() -> None:
    blob = seal(b'{"format": 1}', "pass")
    with pytest.raises(VaultError):
        open_sealed(blob[: len(MAGIC) + 4], "pass")


def test_a_foreign_file_says_so() -> None:
    with pytest.raises(VaultError, match="wrong magic"):
        open_sealed(b"PK\x03\x04 this is a zip", "pass")


def test_an_empty_passphrase_is_refused() -> None:
    with pytest.raises(VaultError, match="empty"):
        seal(b"{}", "")


def test_credentials_are_replaced_by_their_variable_name() -> None:
    payload = build_payload(
        Path("/nonexistent"),
        {"LAYA_API_KEY": "sk-real-secret", "TRICK_TOKEN": "abc", "LIMIT": 8, "TRICK_URL": "http://x"},
    )
    assert payload["settings"]["LAYA_API_KEY"] == "<from $LAYA_API_KEY>"
    assert payload["settings"]["TRICK_TOKEN"] == "<from $TRICK_TOKEN>"
    assert payload["settings"]["LIMIT"] == 8
    assert payload["settings"]["TRICK_URL"] == "http://x"


def test_export_then_import_merges_without_deleting(tmp_path) -> None:
    rows = [("h1", "remote", 100, "{}"), ("h2", "remote", 100, "{}")]
    source = make_home(tmp_path / "a", rows)
    archive = tmp_path / "memory.trk"
    result = export_archive(source, archive, "pass", {"LIMIT": 8})
    assert result["decisions"] == 2
    assert archive.stat().st_mode & 0o777 == 0o600

    target = make_home(tmp_path / "b", [("h3", "remote", 50, "{}")])
    merged = import_archive(archive, target, "pass")
    assert merged["added"] == 2
    assert merged["updated"] == 0
    assert [row[0] for row in rows_of(target)] == ["h1", "h2", "h3"]


def test_an_older_row_does_not_overwrite_a_newer_one(tmp_path) -> None:
    source = make_home(tmp_path / "a", [("h1", "remote", 100, "old")])
    archive = tmp_path / "memory.trk"
    export_archive(source, archive, "pass")
    target = make_home(tmp_path / "b", [("h1", "remote", 200, "new")])
    merged = import_archive(archive, target, "pass")
    assert merged["added"] == 0 and merged["updated"] == 0 and merged["skipped"] == 1
    conn = sqlite3.connect(target / "trick.db")
    assert conn.execute("SELECT payload FROM decisions").fetchone()[0] == "new"
    conn.close()


def test_a_newer_row_replaces_an_older_one(tmp_path) -> None:
    source = make_home(tmp_path / "a", [("h1", "remote", 300, "newer")])
    archive = tmp_path / "memory.trk"
    export_archive(source, archive, "pass")
    target = make_home(tmp_path / "b", [("h1", "remote", 100, "older")])
    merged = import_archive(archive, target, "pass")
    assert merged["updated"] == 1
    conn = sqlite3.connect(target / "trick.db")
    assert conn.execute("SELECT payload FROM decisions").fetchone()[0] == "newer"
    conn.close()


def test_import_into_an_empty_directory_explains_the_order(tmp_path) -> None:
    source = make_home(tmp_path / "a", [])
    archive = tmp_path / "memory.trk"
    export_archive(source, archive, "pass")
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(VaultError, match="run the installer once"):
        import_archive(archive, empty, "pass")


def test_info_never_needs_the_local_state(tmp_path) -> None:
    source = make_home(tmp_path / "a", [("h1", "remote", 7, "{}")])
    archive = tmp_path / "memory.trk"
    export_archive(source, archive, "pass")
    payload = read_archive(archive, "pass")
    info = describe(payload)
    assert info["decisions"] == 1
    assert info["matrix_records"] is None


def test_a_missing_archive_is_an_error_not_an_empty_backup(tmp_path) -> None:
    with pytest.raises(VaultError, match="no such archive"):
        read_archive(tmp_path / "nope.trk", "pass")


def test_a_newer_format_is_refused_rather_than_guessed(tmp_path) -> None:
    archive = tmp_path / "future.trk"
    archive.write_bytes(seal(json.dumps({"format": 99, "decisions": [], "settings": {}}).encode(), "pass"))
    with pytest.raises(VaultError, match="newer format"):
        read_archive(archive, "pass")


def test_without_the_optional_dependency_the_message_names_the_install(monkeypatch) -> None:
    def missing():
        raise VaultError("encrypted backups need the optional dependency: pip install 'trick[backup]'")

    monkeypatch.setattr(vault, "_crypto", missing)
    with pytest.raises(VaultError, match="trick\\[backup\\]"):
        seal(b"{}", "pass")


def test_the_passphrase_can_come_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("TRICK_BACKUP_PASSPHRASE", "from-env")
    assert vault.read_passphrase() == "from-env"
