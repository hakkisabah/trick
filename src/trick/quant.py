"""Int8 signature quantisation.

The index stores a 16 byte signature per record instead of the full float
embedding. Records are unit-normalised before quantisation, so the int8 dot
product divided by ``qscale**2`` approximates cosine similarity without a
per-record scale factor. That keeps the record at a fixed 24 bytes.

Why 16 dimensions: the target user is the cheapest host in the fleet. Sixteen
int8 values cost 16 bytes per record (24 with the fixed header fields), which is
24 MB per million records. The recall loss is recovered by the two-stage search
(signature prefilter, then BM25/FTS5 rescoring), not by spending RAM.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

SIG_DIM = 16
QSCALE = 127.0
SCORE_MAX = 0xFFFF


def quantize(vector: Sequence[float], qscale: float = QSCALE) -> bytes:
    """L2-normalise ``vector`` and squash it into ``SIG_DIM`` signed bytes.

    Raises ``ContractViolation`` when the input has the wrong length.
    """
    from .errors import ContractViolation

    if len(vector) != SIG_DIM:
        raise ContractViolation(f"signature must have {SIG_DIM} components, got {len(vector)}")

    norm = math.sqrt(sum(float(x) * float(x) for x in vector))
    if norm == 0.0:
        return bytes(SIG_DIM)

    out = bytearray(SIG_DIM)
    for i, x in enumerate(vector):
        v = int(round((float(x) / norm) * qscale))
        v = 127 if v > 127 else (-127 if v < -127 else v)
        out[i] = v & 0xFF
    return bytes(out)


def dequantize(sig: bytes) -> list[float]:
    """Return the approximate unit vector behind a stored signature."""
    if len(sig) != SIG_DIM:
        from .errors import ContractViolation

        raise ContractViolation(f"signature must be {SIG_DIM} bytes, got {len(sig)}")
    return [((b + 128) % 256 - 128) / QSCALE for b in sig]


def dot_int8(a: bytes, b: bytes) -> int:
    """Signed dot product of two 16 byte signatures (pure Python, no numpy)."""
    total = 0
    for i in range(SIG_DIM):
        x = a[i]
        y = b[i]
        total += (x - 256 if x > 127 else x) * (y - 256 if y > 127 else y)
    return total


def cosine_from_dot(dot: int, qscale: float = QSCALE) -> float:
    """Map an int8 dot product back to an approximate cosine in [-1, 1]."""
    c = dot / (qscale * qscale)
    return 1.0 if c > 1.0 else (-1.0 if c < -1.0 else c)


def encode_score(value: float) -> int:
    """Encode a 0..1 score as an unsigned 16 bit integer."""
    if value != value:  # NaN
        return 0
    v = int(round(float(value) * SCORE_MAX))
    return 0 if v < 0 else (SCORE_MAX if v > SCORE_MAX else v)


def decode_score(raw: int) -> float:
    """Decode an unsigned 16 bit integer back to a 0..1 score."""
    return min(max(int(raw), 0), SCORE_MAX) / SCORE_MAX


def feature_hash_signature(text: str, dim: int = SIG_DIM) -> bytes:
    """Deterministic signature for text when no embedding model is available.

    Stable across processes and runs (CRC32, not ``hash()``), which is what the
    emergency matrix and the test fixtures rely on. It is a placeholder encoder:
    records produced with it are always flagged synthetic.
    """
    import zlib

    acc = [0.0] * dim
    for token in text.lower().replace("|", " ").split():
        h = zlib.crc32(token.encode("utf-8"))
        idx = h % dim
        sign = 1.0 if (h >> 16) & 1 else -1.0
        acc[idx] += sign
    return quantize(acc)


def iter_tokens(text: str) -> Iterable[str]:
    """Token iterator shared by the placeholder encoder and the FTS layer."""
    return (t for t in text.lower().split() if t)
