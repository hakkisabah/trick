"""Trick core: scored retrieval over scientific and engineering sources.

This package holds the storage and scoring primitives. Network adapters, the
laya client and the UI layers are added in later phases and all talk to the
interfaces defined here.
"""

from __future__ import annotations

from .emergency import LoadResult, build_emergency_matrix, open_or_emergency
from .errors import (
    Contention,
    ContractViolation,
    MatrixCorrupt,
    MatrixMissing,
    ResourceExhausted,
    ScorerUnavailable,
    SourceUnavailable,
    StorageIO,
    TrickError,
)
from .matrix import DEFAULT_K, Hit, MatrixIndex, MatrixWriter
from .quant import SIG_DIM, decode_score, encode_score, feature_hash_signature, quantize

__version__ = "0.1.0"

__all__ = [
    "Contention",
    "ContractViolation",
    "DEFAULT_K",
    "Hit",
    "LoadResult",
    "MatrixCorrupt",
    "MatrixIndex",
    "MatrixMissing",
    "MatrixWriter",
    "ResourceExhausted",
    "SIG_DIM",
    "ScorerUnavailable",
    "SourceUnavailable",
    "StorageIO",
    "TrickError",
    "build_emergency_matrix",
    "decode_score",
    "encode_score",
    "feature_hash_signature",
    "open_or_emergency",
    "quantize",
    "__version__",
]
