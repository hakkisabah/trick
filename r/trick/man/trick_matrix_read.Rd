
\name{trick_matrix_read}
\alias{trick_matrix_read}
\title{Read a score matrix}
\description{
Reads the fixed-record matrix written by trick_matrix_write or by the companion
Python implementation. A truncated, corrupted or foreign file is refused with a
message naming what was found, never returned half-read.
}
\usage{
trick_matrix_read(path)
}
\arguments{
  \item{path}{Path to the matrix.}
}
\value{
A list with count, dim, qscale, synthetic, crc32_ok and records, the last being a
data frame with id, score, flags and a list column of signatures.
}
\examples{
path <- tempfile(fileext = ".trk")
trick_matrix_write(path, id = 1:2, signature = rbind(rep(0, 16), rep(0, 16)),
                   score = c(0.1, 0.2))
trick_matrix_read(path)$count
unlink(path)
}

