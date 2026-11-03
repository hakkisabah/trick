"""Portable, encrypted memory.

What is "memory" here, and what is not. The catalogue (CRAN, Bioconductor,
arXiv, OpenAlex rows, 44 MB of them) is *not* memory: it is public, re-derivable
with one command, and backing it up would waste the quota the cheapest cloud box
has. Memory is the small thing this installation learned and cannot re-derive:

* the decision cache - which query was answered, with which scorer, at which
  time. Restoring it means a repeat query costs no model call and no latency;
* the settings snapshot, with every credential replaced by the name of the
  environment variable that holds it. An archive that carries an API key is a
  liability, not a backup;
* the matrix header (count, dimension, fingerprint) so a restore can tell you
  whether it belongs to the catalogue you now have.

Format, one file:

    b"TRICKV1\\0" | salt(16) | nonce(12) | AES-256-GCM(payload)

The key is scrypt(passphrase, salt, n=2**15, r=8, p=1). GCM authenticates the
whole payload, so a flipped byte fails the restore instead of producing a
half-written memory. The payload is a JSON document, not a tar: it is small, it
stays readable in a debugger once decrypted, and it cannot carry a path traversal
because there are no paths in it.

The passphrase is read from ``TRICK_BACKUP_PASSPHRASE`` or asked for on the
terminal. It is never written to disk, never logged, never echoed.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

MAGIC = b"TRICKV1\0"
SALT_BYTES = 16
NONCE_BYTES = 12
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
KEY_BYTES = 32
FORMAT_VERSION = 1

# Anything whose name looks like this is replaced by a pointer before export.
SECRET_HINTS = ("PASS", "SECRET", "TOKEN", "KEY", "CREDENTIAL", "PASSWORD")


class VaultError(Exception):
    """A backup that cannot be produced, opened or trusted."""


def _crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - exercised by the skip test
        raise VaultError(
            "encrypted backups need the optional dependency: "
            "pip install 'trick[backup]' (or pip install 'cryptography>=42')"
        ) from exc
    return AESGCM


def derive_key(passphrase: str, salt: bytes) -> bytes:
    if not passphrase:
        raise VaultError("the passphrase is empty")
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
        # OpenSSL wants room for the whole working set (128 * r * (N + 2)) plus
        # its own bookkeeping; the tighter bound is refused with "memory limit
        # exceeded" before a single byte is derived.
        maxmem=128 * SCRYPT_R * (SCRYPT_N + 2) + 1024 * 1024,
    )


def seal(payload: bytes, passphrase: str) -> bytes:
    AESGCM = _crypto()
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    key = derive_key(passphrase, salt)
    return MAGIC + salt + nonce + AESGCM(key).encrypt(nonce, payload, MAGIC + salt + nonce)


def open_sealed(blob: bytes, passphrase: str) -> bytes:
    AESGCM = _crypto()
    if not blob.startswith(MAGIC):
        raise VaultError("this is not a Trick archive (wrong magic)")
    body = len(MAGIC)
    salt = blob[body : body + SALT_BYTES]
    nonce = blob[body + SALT_BYTES : body + SALT_BYTES + NONCE_BYTES]
    sealed = blob[body + SALT_BYTES + NONCE_BYTES :]
    if len(salt) != SALT_BYTES or len(nonce) != NONCE_BYTES or not sealed:
        raise VaultError("the archive is truncated")
    key = derive_key(passphrase, salt)
    try:
        return AESGCM(key).decrypt(nonce, sealed, MAGIC + salt + nonce)
    except Exception as exc:  # InvalidTag, and nothing else is expected
        raise VaultError(
            "cannot open the archive: wrong passphrase, or the file was changed "
            "after it was written"
        ) from exc


def read_passphrase(confirm: bool = False) -> str:
    """From the environment, else the terminal. Never from an argument."""
    env = os.environ.get("TRICK_BACKUP_PASSPHRASE")
    if env:
        return env
    if not sys.stdin.isatty():
        raise VaultError("set TRICK_BACKUP_PASSPHRASE, or run this on a terminal")
    first = getpass.getpass("backup passphrase: ")
    if confirm:
        again = getpass.getpass("again: ")
        if first != again:
            raise VaultError("the passphrases do not match")
    return first


def _redact(payload: dict) -> dict:
    """Replace credential-shaped values with where to find them instead."""
    out = {}
    for name, value in payload.items():
        upper = str(name).upper()
        if any(hint in upper for hint in SECRET_HINTS) and value:
            out[name] = f"<from ${name}>"
        else:
            out[name] = value
    return out


def _decisions(db: Path) -> list[dict]:
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute("SELECT * FROM decisions ORDER BY created_at")]
    except sqlite3.Error as exc:
        raise VaultError(f"cannot read the decision cache ({exc})") from exc
    finally:
        conn.close()
    return rows


def _matrix_header(matrix: Path) -> dict | None:
    if not matrix.exists():
        return None
    try:
        from .matrix import SIG_DIM, MatrixIndex

        index = MatrixIndex(matrix)
        try:
            return {
                "count": index.count,
                "dimensions": SIG_DIM,
                "bytes": matrix.stat().st_size,
                "sha256": hashlib.sha256(matrix.read_bytes()).hexdigest(),
            }
        finally:
            index.close()
    except Exception:  # a broken matrix is not a reason to refuse the backup
        return None


def build_payload(home: Path, settings: dict | None = None) -> dict:
    home = Path(home)
    return {
        "format": FORMAT_VERSION,
        "created_at": int(time.time()),
        "trick_version": _version(),
        "settings": _redact(dict(settings or {})),
        "matrix": _matrix_header(home / "scores.trk"),
        "decisions": _decisions(home / "trick.db"),
    }


def _version() -> str:
    try:
        from . import __version__

        return str(__version__)
    except Exception:  # pragma: no cover
        return "unknown"


def export_archive(home: Path, out: Path, passphrase: str, settings: dict | None = None) -> dict:
    payload = build_payload(home, settings)
    blob = seal(json.dumps(payload, ensure_ascii=False).encode("utf-8"), passphrase)
    out = Path(out)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_bytes(blob)
    os.chmod(tmp, 0o600)
    os.replace(tmp, out)
    return {
        "path": str(out),
        "bytes": len(blob),
        "decisions": len(payload["decisions"]),
        "sha256": hashlib.sha256(blob).hexdigest(),
    }


def read_archive(path: Path, passphrase: str) -> dict:
    path = Path(path)
    if not path.exists():
        raise VaultError(f"no such archive: {path}")
    payload = json.loads(open_sealed(path.read_bytes(), passphrase).decode("utf-8"))
    if int(payload.get("format", 0)) > FORMAT_VERSION:
        raise VaultError(
            f"the archive was written by a newer format ({payload.get('format')}) "
            f"than this build understands ({FORMAT_VERSION})"
        )
    for required in ("format", "decisions", "settings"):
        if required not in payload:
            raise VaultError(f"the archive has no {required!r} section")
    return payload


def describe(payload: dict) -> dict:
    matrix = payload.get("matrix") or {}
    return {
        "created_at": payload.get("created_at"),
        "trick_version": payload.get("trick_version"),
        "decisions": len(payload.get("decisions") or []),
        "settings": len(payload.get("settings") or {}),
        "matrix_records": matrix.get("count"),
        "matrix_dimensions": matrix.get("dimensions"),
        "matrix_sha256": (matrix.get("sha256") or "")[:16] or None,
    }


def import_archive(path: Path, home: Path, passphrase: str) -> dict:
    """Merge decisions into the local cache. Newer rows win; nothing is deleted."""
    payload = read_archive(path, passphrase)
    home = Path(home)
    db = home / "trick.db"
    incoming = payload.get("decisions") or []
    if not db.exists():
        raise VaultError(
            f"{db} does not exist yet; run the installer once so the cache has a table to merge into"
        )
    conn = sqlite3.connect(db)
    added = updated = 0
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(decisions)")]
        if not columns:
            raise VaultError("the local cache has no decisions table")
        for row in incoming:
            row = {k: v for k, v in row.items() if k in columns}
            if "query_hash" not in row or "scorer" not in row:
                continue
            existing = conn.execute(
                "SELECT created_at FROM decisions WHERE query_hash = ? AND scorer = ?",
                (row["query_hash"], row["scorer"]),
            ).fetchone()
            if existing and existing[0] >= row.get("created_at", 0):
                continue
            names = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            conn.execute(
                f"INSERT OR REPLACE INTO decisions ({names}) VALUES ({marks})",
                tuple(row.values()),
            )
            if existing:
                updated += 1
            else:
                added += 1
        conn.commit()
    finally:
        conn.close()
    local_matrix = _matrix_header(home / "scores.trk")
    remote_matrix = payload.get("matrix") or {}
    return {
        "added": added,
        "updated": updated,
        "skipped": len(incoming) - added - updated,
        "matrix_matches": bool(
            local_matrix
            and remote_matrix
            and local_matrix.get("sha256") == remote_matrix.get("sha256")
        ),
        "archive_created_at": payload.get("created_at"),
    }


__all__ = [
    "FORMAT_VERSION",
    "MAGIC",
    "VaultError",
    "build_payload",
    "describe",
    "export_archive",
    "import_archive",
    "open_sealed",
    "read_archive",
    "read_passphrase",
    "seal",
]
