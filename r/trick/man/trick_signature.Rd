
\name{trick_signature}
\alias{trick_signature}
\title{Quantise one vector into a 16 byte signature}
\description{
Normalises the vector to unit length and stores each component as a signed byte
scaled by 127, the form used in the score matrix.
}
\usage{
trick_signature(vector)
}
\arguments{
  \item{vector}{Numeric vector of length 16.}
}
\value{
A raw vector of length 16.
}
\examples{
length(trick_signature(c(1, rep(0, 15))))
}

