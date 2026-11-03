
\name{trick_search}
\alias{trick_search}
\title{Find scored passages in a conversation}
\description{
Ranks the passages of an index against a query. Scoring is Okapi BM25 over folded
tokens, multiplied by a phrase bonus when the whole query appears verbatim in a
passage. When laya is supplied it is blended in as a probability, but only when
the lexical ranking is ambiguous: if the best passage beats the second by more
than gate, the model is ignored and the attribute laya_used is FALSE. That gate
keeps a weak model from displacing a passage that literally contains the words.
}
\usage{
trick_search(index, query, limit = 3L, laya = NULL,
             weights = c(0.2, 0.8), gate = 0.15)
}
\arguments{
  \item{index}{An index from trick_index.}
  \item{query}{A search string.}
  \item{limit}{Maximum number of passages to return.}
  \item{laya}{Optional named numeric vector of probabilities in zero to one,
  keyed by passage number as a character, in the order of the passages.}
  \item{weights}{Numeric vector of length two: weight of the model and weight of
  the lexical score.}
  \item{gate}{Relative margin above which the model is not consulted.}
}
\value{
A data frame with one row per returned passage and the columns rank, turn, role,
message_id, matched, coverage, lexical, model, score and text. Attributes: weak,
TRUE when no passage carries half of the question is weight, and laya_used.
}
\examples{
index <- trick_index(list(
  list(role = "user", content = "Where is the taper set?"),
  list(role = "assistant", content = "The taper is 0.15 for low counts.")
))
trick_search(index, "taper", limit = 1)$turn
}

