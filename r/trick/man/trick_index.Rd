
\name{trick_index}
\alias{trick_index}
\alias{print.trick_index}
\title{Index a conversation for scored search}
\description{
Chunks the messages and computes the term statistics the ranking needs: term
frequency per passage, document frequency per term and an Okapi BM25 inverse
document frequency. The index is an ordinary R object; it is not written to disk
and it is not shared between sessions.
}
\usage{
trick_index(messages, min_chars = 120L, max_chars = 600L)
}
\arguments{
  \item{messages}{A list of chat messages, as accepted by trick_chunks.}
  \item{min_chars}{Minimum passage length.}
  \item{max_chars}{Maximum passage length.}
}
\value{
An object of class trick_index with the passages, their tokens, lengths and the
term statistics.
}
\examples{
index <- trick_index(list(
  list(role = "user", content = "Where is the taper set?"),
  list(role = "assistant", content = "The taper is 0.15 for low counts.")
))
index
}

