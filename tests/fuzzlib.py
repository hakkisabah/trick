"""Mutation harness for the two binary formats Trick owns.

Both formats are read from disk on a host the project does not control, and both
have exactly one contract that matters after a bad sector, a half-finished copy
or a hostile file:

* the reader either returns a usable value or raises a *declared* error - never
  an ``struct.error``, an ``IndexError``, an ``AssertionError`` or an
  ``InvalidTag`` leaking out of the loader;
* if the reader accepts the file, what it returns is self-consistent (declared
  record count == bytes on disk, every scan result finite, at most ``k`` rows).

The same harness is used twice on purpose: the pytest suite runs a small fixed
number of cases so a regression is caught on every commit, and
``scripts/fuzz_binaries.py`` runs a much larger deterministic sweep as a gate
step. Sharing the code is what makes the two runs comparable.
"""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Callable, Iterator

from trick.errors import MatrixCorrupt, MatrixMissing
from trick.matrix import HEADER_SIZE, RECORD_SIZE, MatrixIndex, MatrixWriter
from trick.quant import SIG_DIM, quantize
from trick.vault import VaultError, open_sealed, seal

# The complete set of errors the matrix loader is allowed to raise for a bad
# file. Anything else escaping is a defect in the loader, not in the file.
MATRIX_ERRORS = (MatrixCorrupt, MatrixMissing)
# Errors the archive reader is allowed to raise for a bad file.
ARCHIVE_ERRORS = (VaultError,)


def build_matrix(path: Path, records: int = 24) -> Path:
    """A small, valid matrix with mixed signatures and scores."""
    with MatrixWriter(path) as writer:
        for i in range(records):
            vec = [0.0] * SIG_DIM
            vec[i % SIG_DIM] = 1.0
            vec[(i * 7) % SIG_DIM] += 0.25
            writer.add(i + 1, vec, (i % 100) / 100.0, i % 3)
    return path


def matrix_cases(base: bytes, rng: random.Random, count: int) -> Iterator[tuple[str, bytes]]:
    """Deterministic mutations of a valid matrix file."""
    size = len(base)
    for n in range(count):
        kind = n % 6
        data = bytearray(base)
        if kind == 0:  # single bit flip in the header
            pos = rng.randrange(0, HEADER_SIZE)
            data[pos] ^= 1 << rng.randrange(0, 8)
            yield f"header-bit-{pos}", bytes(data)
        elif kind == 1:  # single bit flip in the body
            pos = rng.randrange(HEADER_SIZE, size)
            data[pos] ^= 1 << rng.randrange(0, 8)
            yield f"body-bit-{pos}", bytes(data)
        elif kind == 2:  # truncation anywhere, including inside the header
            cut = rng.randrange(0, size)
            yield f"truncate-{cut}", base[:cut]
        elif kind == 3:  # trailing bytes
            extra = bytes(rng.getrandbits(8) for _ in range(rng.randrange(1, RECORD_SIZE + 2)))
            yield f"append-{len(extra)}", base + extra
        elif kind == 4:  # zeroed run, the shape a half-written file takes
            start = rng.randrange(0, max(1, size - 1))
            length = rng.randrange(1, min(64, size - start) + 1)
            data[start : start + length] = b"\x00" * length
            yield f"zero-{start}-{length}", bytes(data)
        else:  # whole header replaced by noise
            noise = bytes(rng.getrandbits(8) for _ in range(HEADER_SIZE))
            yield f"header-noise-{n}", noise + base[HEADER_SIZE:]


def check_matrix_bytes(path: Path, signature: bytes, k: int = 12) -> str:
    """Load, verify and scan ``path``; return 'loaded' or 'rejected'.

    Raises ``AssertionError`` when the loader misbehaves. Both directions are
    checked: a declared error is a pass, and an accepted file must be usable.
    """
    try:
        index = MatrixIndex(path)
    except MATRIX_ERRORS:
        return "rejected"
    except Exception as exc:  # noqa: BLE001 - the point of the test
        raise AssertionError(f"{path.name}: undeclared {type(exc).__name__}: {exc}") from exc

    try:
        assert len(index) == index.count, "declared count disagrees with len()"
        body = index.body
        assert len(body) == index.count * RECORD_SIZE, (
            f"body is {len(body)} bytes for {index.count} records"
        )
        try:
            index.verify()
        except MatrixCorrupt as exc:
            assert exc.reason == "crc-mismatch", exc.reason
        hits = index.scan(signature, k=k)
        assert len(hits) <= k, f"scan returned {len(hits)} rows for k={k}"
        for hit in hits:
            assert math.isfinite(hit.cosine), f"non-finite cosine {hit.cosine!r}"
            assert math.isfinite(hit.score), f"non-finite score {hit.score!r}"
        return "loaded"
    finally:
        index.close()


def archive_cases(
    base: bytes,
    rng: random.Random,
    count: int,
    *,
    magic_len: int = 8,
    salt_len: int = 16,
    nonce_len: int = 12,
) -> Iterator[tuple[str, bytes]]:
    """Deterministic mutations of a sealed archive.

    The offsets are parameters rather than literals so this keeps working if the
    archive header changes size; the defaults are the current layout
    (``MAGIC | salt | nonce | ciphertext | tag``).
    """
    size = len(base)
    salt_start = magic_len
    nonce_start = salt_start + salt_len
    cipher_start = nonce_start + nonce_len
    for n in range(count):
        kind = n % 6
        data = bytearray(base)
        if kind == 0:  # bit flip in the salt
            pos = rng.randrange(salt_start, nonce_start)
            data[pos] ^= 1 << rng.randrange(0, 8)
            yield f"salt-bit-{pos}", bytes(data)
        elif kind == 1:  # bit flip in the nonce
            pos = rng.randrange(nonce_start, cipher_start)
            data[pos] ^= 1 << rng.randrange(0, 8)
            yield f"nonce-bit-{pos}", bytes(data)
        elif kind == 2:  # bit flip in the ciphertext or the tag
            pos = rng.randrange(cipher_start, size)
            data[pos] ^= 1 << rng.randrange(0, 8)
            yield f"cipher-bit-{pos}", bytes(data)
        elif kind == 3:  # truncation
            cut = rng.randrange(0, size)
            yield f"truncate-{cut}", base[:cut]
        elif kind == 4:  # trailing bytes
            yield f"append-{n}", base + bytes(rng.getrandbits(8) for _ in range(8))
        else:  # different magic entirely
            yield f"magic-{n}", b"NOTTRICK" + base[magic_len:]


def check_archive_bytes(blob: bytes, passphrase: str) -> str:
    """Open ``blob``; return 'opened' or 'rejected', never leak another error."""
    try:
        payload = open_sealed(blob, passphrase)
    except ARCHIVE_ERRORS:
        return "rejected"
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"undeclared {type(exc).__name__}: {exc}") from exc
    assert isinstance(payload, bytes) and payload, "an opened archive must carry bytes"
    return "opened"


def sweep(
    writer: Callable[[Path, bytes], None],
    checker: Callable[[Path], str],
    cases: Iterator[tuple[str, bytes]],
    directory: Path,
) -> dict[str, int]:
    """Write each case to ``directory`` and hand it to ``checker``."""
    directory.mkdir(parents=True, exist_ok=True)
    tally = {"loaded": 0, "rejected": 0, "opened": 0}
    for name, blob in cases:
        path = directory / name.replace("/", "_")
        writer(path, blob)
        outcome = checker(path)
        tally[outcome] = tally.get(outcome, 0) + 1
    return tally


def sealed_sample(passphrase: str = "correct horse battery staple") -> bytes:
    return seal(b'{"format": "TRICKV1", "decisions": []}', passphrase)


def quantise_sample() -> bytes:
    return quantize([1.0] + [0.0] * (SIG_DIM - 1))
