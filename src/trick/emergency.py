"""Embedded emergency matrix.

When the real matrix is missing, corrupt or unmappable, Trick must still answer.
The fallback is a tiny catalogue of genuinely existing scientific and
engineering sources, compiled into the package (roughly 1.5 KB of records) so it
survives a wiped data directory, a broken download or a read-only filesystem.

Honesty rules baked into the data:

* every seeded record carries ``RF_SYNTHETIC`` because its signature comes from
  the deterministic hash encoder, not from a real embedding model;
* every record is *unscored*: ``RF_SCORED`` is not set and the score is 0.0, so
  the UI can label these rows as "catalogue suggestions, not scored";
* the header carries ``HF_SYNTHETIC`` so a whole-file check is one flag test.

This is a seed catalogue, not a search engine: 64 curated sources are enough to
keep the tool useful while the real index is rebuilt.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from pathlib import Path

from .errors import TrickError
from .matrix import (
    HF_SYNTHETIC,
    MatrixIndex,
    MatrixWriter,
    RF_SYNTHETIC,
)
from .quant import feature_hash_signature

# source|name|one-line description
SEED_CATALOGUE: tuple[str, ...] = (
    "cran|MASS|Support functions and datasets for Venables and Ripley statistics",
    "cran|lme4|Linear mixed-effects models using Eigen and S4",
    "cran|ggplot2|Grammar of graphics plotting system",
    "cran|data.table|Fast aggregation and joins for large tabular data",
    "cran|sf|Simple features for spatial vector data",
    "cran|Rcpp|Seamless C++ integration for R",
    "cran|survival|Survival analysis including Cox and Kaplan-Meier models",
    "cran|forecast|Time series forecasting with ARIMA and ETS",
    "cran|Matrix|Sparse and dense matrix classes and methods",
    "cran|nlme|Linear and nonlinear mixed-effects models",
    "cran|caret|Classification and regression training harness",
    "cran|rstan|R interface to the Stan probabilistic programming language",
    "cran|igraph|Network analysis and graph algorithms",
    "cran|quantmod|Quantitative financial modelling framework",
    "cran|ROCR|Visualising classifier performance curves",
    "cran|boot|Bootstrap resampling functions",
    "bioconductor|DESeq2|Differential gene expression by negative binomial GLM",
    "bioconductor|edgeR|Empirical Bayes analysis of RNA-seq count data",
    "bioconductor|limma|Linear models for microarray and RNA-seq data",
    "bioconductor|GenomicRanges|Range-based genomic data structures",
    "bioconductor|Biostrings|String containers for biological sequences",
    "bioconductor|SingleCellExperiment|Single-cell experiment data container",
    "bioconductor|SummarizedExperiment|Matrix-like assay containers with metadata",
    "bioconductor|ComplexHeatmap|Composable heatmaps for multi-omics data",
    "bioconductor|ShortRead|FASTQ and FASTA reading and quality assessment",
    "bioconductor|rtracklayer|Import and export of genome track formats",
    "bioconductor|Rsamtools|Low-level BAM and SAM file access",
    "bioconductor|VariantAnnotation|Annotation of genetic variants",
    "bioconductor|AnnotationHub|Query of online annotation resources",
    "bioconductor|BiocParallel|Parallel evaluation for R bioinformatics",
    "arxiv|stat.ML|Machine learning statistics category",
    "arxiv|stat.CO|Computation statistics category",
    "arxiv|stat.ME|Methodology statistics category",
    "arxiv|cs.LG|Machine learning computer science category",
    "arxiv|math.NA|Numerical analysis category",
    "arxiv|math.OC|Optimisation and control category",
    "arxiv|physics.comp-ph|Computational physics category",
    "arxiv|q-bio.GN|Genomics quantitative biology category",
    "arxiv|eess.SP|Signal processing category",
    "arxiv|cond-mat.mtrl-sci|Materials science category",
    "pypi|numpy|Array programming primitives for Python",
    "pypi|scipy|Scientific algorithms: optimisation, integration, signal",
    "pypi|pandas|Labelled data frames and time series",
    "pypi|scikit-learn|Classical machine learning toolkit",
    "pypi|statsmodels|Statistical models and hypothesis tests",
    "pypi|sympy|Symbolic mathematics",
    "pypi|networkx|Graph creation and graph algorithms",
    "pypi|pymc|Bayesian modelling with probabilistic programming",
    "pypi|xarray|Labeled multi-dimensional arrays",
    "pypi|astropy|Astronomy and astrophysics core library",
    "pypi|biopython|Biological computation toolkit",
    "pypi|matplotlib|Publication-quality plotting library",
    "pypi|numba|JIT compilation for numerical Python",
    "pypi|pyomo|Algebraic modelling of optimisation problems",
    "julia|DifferentialEquations.jl|Numerical differential equation solvers",
    "julia|JuMP.jl|Algebraic modelling language for optimisation",
    "julia|Distributions.jl|Probability distributions and statistics",
    "julia|Flux.jl|Machine learning framework",
    "julia|Turing.jl|Bayesian inference and probabilistic programming",
    "julia|Plots.jl|Unified plotting interface",
    "github|tensorflow/tensorflow|End-to-end machine learning platform",
    "github|pytorch/pytorch|Tensors and dynamic neural networks",
    "github|openai/gym|Reinforcement learning environments toolkit",
    "github|laya-engine/laya|Typed single-pass decision scoring engine",
)


@dataclass(frozen=True)
class LoadResult:
    """Outcome of :func:`open_or_emergency`."""

    index: MatrixIndex
    source: str  # "file" or "emergency"
    error: TrickError | None
    emergency_path: Path | None

    @property
    def degraded(self) -> bool:
        return self.source != "file"


def seed_records() -> list[tuple[int, bytes, float, int]]:
    """Deterministic ``(id, signature, score, flags)`` rows for the seed catalogue."""
    rows: list[tuple[int, bytes, float, int]] = []
    for i, line in enumerate(SEED_CATALOGUE):
        sig = feature_hash_signature(line)
        rows.append((i + 1, sig, 0.0, RF_SYNTHETIC))
    return rows


def build_emergency_matrix(path: str | Path) -> Path:
    """Write the embedded catalogue to ``path`` atomically and return the path."""
    path = Path(path)
    rows = seed_records()
    with MatrixWriter(path, header_flags=HF_SYNTHETIC) as writer:
        for row in rows:
            writer.add(*row)
    return path


def open_or_emergency(path: str | Path, emergency_path: str | Path | None = None) -> LoadResult:
    """Open the real matrix, falling back to the embedded catalogue on failure.

    Never raises for the expected failure modes (S1 missing, S2 corrupt, S3
    resource, S6 storage): the caller gets a degraded but usable index plus the
    error that caused it.
    """
    path = Path(path)
    try:
        return LoadResult(index=MatrixIndex(path), source="file", error=None, emergency_path=None)
    except TrickError as exc:
        err: TrickError = exc

    target = Path(emergency_path) if emergency_path is not None else path.with_name("emergency.trk")
    try:
        if not target.exists():
            build_emergency_matrix(target)
        return LoadResult(index=MatrixIndex(target), source="emergency", error=err, emergency_path=target)
    except TrickError as exc:  # pragma: no cover - requires an unwritable directory
        raise exc from err


def catalogue_text(record_id: int) -> str | None:
    """Return the seed line for a record id (1-based), or ``None``."""
    if 1 <= record_id <= len(SEED_CATALOGUE):
        return SEED_CATALOGUE[record_id - 1]
    return None


def catalogue_digest() -> str:
    """Short stable digest of the embedded catalogue, used in diagnostics."""
    joined = "\n".join(SEED_CATALOGUE).encode("utf-8")
    return f"{zlib.crc32(joined):08x}"


__all__ = [
    "SEED_CATALOGUE",
    "LoadResult",
    "build_emergency_matrix",
    "catalogue_digest",
    "catalogue_text",
    "open_or_emergency",
    "seed_records",
]
