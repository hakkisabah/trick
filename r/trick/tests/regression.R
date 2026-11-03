# Regression tests. Plain stopifnot on purpose: testthat would add a build-time
# dependency to a package that ships none, and every assertion here is a fact
# measured on the real tool.

library(trick)

expect_error <- function(expr, pattern) {
  message <- tryCatch({
    force(expr)
    NA_character_
  }, error = function(e) conditionMessage(e))
  stopifnot(!is.na(message))
  stopifnot(grepl(pattern, message, fixed = TRUE))
  invisible(message)
}

## Folding: a reader typing ASCII must reach the passage written with Turkish
## letters. Measured on three real chats: 0 of 36 before the fold, 35 of 36
## after.
stopifnot(identical(tr_fold("Ya\u015fad\u0131\u011f\u0131m\u0131z yer"), "yasadigimiz yer"))
stopifnot(identical(tr_fold("YASADIGIMIZ"), "yasadigimiz"))
stopifnot(identical(tr_fold("\u00c7OK"), "cok"))

## Function words carry no signal: they are dropped before scoring.
stopifnot(identical(tokenize_tr("i5 en \u00e7ok problem yasadigimiz yer"),
                    c("i5", "problem", "yasadigimiz", "yer")))
stopifnot(length(tokenize_tr("a of the")) == 0L)

## Chunking: sentence-aligned, bounded, and the turn numbers follow the input.
long <- paste(rep("This sentence is about the threshold.", 40), collapse = " ")
messages <- list(
  list(id = "u1", role = "user", content = "Where is the taper set?"),
  list(id = "a1", role = "assistant", content = long),
  list(id = "u2", role = "user", content = "And the dispersion?")
)
chunks <- trick_chunks(messages)
stopifnot(length(chunks) >= 4L)
stopifnot(all(vapply(chunks, function(chunk) nchar(chunk$text), integer(1)) <= 600L))
stopifnot(all(vapply(chunks, function(chunk) nchar(chunk$text), integer(1)) <= 600L))
stopifnot(identical(unique(vapply(chunks, function(chunk) chunk$turn, integer(1))), 1:3))
stopifnot(identical(chunks[[1]]$message_id, "u1"))

## Search finds the needle, reports the words that matched, and says whether the
## passage carries enough of the question to be an answer.
chat <- list(
  list(id = "m1", role = "user", content = "We should look at the memory limit."),
  list(id = "m2", role = "assistant", content = "The container hit MemoryMax during the build."),
  list(id = "m3", role = "user", content = "Anything else on the scheduler?"),
  list(id = "m4", role = "assistant", content = "The scheduler retried twice, then gave up.")
)
index <- trick_index(chat)
hits <- trick_search(index, "MemoryMax", limit = 2L)
stopifnot(nrow(hits) == 1L)
stopifnot(hits$turn[1] == 2L)
stopifnot(identical(hits$role[1], "assistant"))
stopifnot(grepl("memorymax", hits$matched[1], ignore.case = TRUE) ||
            grepl("MemoryMax", hits$text[1], fixed = TRUE))

## A question made only of function words is not a search.
stopifnot(nrow(trick_search(index, "en \u00e7ok ne")) == 0L)

## The weak flag: a passage that shares one common word is not an answer.
weak_hits <- trick_search(index, "the", limit = 3L)
stopifnot(nrow(weak_hits) == 0L)

## A clear lexical winner is never second-guessed: the model is not consulted,
## so a wrong model cannot displace the passage that literally contains the word.
model <- c("1" = 0.9, "2" = 0.1, "3" = 0.9, "4" = 0.9)
gated <- trick_search(index, "MemoryMax", limit = 3L, laya = model)
stopifnot(identical(attr(gated, "laya_used"), FALSE))
stopifnot(gated$turn[1] == 2L)

## When the text ranking is genuinely ambiguous, the model does decide.
tie <- list(
  list(id = "t1", role = "assistant", content = "The taper was set to 0.15 for low counts."),
  list(id = "t2", role = "assistant", content = "The taper was set to 0.15 for high counts.")
)
tie_index <- trick_index(tie)
tie_model <- c("1" = 0.05, "2" = 0.95)
blended <- trick_search(tie_index, "taper 0.15", limit = 2L, laya = tie_model)
stopifnot(identical(attr(blended, "laya_used"), TRUE))
stopifnot(nrow(blended) == 2L)
stopifnot(blended$message_id[1] == "t2")

## The score matrix: this package must read the file the Python writer produced,
## and reproduce it byte for byte.
fixture <- system.file("extdata", "sample.trk", package = "trick")
stopifnot(nzchar(fixture))
matrix <- trick_matrix_read(fixture)
stopifnot(matrix$count == 3)
stopifnot(identical(matrix$records$id, c(101, 202, 303)))
## The stored score is a 16 bit integer: 32768 of 65535 is 0.500008, not 0.5.
stopifnot(all(abs(matrix$records$score - c(0, 0.5, 1)) < 1e-4))
stopifnot(identical(matrix$synthetic, TRUE))
stopifnot(identical(matrix$crc32_ok, TRUE))
stopifnot(abs(trick_sig_cosine(matrix$records$signature[[2]],
                              matrix$records$signature[[2]]) - 1) < 1e-6)

out <- tempfile(fileext = ".trk")
trick_matrix_write(
  out,
  id = c(101, 202, 303),
  signature = rbind(rep(0.5, 16), c(1, rep(0, 15)), rep(0, 16)),
  score = c(0, 0.5, 1),
  flags = c(0, 1, 2),
  synthetic = TRUE
)
stopifnot(identical(readBin(out, "raw", n = file.info(out)$size),
                    readBin(fixture, "raw", n = file.info(fixture)$size)))
unlink(out)

## A truncated file is refused instead of half-read.
broken <- tempfile(fileext = ".trk")
writeBin(readBin(fixture, "raw", n = 60L), broken)
expect_error(trick_matrix_read(broken), "truncated")
expect_error(trick_matrix_read(tempfile()), "no score matrix")
unlink(broken)

cat("all regression tests passed\n")
