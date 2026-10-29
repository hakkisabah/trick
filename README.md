# Trick

Scored retrieval for scientific and engineering sources, driven by
[laya](https://github.com/NandhaKishorM/laya).

Trick is a small tool with one job: you type a question (terminal, Open WebUI,
or any MCP client), it fetches candidate sources from scientific catalogues, then
ranks them with laya's typed decision scoring. The local build is free, offline
capable, and never sends your text anywhere except the sources and the scorer you
configured.

## Status

Phase 3 of 7 is complete: the storage core, the retrieval adapters, the laya
client and the ranking pipeline, plus every surface that talks to them - a
terminal UI, an MCP server, a unix-socket core service and a loopback HTTP
bridge. Open WebUI and the Gemini extension sit on the same server in phase 4.

Measured on 22 gold queries with the live model and live sources, no re-ranking
shortcuts (see `scripts/eval_ranking.py`):

```
                     recall@12   mrr@10   ndcg@10
laya alone              0.682     0.326    0.414
retrieval only          0.955     0.788    0.829
blend (shipped)         0.955     0.779    0.823

canonical set (12)      0.917     0.764    0.803
niche set (10)          1.000     0.798    0.846
```

End-to-end latency with live sources and one laya call: p50 1175 ms, min 810 ms,
max 1953 ms. The confidence gate skipped the model on 6 of 22 queries.

## Surfaces

One engine, four doors. They cannot disagree, because they share one process and
one ranking path:

```sh
trick tui                     # terminal session, Enter submits, arrows, history
trick serve                   # core service: unix socket + loopback HTTP bridge
trick mcp                     # MCP server on stdio (Gemini CLI, Open WebUI, Claude)
trick query "..." --json      # one shot, scriptable
```

The core service exists so a plugin does not need its own SQLite connection and
its own copy of the ranking code:

```
plugin / CLI / MCP host
        |
        +-- unix socket  $TRICK_HOME/run/trick.sock   owner-only (0600)
        +-- http bridge  127.0.0.1:8765/v1/search     bearer token from a 0600 file
                              |
                        trick serve  (one engine, one thread, one worker queue)
```

Why the worker queue: a `sqlite3.Connection` is bound to the thread that created
it, and an mmap has no lock of its own. Requests are queued to a single engine
thread, which is also why 12 concurrent searches over HTTP answer in 570 ms
without a single cross-thread error.

Security defaults, each one deliberate: the socket file is 0600 in a 0700
directory; the bridge token is 32 random bytes compared with `hmac.compare_digest`
and never logged; the bridge refuses any host that is not loopback; request
bodies are capped at 32 KB; CORS is off unless `TRICK_CORS_ORIGINS` names exact
origins, because a wildcard would let any page in the user's browser spend their
laya credits.

### MCP tools

| Tool | Arguments | Returns |
| --- | --- | --- |
| `trick_search` | `query`, `limit` 1-26, `live`, `verbose` | text summary + `structuredContent` with every row, both score components and the `S<n>` codes |
| `trick_health` | none | store, matrix, scorer and service state |

A tool failure is a result (`isError: true` plus the `S<n>` code), never a dead
session: the model can tell the user "laya did not answer" instead of hanging.
stdout carries JSON-RPC and nothing else; every diagnostic goes to stderr, and
the repository gate asserts it.


## Why two layers

laya is a single-pass decision engine, not a web crawler. It scores a small,
already-narrowed candidate set well and scores a raw catalogue badly, so Trick
always runs the same pipeline:

```
query
  -> live adapters (arXiv, OpenAlex, Crossref), per-source budget   S4: skipped
  -> local FTS5/BM25 over bulk catalogues (CRAN, Bioconductor)
  -> int8 signature prefilter (matrix)                              S1/S2/S3: skipped
  -> BM25 rescore + canonicality prior -> shortlist of 12
  -> confidence gate: skip the scorer when top-1 clearly leads
  -> laya choice question over opaque labels A..L                   S5: retrieval order
  -> ranking: 0.2 laya + 0.8 retrieval
  -> decision cache, so an identical query never calls laya twice
```

### What the measurements changed

Four parts of the design were decided by numbers, not taste:

* **The canonicality prior is not optional.** Raw BM25 ranks short
  keyword-dense packages above established tools: `ggplot2` came 15th, `forecast`
  26th, `survival` 35th for their own canonical queries. Adding an ingested prior
  (CRAN monthly downloads, Bioconductor reverse dependencies) lifted recall@12
  from 0.333 to 0.917 on the canonical set.
* **laya is a tie-breaker, not the ranker.** Given the same 12 candidates, laya
  alone reaches nDCG@10 0.414 while retrieval reaches 0.829. The shipped blend is
  0.2 model / 0.8 retrieval: at 0.3 the canonical set scored 0.797, at 0.2 it
  scored 0.803, and below 0.2 the curve is flat - so the model keeps a real share
  without being trusted with the order.
* **Raising the prior was tried and rejected.** At 0.58 the retrieval-only
  ranking improves (mock nDCG@10 0.835 -> 0.895) but the end-to-end result gets
  worse, because extra popular candidates enter the shortlist and the model then
  ranks them low, while specific niche matches are evicted (niche recall@12
  1.0 -> 0.6 at 0.62). The prior is a tie-breaker for text relevance, and that is
  how it is wired.
* **Presentation matters more than the prompt.** Tagging candidates with
  `[cran/r-package]` dropped laya's nDCG@10 from 0.760 to 0.650, and a long
  "you are an expert" instruction was worse than a four-word question.

The numbers are small-sample and the gold sets are two lists of 12 and 10
queries, one biased towards popular tools, one towards niche exact matches. They
are enough to catch a regression, not enough to claim a ranking is optimal.

### Known limit

Live papers can crowd the local catalogue out of the shortlist: for
`croston method intermittent demand forecasting` the OpenAlex hits displaced
`tsintermittent` entirely after the store grew with fetched papers. The measured
aggregate is stable (niche nDCG@10 0.846), but a per-source quota on the
shortlist is the next ranking change, together with the query expansion that the
`data.table` miss ("Extension of data.frame" vs "fast aggregation and joins")
actually needs.


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

## Sources

Bulk catalogues are downloaded rarely and indexed locally in SQLite/FTS5. Live
APIs are queried per search with a per-source budget.

| Source | Kind | Endpoint | Notes |
| --- | --- | --- | --- |
| CRAN | bulk | `web/packages/available_packages_by_date.html` (4.9 MB) | 25,595 packages in 0.6 s |
| CRAN downloads | bulk | `cranlogs.r-pkg.org/downloads/total/last-month/<names>` | 120 names per request, ~214 requests, ~2 min |
| Bioconductor | bulk | `packages/json/3.21/bioc/packages.json` (6.3 MB) | 2,341 packages, reverse deps computed locally |
| arXiv | live | `export.arxiv.org/api/query` | quotes must be percent-encoded or HTTP 400 |
| OpenAlex | live | `api.openalex.org/works?select=...` | `select` is required, the default payload is 10x larger |
| Crossref | live | `api.crossref.org/works` | cheapest scholarly fallback |

Rejected on evidence, so they are not silently missing: `crandb /-/search` is
404, `crandb /-/all` is 307 MB, `cran src/contrib/PACKAGES.gz` has no Title or
Description fields (25,608 blocks, zero usable rows), Papers with Code redirects
away and its API is gone, Semantic Scholar answers 429 without a key, and PyPI's
simple index is 47 MB of HTML.

## Layout

```
src/trick/errors.py       stable S0..S9 taxonomy, each with a degraded action
src/trick/quant.py        unit-vector int8 signature quantisation
src/trick/matrix.py       file format, atomic writer, mmap reader, scan
src/trick/accel.py        optional numpy scan (stdlib fallback is authoritative)
src/trick/emergency.py    embedded seed catalogue and fallback loader
src/trick/credentials.py  key-file parsing, permission check, redaction
src/trick/laya.py         laya client: opaque labels, retries, breaker, usage
src/trick/textindex.py    SQLite store: documents, FTS5, priors, decision cache
src/trick/retrieval.py    bulk and live source adapters, dedupe, budgets
src/trick/search.py       the pipeline above, with provenance on every row
src/trick/render.py       one formatter for every surface
src/trick/service.py      core service: unix socket, worker queue, bridge token
src/trick/bridge.py       loopback HTTP bridge with bearer auth
src/trick/mcp.py          MCP server on stdio (JSON-RPC 2.0)
src/trick/tui.py          terminal session: Enter submits, history, commands
src/trick/cli.py          query / tui / serve / mcp / ingest / doctor / probe
tests/                    229 tests: layout, corruption, contention, budget,
                          adapters, client, pipeline, credentials, protocol,
                          permissions, concurrency, session behaviour
scripts/bench.py          produced numbers (not estimates) for this host
scripts/eval_ranking.py   22-query ranking harness, three orderings per query
scripts/laya_probe.py     one real call, prints the response shape only
scripts/check.sh          repository gate (10 checks)
```

## Install and verify

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'      # '.[accel]' adds numpy
.venv/bin/python -m pytest -q
sh scripts/check.sh
```

First run:

```sh
.venv/bin/python -m trick ingest                 # CRAN + Bioconductor + priors, ~2.5 min
.venv/bin/python -m trick doctor                 # store, matrix, credential state
.venv/bin/python -m trick query "mixed effects model does not converge" 
.venv/bin/python -m trick query "simple features spatial" --offline --json
.venv/bin/python -m trick tui                    # interactive; /help lists the commands
```

Run the service once, then point every host at it:

```sh
.venv/bin/python -m trick serve            # prints the socket and the bridge URL
.venv/bin/python -m trick tui              # attaches to the running service
.venv/bin/python -m trick mcp              # attaches too; the host spawns this
curl -s http://127.0.0.1:8765/v1/search \
  -H "Authorization: Bearer $(cat ~/.local/share/trick/bridge.token)" \
  -H 'Content-Type: application/json' \
  -d '{"query":"mixed effects model does not converge","limit":5}'
```

Without a running service each command builds its own engine. That works, but
two processes then hold two SQLite connections and two decision caches, so the
service is the recommended mode as soon as more than one surface is in use.

## Configuration

Credentials are read from the environment (`LAYA_BASE_URL`, `LAYA_API_KEY`,
`LAYA_TIMEOUT_MS`) or from `$TRICK_CREDENTIALS`, default
`~/.config/hermes-secrets/laya.credentials`. The file must be mode 600: a
group-readable key file is refused rather than warned about, because the file
grants spend on a paid endpoint. `TRICK_ALLOW_INSECURE_CREDENTIALS=1` overrides
that on throwaway machines.

```
LAYA_BASE_URL=https://your-endpoint/v1/systemone/batch
LAYA_API_KEY=your-key
LAYA_TIMEOUT_MS=20000
```

Data lives in `~/.local/share/trick/` (`trick.db`, `scores.trk`, `run/trick.sock`,
`bridge.token`, `history`); override with `--db`, `--matrix`, `--socket` or
`TRICK_HOME`.

`TRICK_CONTACT_EMAIL` puts the caller in the scholarly APIs' polite pools
(OpenAlex's anonymous bucket answers HTTP 429 after a handful of searches, which
shows up as `S4` on every result). `TRICK_CORS_ORIGINS` is a comma-separated
allow-list for browser callers; empty means no CORS headers at all.

`--scorer auto` uses laya when credentials exist and the offline scorer
otherwise. A configured endpoint that is down surfaces as `S5`, never as a silent
downgrade to fake scores.
