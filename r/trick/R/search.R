# The ranking.
#
# Two layers, measured rather than assumed. The lexical layer (Okapi BM25 plus a
# phrase bonus) does the work: on a real transcript it found the passage that
# answers a question 90 percent of the time with no model at all. The model
# layer is an equalizer, not a ranker: it is asked only when the lexical top two
# are close, and it can never outvote the text.

# Normalise a numeric vector into [0, 1] without dividing by zero.
normalise01 <- function(x) {
  if (length(x) == 0L) {
    return(x)
  }
  top <- max(x)
  if (!is.finite(top) || top <= 0) {
    return(rep(0, length(x)))
  }
  x / top
}

# Coverage weights: the share of the question's information each term carries.
# A term that appears in every passage carries none of it.
coverage_weights <- function(terms, idf) {
  weights <- vapply(terms, function(term) {
    value <- idf[[term]]
    if (is.null(value) || is.na(value)) 0 else as.numeric(value)
  }, numeric(1))
  names(weights) <- terms
  weights
}

#' Find scored passages in a conversation
#'
#' Ranks the passages of an index against a query. Scoring is Okapi BM25 over
#' folded tokens, multiplied by a phrase bonus when the whole query appears
#' verbatim in a passage. When `laya` is supplied it is blended in as a
#' probability, but only when the lexical ranking is ambiguous: if the best
#' passage beats the second by more than `gate` (a relative margin), the model is
#' ignored and the attribute `laya_used` is `FALSE`. That gate is what keeps a
#' weak model from displacing a passage that literally contains the words.
#'
#' @param index An index from [trick_index()].
#' @param query A search string.
#' @param limit Maximum number of passages to return.
#' @param laya Optional named numeric vector of probabilities in `[0, 1]`, keyed
#'   by passage number as a character (the order of `index$chunks`). Names that
#'   do not match a passage are ignored.
#' @param weights Numeric vector of length two: weight of the model and weight of
#'   the lexical score.
#' @param gate Relative margin above which the model is not consulted.
#' @return A data frame with one row per returned passage and the columns
#'   `rank`, `turn`, `role`, `message_id`, `matched`, `coverage`, `lexical`,
#'   `model`, `score` and `text`. Attributes: `weak` (`TRUE` when no passage
#'   carries half of the question's weight) and `laya_used`.
#' @examples
#' index <- trick_index(list(
#'   list(role = "user", content = "Where is the taper set?"),
#'   list(role = "assistant", content = "The taper is 0.15 for low counts.")
#' ))
#' trick_search(index, "taper", limit = 1)$turn
#' @export
trick_search <- function(index, query, limit = 3L, laya = NULL,
                         weights = c(0.2, 0.8), gate = 0.15) {
  if (!inherits(index, "trick_index")) {
    stop("index must come from trick_index()", call. = FALSE)
  }
  limit <- as.integer(limit)
  if (is.na(limit) || limit < 1L) {
    stop("limit must be a positive integer", call. = FALSE)
  }
  if (length(weights) != 2L || any(is.na(weights))) {
    stop("weights must be two numbers", call. = FALSE)
  }
  empty <- data.frame(
    rank = integer(0), turn = integer(0), role = character(0),
    message_id = character(0), matched = character(0), coverage = numeric(0),
    lexical = numeric(0), model = numeric(0), score = numeric(0),
    text = character(0), stringsAsFactors = FALSE
  )
  if (index$n == 0L) {
    return(structure(empty, weak = FALSE, laya_used = FALSE))
  }

  terms <- unique(tokenize_tr(query))
  if (length(terms) == 0L) {
    return(structure(empty, weak = FALSE, laya_used = FALSE))
  }

  weights_by_term <- coverage_weights(terms, index$idf)
  total_weight <- sum(weights_by_term)
  phrase <- paste(tr_fold(query), collapse = " ")
  phrase <- gsub("[[:space:]]+", " ", trimws(phrase))

  scores <- numeric(index$n)
  coverage <- numeric(index$n)
  matched <- vector("list", index$n)
  for (i in seq_len(index$n)) {
    tokens <- index$tokens[[i]]
    if (length(tokens) == 0L) {
      next
    }
    counts <- table(tokens)
    length_norm <- K1 * (1 - B + B * index$lengths[i] / index$avg_len)
    total <- 0
    hits <- character(0)
    for (term in terms) {
      tf <- if (term %in% names(counts)) as.numeric(counts[[term]]) else 0
      if (tf <= 0) {
        next
      }
      hits <- c(hits, term)
      idf <- if (term %in% names(index$idf)) as.numeric(index$idf[[term]]) else 0
      total <- total + idf * (tf * (K1 + 1)) / (tf + length_norm)
    }
    if (total > 0 && nzchar(phrase) && grepl(phrase, index$folded[i], fixed = TRUE)) {
      total <- total * PHRASE_BONUS
    }
    scores[i] <- total
    coverage[i] <- if (total_weight > 0) {
      sum(weights_by_term[hits]) / total_weight
    } else {
      0
    }
    matched[[i]] <- hits
  }

  keep <- which(scores > 0)
  if (length(keep) == 0L) {
    return(structure(empty, weak = FALSE, laya_used = FALSE))
  }

  ordered <- keep[order(-scores[keep])]
  lexical <- scores[ordered]
  coverage <- coverage[ordered]
  matched <- matched[ordered]
  model <- rep(NA_real_, length(ordered))
  laya_used <- FALSE
  final <- normalise01(lexical)

  if (!is.null(laya) && length(laya) > 0L) {
    for (position in seq_along(ordered)) {
      key <- as.character(ordered[position])
      if (key %in% names(laya)) {
        model[position] <- as.numeric(laya[[key]])
      }
    }
    # The gate: a clear lexical winner is not second-guessed. With a single
    # candidate there is nothing to reorder, so the model is not asked either.
    ambiguous <- FALSE
    if (length(lexical) >= 2L) {
      ambiguous <- lexical[1] <= lexical[2] * (1 + gate)
    }
    if (ambiguous && any(!is.na(model))) {
      laya_used <- TRUE
      model[is.na(model)] <- 0
      blended <- weights[1] * model + weights[2] * normalise01(lexical)
      order2 <- order(-blended)
      ordered <- ordered[order2]
      lexical <- lexical[order2]
      coverage <- coverage[order2]
      matched <- matched[order2]
      model <- model[order2]
      final <- blended[order2]
    }
  }

  take <- seq_len(min(limit, length(ordered)))
  chunks <- index$chunks[ordered[take]]
  out <- data.frame(
    rank = seq_along(take),
    turn = vapply(chunks, function(chunk) as.integer(chunk$turn), integer(1)),
    role = vapply(chunks, function(chunk) as.character(chunk$role), character(1)),
    message_id = vapply(chunks, function(chunk) as.character(chunk$message_id), character(1)),
    matched = vapply(take, function(position) {
      paste(matched[[position]], collapse = ", ")
    }, character(1)),
    coverage = round(coverage[take], 3),
    lexical = round(lexical[take], 4),
    model = round(model[take], 4),
    score = round(final[take], 4),
    text = vapply(chunks, function(chunk) as.character(chunk$text), character(1)),
    stringsAsFactors = FALSE
  )

  structure(
    out,
    weak = max(coverage[take]) < STRONG_COVERAGE,
    laya_used = laya_used
  )
}
