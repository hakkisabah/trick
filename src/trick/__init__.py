"""Trick core: scored retrieval over scientific and engineering sources.

This package holds the storage and scoring primitives. Network adapters, the
laya client and the UI layers are added in later phases and all talk to the
interfaces defined here.
"""

from __future__ import annotations

from .emergency import LoadResult, build_emergency_matrix, open_or_emergency
from .errors import (
    ConfigError,
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
from .laya import LayaClient, MockScorer, Ranked, build_scorer, label_order
from .matrix import DEFAULT_K, Hit, MatrixIndex, MatrixWriter
from .quant import SIG_DIM, decode_score, encode_score, feature_hash_signature, quantize
from .retrieval import SourceDoc, live_adapters, search_live
from .search import ResultRow, SearchEngine, SearchOutcome, build_engine
from .textindex import DocumentStore

__version__ = "0.2.0"

__all__ = [
    "ConfigError",
    "Contention",
    "ContractViolation",
    "DEFAULT_K",
    "DocumentStore",
    "Hit",
    "LayaClient",
    "LoadResult",
    "MatrixCorrupt",
    "MatrixIndex",
    "MatrixMissing",
    "MatrixWriter",
    "MockScorer",
    "Ranked",
    "ResourceExhausted",
    "ResultRow",
    "SIG_DIM",
    "ScorerUnavailable",
    "SearchEngine",
    "SearchOutcome",
    "SourceDoc",
    "SourceUnavailable",
    "StorageIO",
    "TrickError",
    "build_emergency_matrix",
    "build_engine",
    "build_scorer",
    "decode_score",
    "encode_score",
    "feature_hash_signature",
    "label_order",
    "live_adapters",
    "open_or_emergency",
    "quantize",
    "search_live",
    "__version__",
]
