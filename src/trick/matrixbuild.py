"""Build the int8 score matrix from the document store.

The matrix is not a second search engine. It is the remembered-scores channel
that requirement three of the product asks for: open the app, have a numeric
matrix on disk, memory-map it, and answer without reading documents. It also
survives the case where the text index is unavailable (S1), because a signature
scan needs neither FTS5 nor SQLite.

What it is *not*: a semantic index. With no embedding model on the target host,
signatures come from :func:`trick.quant.feature_hash_signature`, a hashed
bag-of-words. That encoder cannot reach a document whose words differ from the
query, so the matrix is a candidate channel and a fallback, and every record it
writes is flagged ``RF_SYNTHETIC`` so nothing downstream can present it as model
output. Ranking quality is measured; the matrix is not allowed to change it
silently (see ``scripts/eval_expansion.py`` and the ranking harness).

Cost: 24 bytes per document. Twenty-five thousand packages cost 600 KB, one
million documents cost 24 MB, which is why the signature is 16 dimensions and
not 384.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

from .matrix import HF_SYNTHETIC, RF_SYNTHETIC, MatrixWriter
from .quant import feature_hash_signature

PRIOR_CHUNK = 512


def _priors(store, doc_ids: list[int]) -> dict[int, float]:
    out: dict[int, float] = {}
    for start in range(0, len(doc_ids), PRIOR_CHUNK):
        out.update(store.weights_for(doc_ids[start : start + PRIOR_CHUNK]))
    return out


def rows_from_store(store, *, limit: int = 0) -> Iterable[tuple[int, bytes, float, int]]:
    """Yield ``(doc_id, signature, score, flags)`` for every document."""

    def generate() -> Iterable[tuple[int, bytes, float, int]]:
        batch: list = []
        for doc in store.iter_docs(limit=limit or None):
            batch.append(doc)
            if len(batch) >= PRIOR_CHUNK:
                priors = _priors(store, [item.doc_id for item in batch])
                for item in batch:
                    yield _row(item, priors)
                batch = []
        if batch:
            priors = _priors(store, [item.doc_id for item in batch])
            for item in batch:
                yield _row(item, priors)

    return generate()


def _row(doc, priors: dict[int, float]) -> tuple[int, bytes, float, int]:
    text = f"{doc.title} {doc.summary}".strip()
    signature = feature_hash_signature(text)
    score = priors.get(doc.doc_id, 0.0)
    return (doc.doc_id, signature, score, RF_SYNTHETIC)


def build_from_store(store, path: str | Path, *, limit: int = 0) -> dict:
    """Write a matrix for the whole store and publish it atomically."""
    target = Path(path)
    started = time.monotonic()
    # The header flag says the whole file came from the placeholder encoder, so a
    # reader cannot mistake it for embedding output.
    writer = MatrixWriter(target, header_flags=HF_SYNTHETIC)
    with writer:
        for row in rows_from_store(store, limit=limit):
            writer.add(*row)
    elapsed = time.monotonic() - started
    size = target.stat().st_size if target.exists() else 0
    return {
        "path": str(target),
        "records": writer.count,
        "bytes": size,
        "seconds": round(elapsed, 3),
        "synthetic": True,
    }
