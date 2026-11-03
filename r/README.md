# trick (R)

The same scored find as the Python package, in R, with no compiled code and no
dependencies. It exists because the corpora this tool cares about are R corpora:
a reader who works in R should be able to point the same ranking at their own
material without leaving the language.

Two things are shared with the Python implementation, byte for byte:

* the **score matrix** format (`.trk`): a 48 byte header plus 24 byte records, so
  a matrix written by either language is read by the other. `inst/extdata/sample.trk`
  is a fixture produced by the Python writer, and the test suite asserts that R
  reads it and reproduces it exactly.
* the **matching contract**: Turkish letters fold to ASCII on both sides, so a
  query typed without Turkish letters finds a passage written with them.

## Install

```sh
R CMD INSTALL r/trick
```

CRAN submission uses the same command CRAN does:

```sh
sh r/check.sh          # R CMD build + R CMD check --as-cran --no-manual
```

## Use

```r
library(trick)

messages <- list(
  list(id = "u1", role = "user", content = "Where did we set the threshold?"),
  list(id = "a1", role = "assistant", content = "At 0.15, after the second run."),
  list(id = "u2", role = "user", content = "And the taper?")
)

index <- trick_index(messages)
trick_search(index, "taper 0.15", limit = 2)
```

`trick_search()` returns a data frame: `turn`, `role`, `message_id`, the words
that matched, a coverage share, the lexical score, the model score and the
blended score. Two attributes carry the caveats:

* `weak` is `TRUE` when no passage carries half of the question's weight. That is
  the honest reading of "these are the closest turns, not an answer".
* `laya_used` is `FALSE` when the lexical ranking was clear enough that the model
  was not consulted. On a real transcript the lexical layer alone found the
  answering passage 90 percent of the time, so the model is an equalizer here,
  not a ranker: it is asked only when the top two passages are within `gate`
  (15 percent by default) of each other.

Passing a model is optional and explicit:

```r
# probabilities in [0, 1], keyed by passage number as a character
trick_search(index, "threshold", laya = c("1" = 0.9, "2" = 0.1))
```

Nothing in this package makes a network call, reads a credential or embeds a
model. Every score is computed from the data the caller passes in.

## Score matrix

```r
trick_matrix_write("scores.trk", id = 1:2,
                   signature = rbind(rep(0.1, 16), rep(0.2, 16)),
                   score = c(0.5, 0.6), synthetic = TRUE)

m <- trick_matrix_read("scores.trk")
m$count          # 2
m$crc32_ok       # TRUE - the body matches its checksum
m$synthetic      # TRUE - not a learned embedding, and the header says so
```

The record is 24 bytes because the cheapest host in the fleet is the target:
24 MB per million passages. Sixteen dimensions is a deliberate loss of fidelity,
recovered by rescoring the shortlist with the text index rather than by spending
memory.

## Why the check script exists

Some builds of R hardcode the path of `which` (conda-forge's build looks for
`/usr/bin/which`), and on a host without that file R cannot load even `utils`.
`r/check.sh` detects this and runs the check inside an unprivileged user
namespace that overlays a `which` shim onto `/usr/bin` for that process tree
only: no root, no change to the host. On a normal Linux distribution the script
just runs `R CMD check`.
