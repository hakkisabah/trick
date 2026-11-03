# The in-memory index: passages, term frequencies and inverse document
# frequency. Nothing is persisted; an index lives as long as the R object, which
# is the whole point of a find that must answer in milliseconds.

K1 <- 1.2
B <- 0.75

#' Index a conversation for scored search
#'
#' Chunks the messages with [trick_chunks()] and computes the term statistics the
#' ranking needs: term frequency per passage, document frequency per term and an
#' Okapi BM25 inverse document frequency. The index is an ordinary R object; it
#' is not written to disk and it is not shared between sessions.
#'
#' @param messages A list of chat messages, as accepted by [trick_chunks()].
#' @param min_chars Minimum passage length.
#' @param max_chars Maximum passage length.
#' @return An object of class `trick_index`.
#' @examples
#' index <- trick_index(list(
#'   list(role = "user", content = "Where is the taper set?"),
#'   list(role = "assistant", content = "The taper is 0.15 for low counts.")
#' ))
#' index$n
#' @export
trick_index <- function(messages, min_chars = 120L, max_chars = 600L) {
  chunks <- trick_chunks(messages, min_chars = min_chars, max_chars = max_chars)
  n <- length(chunks)
  folded <- vapply(chunks, function(chunk) paste(tr_fold(chunk$text), collapse = " "),
                   character(1))
  tokens <- lapply(chunks, function(chunk) tokenize_tr(chunk$text))
  lengths <- vapply(tokens, length, integer(1))

  df <- integer(0)
  if (n > 0L) {
    counts <- table(unlist(tokens, use.names = FALSE))
    df <- as.integer(counts)
    names(df) <- names(counts)
  }
  idf <- if (length(df) > 0L) {
    log(1 + (n - df + 0.5) / (df + 0.5))
  } else {
    numeric(0)
  }
  names(idf) <- names(df)

  structure(
    list(
      chunks = chunks,
      tokens = tokens,
      folded = folded,
      lengths = lengths,
      avg_len = if (n > 0L) mean(lengths) else 0,
      df = df,
      idf = idf,
      n = n
    ),
    class = "trick_index"
  )
}

#' @export
print.trick_index <- function(x, ...) {
  cat(sprintf("<trick_index> %d passages, %d terms, mean length %.1f\n",
              x$n, length(x$df), x$avg_len))
  invisible(x)
}
