
\name{trick_sig_cosine}
\alias{trick_sig_cosine}
\title{Cosine similarity of two stored signatures}
\description{
The dot product of the int8 signatures divided by the square of the quantisation
scale, clamped to the interval from minus one to one.
}
\usage{
trick_sig_cosine(a, b)
}
\arguments{
  \item{a, b}{Raw vectors of length 16, from trick_signature.}
}
\value{
A single number.
}
\examples{
sig <- trick_signature(c(1, rep(0, 15)))
trick_sig_cosine(sig, sig)
}

