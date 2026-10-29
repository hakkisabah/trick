# Trick

Scored retrieval for scientific and engineering sources, driven by
[laya](https://github.com/NandhaKishorM/laya).

Trick is a small tool with one job: you type a question (terminal, Open WebUI,
or any MCP client), it fetches candidate sources from scientific catalogues, then
ranks them with laya's typed decision scoring. The local build is free, offline
capable, and never sends your text anywhere except the sources and the scorer you
configured.

## Status

Phase 1 of 7 is complete: the storage and scoring-matrix core. Retrieval
adapters, the laya client, the terminal UI, the MCP server and the Open WebUI
integration follow. Nothing here talks to the network yet.

## Why two layers

laya is a single-pass decision engine, not a web crawler. It scores a small,
already-narrowed candidate set well and scores a raw catalogue badly, so Trick
always runs the same pipeline:

```
query
  -> adapters (CRAN, Bioconductor, arXiv, PyPI, Zenodo, OpenAlex, GitHub)
  -> 60-200 candidates
  -> int8 signature prefilter  -> 12 candidates
  -> laya choice question (labels A..L)
  -> ranking (0.7 laya + 0.3 retrieval)
  -> scores written back to the matrix so a repeat query never calls laya again
```

## The matrix

One file, fixed-size records, memory mapped read-only. Nothing is resident until
a scan touches those pages.

```
48 byte header   magic | version | dim | record size | count | crc32 | flags | qscale
24 byte record   id u32 | signature 16 x i8 | score u16 | flags u8 | pad
```

* 1M records = 24 MB on disk (measured: 24,000,048 bytes)
* cold open is O(1): measured 0.068 ms median for 1M records
* one top-12 scan: measured 50.7 ms p50 / 55.8 ms p95 with numpy, 5.5 s without
  numpy (minimal mode, keep the index under ~150k records there)
* crash safe: written to `.trk.tmp`, `fsync`ed, published with one `rename`
* one writer at a time via `flock`; a second writer gets `Contention`
* every malformed file is classified (`MatrixCorrupt` with a stable reason:
  `truncated-header`, `bad-magic`, `unsupported-version`, `bad-dimension`,
  `bad-record-size`, `size-mismatch`, `crc-mismatch`) and falls back to the
  embedded emergency catalogue instead of failing

### Emergency matrix

A 64-entry catalogue of real scientific sources is compiled into the package
(under 4 KB of records). If the real matrix is missing, corrupt or unmappable,
Trick still answers from it. Those rows are always flagged synthetic and
unscored: the UI must present them as catalogue suggestions, never as scored
results.

## Layout

```
src/trick/errors.py      stable S0..S8 taxonomy, each with a degraded action
src/trick/quant.py       unit-vector int8 signature quantisation
src/trick/matrix.py      file format, atomic writer, mmap reader, scan
src/trick/accel.py       optional numpy scan (stdlib fallback is authoritative)
src/trick/emergency.py   embedded seed catalogue and fallback loader
tests/                   layout, roundtrip, corruption, contention, budget
scripts/bench.py         produced numbers (not estimates) for this host
scripts/check.sh         repository gate: tests, layout, language, secrets
```

## Install and verify

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'      # '.[accel]' adds numpy
.venv/bin/python -m pytest -q
sh scripts/check.sh
.venv/bin/python scripts/bench.py --json /tmp/bench.json
```

`numpy` is optional at runtime. Without it Trick uses the pure-Python scan, which
is correct but slower; the test suite runs both paths and asserts identical
rankings.

## Configuration

Nothing to configure in phase 1. Later phases read `TRICK_LAYA_URL`,
`TRICK_LAYA_API_KEY`, `TRICK_DATA_DIR` and `TRICK_MODE` (`minimal`, `local`,
`cloud`) from the environment or `~/.config/trick/config.toml`.
