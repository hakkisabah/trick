
\name{trick_chunks}
\alias{trick_chunks}
\title{Split a conversation into scored passages}
\description{
Turns a list of chat messages into sentence-aligned passages. Each message is cut
at sentence boundaries and packed greedily until it holds at least min_chars, and
no passage exceeds max_chars without being cut at a word boundary. Turn numbers
start at 1 and follow the order of messages.
}
\usage{
trick_chunks(messages, min_chars = 120L, max_chars = 600L)
}
\arguments{
  \item{messages}{A list of messages. Each message is a list with role and
  content, and optionally id, which is carried through so a result can point back
  at its message.}
  \item{min_chars}{Minimum passage length; shorter tails are merged backwards.}
  \item{max_chars}{Maximum passage length.}
}
\value{
A list of passages, each with turn, role, message_id, text, char_start and
char_end.
}
\examples{
messages <- list(
  list(id = "u1", role = "user", content = "Where did we set the threshold?"),
  list(id = "a1", role = "assistant", content = "At 0.15, after the second run.")
)
length(trick_chunks(messages))
}

