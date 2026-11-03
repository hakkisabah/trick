# Sentence-aligned chunking.
#
# A chat message is not a passage: an answer can run to thousands of characters,
# and cutting it into fixed windows splits words in half and separates a claim
# from its qualifier. So a message is cut at sentence boundaries, greedily
# packed to MIN_CHARS..MAX_CHARS, and never mid-word.

MIN_CHARS <- 120L
MAX_CHARS <- 600L
PHRASE_BONUS <- 1.25
STRONG_COVERAGE <- 0.5

# Cut one message into sentences. A sentence ends at . ! ? or an ellipsis,
# followed by whitespace. The terminator stays with the sentence.
split_sentences <- function(text) {
  text <- gsub("[\r\n]+", " ", text)
  text <- gsub("[ \t]+", " ", text)
  text <- trimws(text)
  if (!nzchar(text)) {
    return(character(0))
  }
  pattern <- "(?<=[.!?\u2026])\\s+"
  parts <- strsplit(text, pattern, perl = TRUE)[[1]]
  parts <- trimws(parts)
  parts[nzchar(parts)]
}

# Cut a too-long sentence at a word boundary near MAX_CHARS.
cut_long <- function(sentence, max_chars) {
  out <- character(0)
  rest <- sentence
  while (nchar(rest) > max_chars) {
    window <- substr(rest, 1L, max_chars)
    cut <- max(gregexpr(" ", window, fixed = TRUE)[[1]])
    if (cut < 1L) {
      cut <- max_chars
    }
    out <- c(out, trimws(substr(rest, 1L, cut - 1L)))
    rest <- trimws(substr(rest, cut + 1L, nchar(rest)))
  }
  if (nzchar(rest)) {
    out <- c(out, rest)
  }
  out[nzchar(out)]
}

#' Split a conversation into scored passages
#'
#' Turns a list of chat messages into sentence-aligned passages. Each message is
#' cut at sentence boundaries and packed greedily until it holds at least
#' `min_chars`, and no passage exceeds `max_chars` without being cut at a word
#' boundary. Turn numbers start at 1 and follow the order of `messages`.
#'
#' A message is a list with `role` ("user" or "assistant"), `content` (a string;
#' a list of parts with a `text` field is also accepted) and optionally `id`,
#' which is carried through so a result can point back at its message.
#'
#' @param messages A list of messages.
#' @param min_chars Minimum passage length; shorter tails are merged backwards.
#' @param max_chars Maximum passage length.
#' @return A list of passages, each with `turn`, `role`, `message_id`, `text`,
#'   `char_start` and `char_end`.
#' @examples
#' messages <- list(
#'   list(id = "u1", role = "user", content = "Where did we set the threshold?"),
#'   list(id = "a1", role = "assistant", content = "At 0.15, after the second run.")
#' )
#' length(trick_chunks(messages))
#' @export
trick_chunks <- function(messages, min_chars = 120L, max_chars = 600L) {
  if (!is.list(messages) || length(messages) == 0L) {
    return(list())
  }
  min_chars <- as.integer(min_chars)
  max_chars <- as.integer(max_chars)
  if (is.na(min_chars) || is.na(max_chars) || min_chars < 1L || max_chars < min_chars) {
    stop("min_chars and max_chars must be positive and min_chars <= max_chars",
         call. = FALSE)
  }

  out <- list()
  for (index in seq_along(messages)) {
    message <- messages[[index]]
    if (!is.list(message)) {
      next
    }
    role <- as.character(message$role %||% "user")
    if (!role %in% c("user", "assistant")) {
      role <- "user"
    }
    content <- message$content
    if (is.list(content)) {
      content <- paste(vapply(content, function(part) {
        if (is.list(part)) as.character(part$text %||% "") else as.character(part)
      }, character(1)), collapse = "\n")
    }
    content <- as.character(content %||% "")
    if (!nzchar(trimws(content))) {
      next
    }
    message_id <- as.character(message$id %||% "")
    # Offsets refer to this whitespace-normalised copy of the message: sentences
    # are packed with single spaces, so a verbatim search in the raw content
    # would fail on any newline and silently report offset 0.
    norm <- gsub("[ \t]+", " ", gsub("[\r\n]+", " ", content))

    sentences <- unlist(lapply(split_sentences(norm), cut_long, max_chars),
                        use.names = FALSE)
    if (length(sentences) == 0L) {
      next
    }

    buffer <- character(0)
    buffer_len <- 0L
    emit <- function(parts) {
      text <- paste(parts, collapse = " ")
      if (!nzchar(text)) {
        return(invisible(NULL))
      }
      start <- regexpr(text, norm, fixed = TRUE)[[1]]
      out[[length(out) + 1L]] <<- list(
        turn = index,
        role = role,
        message_id = message_id,
        text = text,
        char_start = if (start > 0L) as.integer(start) else 0L,
        char_end = if (start > 0L) as.integer(start + nchar(text) - 1L) else 0L
      )
      invisible(NULL)
    }

    for (sentence in sentences) {
      if (buffer_len > 0L && buffer_len + 1L + nchar(sentence) > max_chars) {
        emit(buffer)
        buffer <- character(0)
        buffer_len <- 0L
      }
      buffer <- c(buffer, sentence)
      buffer_len <- buffer_len + if (buffer_len > 0L) 1L + nchar(sentence) else nchar(sentence)
      if (buffer_len >= min_chars) {
        emit(buffer)
        buffer <- character(0)
        buffer_len <- 0L
      }
    }
    if (length(buffer) > 0L) {
      emit(buffer)
    }
  }
  out
}
