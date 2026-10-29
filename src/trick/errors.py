"""Trick error taxonomy.

Every failure mode in the design is mapped to a stable ``S<n>`` code so that
tests, logs and the user-facing UI can talk about the same thing. The codes are
contractual: do not renumber them.

S0  ok                     no failure
S1  matrix-missing         the score matrix file does not exist yet
S2  matrix-corrupt         magic, version, size, CRC or record mismatch
S3  resource-exhausted     not enough RAM to map or scan
S4  source-unavailable     an upstream catalogue did not answer in budget
S5  scorer-unavailable     laya endpoint refused, timed out or rejected the key
S6  storage-io             disk full, permission denied, read-only filesystem
S7  contention             another writer holds the matrix lock
S8  contract-violation     caller passed data that breaks the file contract
S9  config                 credentials or settings are missing or unsafe

Each exception carries ``s_code`` and ``degraded_action`` so a caller can decide
what to do without string matching.
"""

from __future__ import annotations

from typing import ClassVar


class TrickError(Exception):
    """Base class for every error Trick raises on a non-recoverable path."""

    s_code: ClassVar[str] = "S0"
    degraded_action: ClassVar[str] = "none"


class MatrixMissing(TrickError):
    """The matrix file has not been built yet. Load the emergency matrix."""

    s_code = "S1"
    degraded_action = "load-emergency-matrix"


class MatrixCorrupt(TrickError):
    """The matrix is present but does not satisfy the file contract."""

    s_code = "S2"
    degraded_action = "load-emergency-matrix"

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"matrix corrupt ({reason})" + (f": {detail}" if detail else ""))


class ResourceExhausted(TrickError):
    """The host cannot hold the index. Fall back to the minimal text-only mode."""

    s_code = "S3"
    degraded_action = "minimal-mode-text-only"


class SourceUnavailable(TrickError):
    """A catalogue adapter failed. Retrieval continues with the remaining sources."""

    s_code = "S4"
    degraded_action = "skip-source"


class ScorerUnavailable(TrickError):
    """The laya scorer is unreachable. Results fall back to retrieval order."""

    s_code = "S5"
    degraded_action = "retrieval-order-only"

    def __init__(self, reason: str = "unreachable", detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"scorer unavailable ({reason})" + (f": {detail}" if detail else ""))


class StorageIO(TrickError):
    """A write or read failed below the file contract (disk, permissions)."""

    s_code = "S6"
    degraded_action = "keep-previous-matrix"


class Contention(TrickError):
    """Another process holds the exclusive writer lock."""

    s_code = "S7"
    degraded_action = "retry-later"


class ContractViolation(TrickError, ValueError):
    """The caller violated the matrix contract (bad id, dimension, flags)."""

    s_code = "S8"
    degraded_action = "reject-input"


class ConfigError(TrickError, RuntimeError):
    """Credentials or settings are missing, malformed or unsafe."""

    s_code = "S9"
    degraded_action = "run-offline"
