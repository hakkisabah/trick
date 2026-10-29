"""Optional numpy accelerator for signature scans.

The runtime works without numpy (minimal mode, pure Python scan). When numpy is
importable the scan becomes a single fused matrix-vector product over the memory
map, which is what makes 1M+ records interactive on a cheap host.

The record layout is declared once, in :mod:`trick.matrix`, and mirrored here so
that the accelerator cannot drift from the on-disk format.
"""

from __future__ import annotations

from typing import Any

from .matrix import HEADER_SIZE, RECORD_SIZE, SIG_DIM
from .quant import QSCALE

_np: Any = None
_checked = False

RECORD_DTYPE_FIELDS = [
    ("id", "<u4"),
    ("sig", "i1", (SIG_DIM,)),
    ("score", "<u2"),
    ("flags", "u1"),
    ("_pad", "u1"),
]


def numpy_module() -> Any:
    """Return the numpy module, or ``None`` when it is not installed."""
    global _np, _checked
    if not _checked:
        _checked = True
        try:  # pragma: no cover - depends on the host
            import numpy  # type: ignore

            _np = numpy
        except Exception:
            _np = None
    return _np


def available() -> bool:
    return numpy_module() is not None


def record_dtype() -> Any:
    np = numpy_module()
    if np is None:
        return None
    dt = np.dtype(RECORD_DTYPE_FIELDS)
    if dt.itemsize != RECORD_SIZE:  # pragma: no cover - guards numpy drift
        raise RuntimeError(f"accelerator dtype is {dt.itemsize} bytes, format is {RECORD_SIZE}")
    return dt


def scan_topk(body: memoryview, count: int, query_sig: bytes, k: int) -> list[tuple[float, int, int, int]]:
    """Return ``k`` best ``(cosine, record_id, raw_score, flags)`` tuples.

    ``body`` must start at the first record (offset ``HEADER_SIZE``).
    """
    np = numpy_module()
    if np is None:
        raise RuntimeError("numpy accelerator unavailable")

    records = np.frombuffer(body, dtype=record_dtype(), count=count)
    sigs = records["sig"].astype(np.float32)
    query = np.frombuffer(query_sig, dtype=np.int8).astype(np.float32)
    dot = sigs @ query
    cos = dot / np.float32(QSCALE * QSCALE)
    np.clip(cos, -1.0, 1.0, out=cos)

    take = min(k, count)
    if take <= 0:
        return []
    if take < count:
        part = np.argpartition(-cos, take - 1)[:take]
    else:
        part = np.arange(count)
    order = sorted(
        (int(i) for i in part),
        key=lambda i: (-float(cos[i]), int(records["id"][i])),
    )
    return [
        (
            float(cos[i]),
            int(records["id"][i]),
            int(records["score"][i]),
            int(records["flags"][i]),
        )
        for i in order
    ]


__all__ = ["available", "numpy_module", "record_dtype", "scan_topk", "HEADER_SIZE"]
