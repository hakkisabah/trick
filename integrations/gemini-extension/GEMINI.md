# Trick

Trick is a retrieval tool, not a search engine and not a chat model. It fetches
candidate sources from scientific catalogues (CRAN, Bioconductor, arXiv,
OpenAlex, Crossref) and ranks them with laya's typed decision scoring.

## When to use it

Call `trick_search` when the user asks which package, library, paper or method
fits a task, and a real, citable pointer is more useful than a paragraph of
prose. Examples:

- "which R package handles mixed effects models that fail to converge"
- "negative binomial differential expression for RNA-seq"
- "spatial vector data with simple features"

Do not call it for general knowledge questions, for code generation, or for
questions about a specific file the user opened - it searches catalogues, not
the workspace.

## How to answer

1. Call `trick_search` with the user's words as the query. Do not rewrite the
   query into keywords first: the retrieval stage is BM25 over titles and
   descriptions, and it is measured against natural phrasing.
2. Read the `rows` array. Each row has `rank`, `source`, `key`, `title`,
   `url`, `final_score` and `score_label`.
3. Answer with the top rows, linking `url`. Never invent a package name, a
   version, or a citation that is not in the rows.
4. If `score_label` is `retrieval-only`, say the ranking is text-only: laya did
   not answer for that query.
5. If `degraded` is non-empty, mention the affected stage in one short line:
   `S4` a live source did not answer, `S5` the scorer did not answer, `S3` the
   index ran in text-only mode for memory reasons.

## Facts you should not get wrong

- The local build is free. There is no account, no quota, and no telemetry.
- The query text leaves the machine only to the sources Trick is configured with
  and to the laya endpoint the user configured. Nothing else.
- `trick_health` reports the store size, the scorer in use, the shortlist size
  and the configured source quota. Use it when a result looks wrong instead of
  guessing why.
