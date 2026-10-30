"""SQLite document store: BM25 rescoring and the decision cache.

Why SQLite instead of a second binary format:

* FTS5 gives BM25 and an inverted index for free, on disk, without a server;
* the file is one page-cached artefact, so the second search stage does not add
  a resident data structure to a 512 MB host;
* ``docs`` and ``docs_fts`` are kept in sync by triggers, so a crash cannot leave
  the index pointing at missing rows.

Two-stage search contract: the int8 signature scan produces a wide candidate
set; this module re-orders that set with BM25 over full text. Rescoring is
restricted to the candidate ids, so cost depends on the shortlist, not on the
corpus size.

The decision cache stores what laya already answered for a given query hash, so
repeating a query costs nothing. It is a cache, never a source of truth: entries
expire and are keyed by the scorer name.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .errors import Contention, ContractViolation, StorageIO

SCHEMA_VERSION = 1
DEFAULT_CACHE_TTL_S = 7 * 24 * 3600
MAX_DOCS_TITLE = 400
MAX_DOCS_SUMMARY = 4000
# How long a statement waits for the write lock before giving up. The store is
# shared: the service may be ranking a query while a second process ingests or
# the terminal UI writes a cache entry. WAL lets readers run regardless; this
# timeout is what a second *writer* waits instead of failing instantly.
BUSY_TIMEOUT_MS = 5000

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS docs (
    doc_id     INTEGER PRIMARY KEY,
    source     TEXT NOT NULL,
    key        TEXT NOT NULL,
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    meta       TEXT NOT NULL DEFAULT '{}',
    fetched_at INTEGER NOT NULL,
    UNIQUE (source, key)
);

CREATE INDEX IF NOT EXISTS docs_source ON docs (source);

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title,
    summary,
    content='docs',
    content_rowid='doc_id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts (rowid, title, summary) VALUES (new.doc_id, new.title, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts (docs_fts, rowid, title, summary) VALUES ('delete', old.doc_id, old.title, old.summary);
END;

CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
    INSERT INTO docs_fts (docs_fts, rowid, title, summary) VALUES ('delete', old.doc_id, old.title, old.summary);
    INSERT INTO docs_fts (rowid, title, summary) VALUES (new.doc_id, new.title, new.summary);
END;

CREATE TABLE IF NOT EXISTS metrics (
    doc_id     INTEGER PRIMARY KEY REFERENCES docs (doc_id) ON DELETE CASCADE,
    weight     REAL NOT NULL DEFAULT 0.0,
    note       TEXT NOT NULL DEFAULT '',
    fetched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    query_hash TEXT NOT NULL,
    scorer     TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (query_hash, scorer)
);
"""


@dataclass(frozen=True)
class StoredDoc:
    doc_id: int
    source: str
    key: str
    title: str
    summary: str
    url: str
    meta: dict
    fetched_at: int


def query_hash(query: str, *, extra: str = "") -> str:
    """Stable hash of a normalised query, used as the cache key."""
    normalised = " ".join(str(query).lower().split())
    return hashlib.blake2s(f"{normalised}\x00{extra}".encode("utf-8"), digest_size=16).hexdigest()


def _fts_expression(query: str) -> str:
    """Turn free text into a safe FTS5 OR-expression of quoted tokens."""
    tokens: list[str] = []
    for raw in str(query).lower().replace("-", " ").split():
        token = "".join(ch for ch in raw if ch.isalnum() or ch in "+#")
        if len(token) >= 2:
            tokens.append('"' + token.replace('"', '""') + '"')
    # Deduplicate while keeping order: repeated tokens only inflate the query.
    seen: set[str] = set()
    unique = [t for t in tokens if not (t in seen or seen.add(t))]
    return " OR ".join(unique)


class DocumentStore:
    """Owns ``trick.db``: documents, their FTS5 mirror and the decision cache."""

    def __init__(self, path: str | os.PathLike[str], *, cache_ttl_s: int = DEFAULT_CACHE_TTL_S) -> None:
        self.path = Path(path)
        self.cache_ttl_s = cache_ttl_s
        self.fts5 = True
        self._conn: sqlite3.Connection | None = None
        self._open()

    # -- lifecycle ---------------------------------------------------------
    def _open(self) -> None:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_MS / 1000.0)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_MS)}")
        except sqlite3.DatabaseError as exc:
            raise StorageIO(f"cannot open {self.path}: {exc}") from exc
        self._conn = conn
        # A search must never need the write lock. Only a store that is new (or
        # written by an older schema) is created or migrated here; an existing
        # store is opened read-only in practice, which is what lets the CLI and
        # the service point at the same file at the same time. Before this
        # check, every open wrote ``schema_version`` and the second process died
        # with "database is locked" while the first one was ranking.
        version = self._schema_version()
        if version is None:
            self._bootstrap()
        elif version != SCHEMA_VERSION:
            conn.close()
            self._conn = None
            raise StorageIO(
                f"store schema {version} is not usable by this build (expected {SCHEMA_VERSION}): "
                f"remove {self.path} and re-run ingest"
            )
        else:
            self._check_tables()
        if not self._probe_fts():
            # Minimal mode: no FTS5 in this SQLite build. Search still works via
            # LIKE, only slower; the caller is told through ``fts5``.
            self.fts5 = False
            if self._writable():
                self._conn.execute("DROP TABLE IF EXISTS docs_fts")

    def _schema_version(self) -> int | None:
        """Recorded schema version, or ``None`` when the store has no schema yet."""
        assert self._conn is not None
        try:
            row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.DatabaseError:
            return None
        if row is None:
            return None
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return None

    def _check_tables(self) -> None:
        """Cheap integrity gate on an existing store: the tables must be there."""
        assert self._conn is not None
        try:
            for table in ("meta", "docs", "decisions"):
                self._conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchall()
        except sqlite3.DatabaseError as exc:
            raise StorageIO(f"{self.path} is not a usable store: {exc}") from exc

    def _writable(self) -> bool:
        """True when this connection can take the write lock right now."""
        assert self._conn is not None
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return False
        self._conn.rollback()
        return True

    def _bootstrap(self) -> None:
        assert self._conn is not None
        try:
            self._conn.executescript(SCHEMA)
        except sqlite3.DatabaseError as exc:
            raise StorageIO(f"cannot create {self.path}: {exc}") from exc
        self._set_meta("schema_version", str(SCHEMA_VERSION))

    def _probe_fts(self) -> bool:
        """Can this build query the FTS5 table? Read-only on purpose.

        The obvious probe is ``INSERT INTO docs_fts(docs_fts) VALUES
        ('integrity-check')``, which is a *write*: it takes the write lock, and
        when another process holds it the probe fails and the store silently
        downgrades to LIKE matching - a correct answer at many times the cost,
        with no error to explain it. Counting rows proves the module is there
        without touching the lock.
        """
        assert self._conn is not None
        try:
            self._conn.execute("SELECT count(*) FROM docs_fts").fetchall()
        except sqlite3.DatabaseError:
            return False
        return True

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.commit()
            except sqlite3.DatabaseError:
                pass
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "DocumentStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageIO("store is closed")
        return self._conn

    def _set_meta(self, key: str, value: str) -> None:
        """Write one meta row, but only when it actually changes.

        Reading first means opening an existing store takes no write lock, which
        is what allows several processes to share ``trick.db``. A concurrent
        writer is reported as contention (S7) instead of leaking ``sqlite3``.
        """
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is not None and str(row["value"]) == str(value):
            return
        try:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self.conn.commit()
        except sqlite3.OperationalError as exc:
            self.conn.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise Contention(f"another writer holds {self.path}: {exc}") from exc
            raise StorageIO(f"cannot write {self.path}: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            self.conn.rollback()
            raise StorageIO(f"cannot write {self.path}: {exc}") from exc

    # -- documents ---------------------------------------------------------
    def upsert_many(self, docs: Iterable[dict], *, now: int | None = None) -> int:
        """Insert or refresh documents; returns the number of rows touched."""
        stamp = int(time.time()) if now is None else int(now)
        rows = 0
        try:
            with self.conn:
                for doc in docs:
                    source = str(doc.get("source") or "").strip()
                    key = str(doc.get("key") or "").strip()
                    title = str(doc.get("title") or "").strip()
                    if not source or not key or not title:
                        raise ContractViolation("document needs source, key and title")
                    self.conn.execute(
                        """
                        INSERT INTO docs (source, key, title, summary, url, meta, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (source, key) DO UPDATE SET
                            title=excluded.title,
                            summary=excluded.summary,
                            url=excluded.url,
                            meta=excluded.meta,
                            fetched_at=excluded.fetched_at
                        """,
                        (
                            source,
                            key,
                            title[:MAX_DOCS_TITLE],
                            str(doc.get("summary") or "")[:MAX_DOCS_SUMMARY],
                            str(doc.get("url") or ""),
                            json.dumps(doc.get("meta") or {}, ensure_ascii=False),
                            stamp,
                        ),
                    )
                    rows += 1
        except sqlite3.DatabaseError as exc:
            raise StorageIO(f"cannot upsert documents: {exc}") from exc
        return rows

    def set_weights(self, weights: Iterable[tuple[str, str, float, str]], *, now: int | None = None) -> int:
        """Store a canonicality prior per document: ``(source, key, weight, note)``.

        ``weight`` is a 0..1 log-scaled popularity/reverse-dependency score that
        the ranking uses to prefer established tools over keyword-dense
        newcomers. It is ingested, never invented at query time.
        """
        stamp = int(time.time()) if now is None else int(now)
        rows = 0
        try:
            with self.conn:
                for source, key, weight, note in weights:
                    doc_id = self.doc_id_for(str(source), str(key))
                    if doc_id is None:
                        continue
                    clamped = 0.0 if weight < 0.0 else (1.0 if weight > 1.0 else float(weight))
                    self.conn.execute(
                        "INSERT INTO metrics (doc_id, weight, note, fetched_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(doc_id) DO UPDATE SET weight=excluded.weight, "
                        "note=excluded.note, fetched_at=excluded.fetched_at",
                        (doc_id, clamped, str(note), stamp),
                    )
                    rows += 1
        except sqlite3.DatabaseError as exc:
            raise StorageIO(f"cannot store weights: {exc}") from exc
        return rows

    def weights_for(self, doc_ids: Sequence[int]) -> dict[int, float]:
        """Return the prior for the given documents; missing ids default to 0.0."""
        ids = [int(i) for i in doc_ids]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        try:
            rows = self.conn.execute(
                f"SELECT doc_id, weight FROM metrics WHERE doc_id IN ({placeholders})", ids
            ).fetchall()
        except sqlite3.DatabaseError:
            return {}
        return {int(row["doc_id"]): float(row["weight"]) for row in rows}

    def weight_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM metrics").fetchone()
        return int(row["n"]) if row else 0

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM docs").fetchone()
        return int(row["n"]) if row else 0

    def get(self, doc_id: int) -> StoredDoc | None:
        row = self.conn.execute("SELECT * FROM docs WHERE doc_id = ?", (int(doc_id),)).fetchone()
        return _to_doc(row) if row else None

    def get_many(self, doc_ids: Sequence[int]) -> dict[int, StoredDoc]:
        ids = [int(i) for i in doc_ids]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(f"SELECT * FROM docs WHERE doc_id IN ({placeholders})", ids).fetchall()
        return {int(row["doc_id"]): _to_doc(row) for row in rows}

    def sources_for(self, doc_ids: Sequence[int]) -> dict[int, str]:
        """Map ``doc_id -> source`` for the given ids.

        The shortlist cut needs the source of every candidate to apply a
        per-source quota, and it needs it cheaply: fetching whole rows for the
        prefilter union would read columns nobody looks at. Ids missing from the
        store are simply absent from the result.
        """
        ids = [int(i) for i in doc_ids]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT doc_id, source FROM docs WHERE doc_id IN ({placeholders})", ids
        ).fetchall()
        return {int(row["doc_id"]): str(row["source"]) for row in rows}

    def iter_docs(self, *, limit: int | None = None) -> Iterable[StoredDoc]:
        sql = "SELECT * FROM docs ORDER BY doc_id"
        args: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            args = (int(limit),)
        for row in self.conn.execute(sql, args):
            yield _to_doc(row)

    def doc_id_for(self, source: str, key: str) -> int | None:
        row = self.conn.execute(
            "SELECT doc_id FROM docs WHERE source = ? AND key = ?", (str(source), str(key))
        ).fetchone()
        return int(row["doc_id"]) if row else None

    # -- search ------------------------------------------------------------
    def bm25(self, query: str, *, limit: int = 50, doc_ids: Sequence[int] | None = None) -> list[tuple[int, float]]:
        """Return ``(doc_id, score)`` best-first, score normalised to 0..1."""
        expression = _fts_expression(query)
        if not expression:
            return []
        raw = self._bm25_raw(expression, limit=limit, doc_ids=doc_ids)
        if not raw:
            return []
        best = max(score for _, score in raw) or 1.0
        return [(doc_id, score / best) for doc_id, score in raw]

    def _bm25_raw(self, expression: str, *, limit: int, doc_ids: Sequence[int] | None) -> list[tuple[int, float]]:
        assert self.conn is not None
        if not self.fts5:
            return self._like_raw(expression, limit=limit, doc_ids=doc_ids)
        sql = "SELECT rowid AS doc_id, bm25(docs_fts) AS rank FROM docs_fts WHERE docs_fts MATCH ?"
        args: list = [expression]
        if doc_ids is not None:
            ids = [int(i) for i in doc_ids]
            if not ids:
                return []
            sql += f" AND rowid IN ({','.join('?' * len(ids))})"
            args.extend(ids)
        # bm25() is negative and more negative is better.
        sql += " ORDER BY rank LIMIT ?"
        args.append(max(1, int(limit)))
        try:
            rows = self.conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
        out = [(int(r["doc_id"]), -float(r["rank"])) for r in rows]
        out.sort(key=lambda item: (-item[1], item[0]))
        return out

    def _like_raw(self, expression: str, *, limit: int, doc_ids: Sequence[int] | None) -> list[tuple[int, float]]:
        """Fallback when FTS5 is absent: token counting over LIKE."""
        tokens = [t.strip('"') for t in expression.split(" OR ") if t.strip('"')]
        if not tokens:
            return []
        sql = "SELECT doc_id, title, summary FROM docs WHERE 1=1"
        args: list = []
        if doc_ids is not None:
            ids = [int(i) for i in doc_ids]
            if not ids:
                return []
            sql += f" AND doc_id IN ({','.join('?' * len(ids))})"
            args.extend(ids)
        rows = self.conn.execute(sql, args).fetchall()
        scored: list[tuple[int, float]] = []
        for row in rows:
            haystack = f"{row['title']} {row['summary']}".lower()
            hits = sum(1 for token in set(tokens) if token in haystack)
            if hits:
                scored.append((int(row["doc_id"]), float(hits)))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[: max(1, int(limit))]

    def rescore(self, query: str, doc_ids: Sequence[int]) -> dict[int, float]:
        """BM25 over an explicit shortlist; missing ids simply get no score."""
        return dict(self.bm25(query, limit=max(len(doc_ids), 1), doc_ids=doc_ids))

    # -- decision cache ----------------------------------------------------
    def cache_get(self, query: str, scorer: str, *, extra: str = "", now: int | None = None) -> dict | None:
        stamp = int(time.time()) if now is None else int(now)
        row = self.conn.execute(
            "SELECT created_at, payload FROM decisions WHERE query_hash = ? AND scorer = ?",
            (query_hash(query, extra=extra), scorer),
        ).fetchone()
        if not row:
            return None
        if stamp - int(row["created_at"]) > self.cache_ttl_s:
            return None
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def cache_put(self, query: str, scorer: str, payload: dict, *, extra: str = "", now: int | None = None) -> bool:
        """Store one decision. Returns False when the cache could not be written.

        The cache is an optimisation: a busy writer or a full disk must not turn
        a successful ranking into a failed request. The caller records the
        failure in the report instead.
        """
        stamp = int(time.time()) if now is None else int(now)
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO decisions (query_hash, scorer, created_at, payload) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(query_hash, scorer) DO UPDATE SET created_at=excluded.created_at, payload=excluded.payload",
                    (
                        query_hash(query, extra=extra),
                        scorer,
                        stamp,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
        except sqlite3.DatabaseError:
            return False
        return True

    def prune(self, *, now: int | None = None, keep_days: int = 30) -> int:
        """Drop expired decisions and stale documents; returns rows removed."""
        stamp = int(time.time()) if now is None else int(now)
        cutoff = stamp - keep_days * 86400
        removed = 0
        try:
            with self.conn:
                cur = self.conn.execute("DELETE FROM decisions WHERE created_at < ?", (cutoff,))
                removed += cur.rowcount or 0
                cur = self.conn.execute("DELETE FROM docs WHERE fetched_at < ?", (cutoff,))
                removed += cur.rowcount or 0
        except sqlite3.DatabaseError as exc:
            raise StorageIO(f"cannot prune store: {exc}") from exc
        return removed


def _to_doc(row: sqlite3.Row) -> StoredDoc:
    try:
        meta = json.loads(row["meta"]) if row["meta"] else {}
    except ValueError:
        meta = {}
    return StoredDoc(
        doc_id=int(row["doc_id"]),
        source=str(row["source"]),
        key=str(row["key"]),
        title=str(row["title"]),
        summary=str(row["summary"]),
        url=str(row["url"]),
        meta=meta if isinstance(meta, dict) else {},
        fetched_at=int(row["fetched_at"]),
    )


__all__ = [
    "DocumentStore",
    "SCHEMA_VERSION",
    "StoredDoc",
    "query_hash",
]
