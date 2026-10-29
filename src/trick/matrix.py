"""The Trick score matrix: a fixed-record, mmap-friendly, crash-safe index.

File layout (little endian)::

    offset 0                     48 byte header
      magic      8s   b"TRKMAT\\x01\\x00"
      version    H    1
      dim        B    16          signature components
      rec_size   B    24          bytes per record
      count      I    record count
      crc32      I    CRC32 of the body
      flags      I    header flags (bit 0: synthetic content)
      qscale     f    quantisation scale (127.0)
      pad        ...  zero filled up to 48 bytes
    offset 48                    count * 24 byte records
      id         I    stable record id
      sig        16s  int8 unit-vector signature
      score      H    0..65535 mapped to 0..1
      flags      B    per-record flags
      pad        x    one alignment byte

Guarantees proven by the test suite:

* a truncated, corrupted or missing file never raises out of the loader: it
  yields a ``MatrixCorrupt``/``MatrixMissing`` error and the caller can fall back
  to the emergency matrix;
* writes are atomic: a crash leaves either the old matrix or the new one, never a
  hybrid, and a failed write keeps the previous file intact;
* a second concurrent writer is refused with ``Contention`` instead of
  interleaving bytes;
* scanning is lazy: ``mmap`` pages are only touched for the records that are
  ranked, so start-up cost does not grow with the number of records.
"""

from __future__ import annotations

import mmap
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Sequence

from .errors import (
    Contention,
    ContractViolation,
    MatrixCorrupt,
    MatrixMissing,
    ResourceExhausted,
    StorageIO,
)
from .quant import SIG_DIM, decode_score, encode_score, quantize

MAGIC = b"TRKMAT\x01\x00"
VERSION = 1
RECORD_SIZE = 24
HEADER_SIZE = 48
# NOTE: on CPython 3.14 struct 'F' is an 8-byte complex, not a 4-byte float.
# Use lowercase 'f' explicitly, otherwise the header silently grows to 32 bytes.
HEADER_STRUCT = struct.Struct("<8sHBBIIIf")  # 28 bytes, padded to HEADER_SIZE
RECORD_STRUCT = struct.Struct(f"<I{SIG_DIM}sHBx")  # 24 bytes

assert HEADER_STRUCT.size <= HEADER_SIZE
assert HEADER_STRUCT.size == 28, HEADER_STRUCT.size
assert RECORD_STRUCT.size == RECORD_SIZE

# header flags
HF_SYNTHETIC = 0x01

# record flags
RF_SCORED = 0x01
RF_SYNTHETIC = 0x02
RF_STALE = 0x04

DEFAULT_K = 12


@dataclass(frozen=True)
class Hit:
    """:class:`MatrixIndex.scan` result row."""

    cosine: float
    record_id: int
    score: float
    flags: int

    @property
    def tested(self) -> bool:
        """True when laya produced the score, False for retrieval-only rows."""
        return bool(self.flags & RF_SCORED)


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


class MatrixWriter:
    """Build a matrix in ``<path>.tmp`` and publish it with one atomic rename."""

    def __init__(self, path: str | os.PathLike[str], header_flags: int = 0, qscale: float = 127.0) -> None:
        self.path = Path(path)
        self.tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        self.header_flags = header_flags
        self.qscale = qscale
        self.count = 0
        self._crc = 0
        self._fh: BinaryIO | None = None
        self._lock_fh = None
        self._closed = False
        self._seen_ids: set[int] = set()

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "MatrixWriter":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.finish()
        else:
            self.abort()

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        try:
            self._fh = open(self.tmp_path, "wb")
            self._fh.write(b"\x00" * HEADER_SIZE)
        except OSError as exc:
            self._release_lock()
            raise StorageIO(f"cannot create {self.tmp_path}: {exc}") from exc

    def _acquire_lock(self) -> None:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX
            return
        lp = _lock_path(self.path)
        try:
            fh = open(lp, "w")
        except OSError as exc:
            raise StorageIO(f"cannot open lock file {lp}: {exc}") from exc
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise Contention(f"another writer holds {lp}") from exc
        self._lock_fh = fh

    def _release_lock(self) -> None:
        if self._lock_fh is not None:
            try:
                import fcntl

                fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            self._lock_fh.close()
            self._lock_fh = None

    # -- writing -----------------------------------------------------------
    def add(self, record_id: int, signature: bytes | Sequence[float], score: float = 0.0, flags: int = 0) -> None:
        """Append one record. ``signature`` is raw int8 bytes or a 16 float vector."""
        if self._fh is None:
            raise StorageIO("writer is not open")
        if not 0 <= int(record_id) <= 0xFFFFFFFF:
            raise ContractViolation(f"record_id out of range: {record_id}")
        if int(record_id) in self._seen_ids:
            raise ContractViolation(f"duplicate record_id: {record_id}")

        if isinstance(signature, (bytes, bytearray)):
            sig = bytes(signature)
            if len(sig) != SIG_DIM:
                raise ContractViolation(f"signature must be {SIG_DIM} bytes, got {len(sig)}")
        else:
            sig = quantize(list(signature), self.qscale)

        raw = RECORD_STRUCT.pack(int(record_id), sig, encode_score(score), int(flags) & 0xFF)
        try:
            self._fh.write(raw)
        except OSError as exc:
            raise StorageIO(f"write failed at record {self.count}: {exc}") from exc
        self._crc = zlib.crc32(raw, self._crc)
        self._seen_ids.add(int(record_id))
        self.count += 1

    def extend(self, rows: Iterator[tuple[int, bytes, float, int]]) -> None:
        for rid, sig, score, flags in rows:
            self.add(rid, sig, score, flags)

    def finish(self) -> Path:
        """Seal the header, fsync, then atomically replace the target file."""
        if self._closed:
            return self.path
        if self._fh is None:
            raise StorageIO("writer is not open")
        header = HEADER_STRUCT.pack(
            MAGIC,
            VERSION,
            SIG_DIM,
            RECORD_SIZE,
            self.count,
            self._crc & 0xFFFFFFFF,
            self.header_flags & 0xFFFFFFFF,
            float(self.qscale),
        )
        header = header + b"\x00" * (HEADER_SIZE - len(header))
        try:
            self._fh.seek(0)
            self._fh.write(header)
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except OSError as exc:
            self._fh.close()
            self._fh = None
            self._closed = True
            self._cleanup_tmp()
            self._release_lock()
            raise StorageIO(f"cannot seal matrix: {exc}") from exc

        self._fh.close()
        self._fh = None
        self._closed = True
        try:
            os.replace(self.tmp_path, self.path)
            self._fsync_dir(self.path.parent)
        except OSError as exc:
            self._cleanup_tmp()
            raise StorageIO(f"cannot publish matrix: {exc}") from exc
        finally:
            self._release_lock()
        return self.path

    def abort(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        self._closed = True
        self._cleanup_tmp()
        self._release_lock()

    def _cleanup_tmp(self) -> None:
        try:
            self.tmp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


class MatrixIndex:
    """Read-only mmap view over a matrix file."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._fh: BinaryIO | None = None
        self._mm: mmap.mmap | None = None
        self.count = 0
        self.header_flags = 0
        self.qscale = 127.0
        self.crc32 = 0
        self._open()

    # -- loading -----------------------------------------------------------
    def _open(self) -> None:
        if not self.path.exists():
            raise MatrixMissing(f"no matrix at {self.path}")
        try:
            size = self.path.stat().st_size
        except OSError as exc:
            raise StorageIO(f"cannot stat {self.path}: {exc}") from exc
        if size < HEADER_SIZE:
            raise MatrixCorrupt("truncated-header", f"{size} < {HEADER_SIZE} bytes")

        try:
            import resource  # noqa: F401  (availability probe only)
        except ImportError:  # pragma: no cover
            pass

        try:
            self._fh = open(self.path, "rb")
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        except MemoryError as exc:  # pragma: no cover - host dependent
            raise ResourceExhausted(f"cannot map {self.path}") from exc
        except OSError as exc:
            raise StorageIO(f"cannot map {self.path}: {exc}") from exc

        try:
            self._parse_header(size)
        except Exception:
            self.close()
            raise

    def _parse_header(self, size: int) -> None:
        assert self._mm is not None
        (
            magic,
            version,
            dim,
            rec_size,
            count,
            crc,
            hflags,
            qscale,
        ) = HEADER_STRUCT.unpack(self._mm[: HEADER_STRUCT.size])

        if magic != MAGIC:
            raise MatrixCorrupt("bad-magic", repr(magic))
        if version != VERSION:
            raise MatrixCorrupt("unsupported-version", str(version))
        if dim != SIG_DIM:
            raise MatrixCorrupt("bad-dimension", f"{dim} != {SIG_DIM}")
        if rec_size != RECORD_SIZE:
            raise MatrixCorrupt("bad-record-size", f"{rec_size} != {RECORD_SIZE}")

        expected = HEADER_SIZE + count * RECORD_SIZE
        if size != expected:
            raise MatrixCorrupt("size-mismatch", f"expected {expected} bytes, file has {size}")

        self.count = count
        self.crc32 = crc
        self.header_flags = hflags
        self.qscale = qscale if qscale > 0 else 127.0

    def verify(self) -> None:
        """Recompute the body CRC. O(n) and only run on demand or after a crash."""
        assert self._mm is not None
        body = self._mm[HEADER_SIZE:]
        if (zlib.crc32(body) & 0xFFFFFFFF) != self.crc32:
            raise MatrixCorrupt("crc-mismatch", "body checksum does not match header")

    @property
    def synthetic(self) -> bool:
        return bool(self.header_flags & HF_SYNTHETIC)

    @property
    def body(self) -> memoryview:
        assert self._mm is not None
        return memoryview(self._mm)[HEADER_SIZE:]

    # -- querying ----------------------------------------------------------
    def scan(self, signature: bytes | Sequence[float], k: int = DEFAULT_K) -> list[Hit]:
        """Return up to ``k`` hits ordered by cosine, tie-broken by record id."""
        if self.count == 0 or k <= 0:
            return []
        sig = signature if isinstance(signature, (bytes, bytearray)) else quantize(list(signature), self.qscale)
        if len(sig) != SIG_DIM:
            raise ContractViolation(f"query signature must be {SIG_DIM} bytes, got {len(sig)}")

        from . import accel

        if accel.available():
            rows = accel.scan_topk(self.body, self.count, bytes(sig), k)
            return [Hit(cos, rid, decode_score(raw), flags) for cos, rid, raw, flags in rows]
        return self._scan_python(bytes(sig), k)

    def _scan_python(self, sig: bytes, k: int) -> list[Hit]:
        from .quant import dot_int8

        best: list[tuple[float, int, int, int]] = []
        for rid, rec_sig, raw, flags in RECORD_STRUCT.iter_unpack(self.body):
            cos = dot_int8(sig, rec_sig) / (self.qscale * self.qscale)
            cos = 1.0 if cos > 1.0 else (-1.0 if cos < -1.0 else cos)
            best.append((cos, rid, raw, flags))
        best.sort(key=lambda r: (-r[0], r[1]))
        return [Hit(c, rid, decode_score(raw), fl) for c, rid, raw, fl in best[:k]]

    def iter_records(self) -> Iterator[tuple[int, bytes, float, int]]:
        """Stream every record as ``(id, signature, score, flags)``."""
        for rid, rec_sig, raw, flags in RECORD_STRUCT.iter_unpack(self.body):
            yield rid, rec_sig, decode_score(raw), flags

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        if self._mm is not None:
            try:
                self._mm.close()
            except Exception:
                pass
            self._mm = None
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def __enter__(self) -> "MatrixIndex":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __len__(self) -> int:
        return self.count
