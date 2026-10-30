"""Two processes on one store: reads never wait, a second writer is told so.

This file exists because of a real failure: the CLI died with
``sqlite3.OperationalError: database is locked`` while the service was ranking a
query, because opening the store always wrote ``schema_version``. A search only
reads, so opening it must not ask for the write lock at all.

The contract these tests pin down:

* opening an existing store requires no write lock, even while another process
  holds one;
* a genuinely blocked write reports contention (S7) instead of leaking
  ``sqlite3``;
* a cache write that cannot happen costs the memo, never the ranking.
"""

from __future__ import annotations

import sqlite3

import pytest

from trick.errors import Contention, StorageIO, TrickError
from trick.textindex import DocumentStore

DOC = {
    "source": "cran",
    "key": "lme4",
    "title": "lme4: Linear Mixed-Effects Models using Eigen and S4",
    "summary": "Fit linear and generalized linear mixed-effects models.",
    "url": "https://CRAN.R-project.org/package=lme4",
    "meta": {"kind": "r-package"},
}


def _seed(path) -> None:
    with DocumentStore(path) as store:
        store.upsert_many([DOC])


def _hold_write_lock(path) -> sqlite3.Connection:
    """Take the exclusive write lock the way a running ingest would."""
    conn = sqlite3.connect(str(path), timeout=0.1)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE meta SET value = value WHERE key = 'schema_version'")
    return conn


def test_opening_a_store_does_not_need_the_write_lock(tmp_path):
    path = tmp_path / "trick.db"
    _seed(path)
    holder = _hold_write_lock(path)
    try:
        # Before the fix this open rewrote schema_version and blocked for the
        # whole busy timeout, then failed with "database is locked".
        with DocumentStore(path) as second:
            assert second.fts5 is True
            assert second.bm25("mixed models") != []
    finally:
        holder.rollback()
        holder.close()


def test_two_processes_can_read_at_the_same_time(tmp_path):
    path = tmp_path / "trick.db"
    _seed(path)
    holder = _hold_write_lock(path)
    try:
        first = DocumentStore(path)
        second = DocumentStore(path)
        try:
            assert first.bm25("mixed") == second.bm25("mixed")
        finally:
            first.close()
            second.close()
    finally:
        holder.rollback()
        holder.close()


def test_a_blocked_write_is_reported_as_contention(tmp_path):
    path = tmp_path / "trick.db"
    _seed(path)
    store = DocumentStore(path)
    store.conn.execute("PRAGMA busy_timeout=50")
    holder = _hold_write_lock(path)
    try:
        with pytest.raises(Contention) as caught:
            store._set_meta("schema_version", "999")
        assert caught.value.s_code == "S7"
    finally:
        holder.rollback()
        holder.close()
        store.close()


def test_a_locked_meta_row_write_does_not_raise_when_nothing_changes(tmp_path):
    path = tmp_path / "trick.db"
    _seed(path)
    store = DocumentStore(path)
    store.conn.execute("PRAGMA busy_timeout=50")
    holder = _hold_write_lock(path)
    try:
        # Same value: no write is attempted, so the lock is irrelevant.
        store._set_meta("schema_version", "1")
    finally:
        holder.rollback()
        holder.close()
        store.close()


def test_a_cache_write_that_cannot_happen_returns_false(tmp_path):
    path = tmp_path / "trick.db"
    _seed(path)
    store = DocumentStore(path)
    store.conn.execute("PRAGMA busy_timeout=50")
    holder = _hold_write_lock(path)
    try:
        assert store.cache_put("some query", "mock", {"rows": []}) is False
    finally:
        holder.rollback()
        holder.close()
        store.close()


def test_the_cache_still_works_when_the_lock_is_free(tmp_path):
    path = tmp_path / "trick.db"
    _seed(path)
    with DocumentStore(path) as store:
        assert store.cache_put("some query", "mock", {"rows": [1]}) is True
        assert store.cache_get("some query", "mock") == {"rows": [1]}


def test_an_unreadable_store_names_the_file(tmp_path):
    path = tmp_path / "trick.db"
    path.write_bytes(b"not a database at all")
    with pytest.raises(StorageIO) as caught:
        DocumentStore(path)
    assert str(path) in str(caught.value)
    assert isinstance(caught.value, TrickError)


def test_a_store_from_a_future_schema_is_refused(tmp_path):
    path = tmp_path / "trick.db"
    _seed(path)
    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(StorageIO) as caught:
        DocumentStore(path)
    assert "schema 99" in str(caught.value)
