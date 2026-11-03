
\name{trick_matrix_write}
\alias{trick_matrix_write}
\title{Write a score matrix}
\description{
Writes the fixed-record matrix read by trick_matrix_read and by the companion
Python implementation. The write is atomic: the file is written beside the target
and renamed, so a crash leaves either the old matrix or the new one.
}
\usage{
trick_matrix_write(path, id, signature, score, flags = NULL,
                   synthetic = FALSE)
}
\arguments{
  \item{path}{Destination path.}
  \item{id}{Integer vector of record ids.}
  \item{signature}{A list of raw vectors, or a numeric matrix with 16 columns,
  one row per record.}
  \item{score}{Numeric vector in the interval from zero to one.}
  \item{flags}{Optional integer vector of per-record flags.}
  \item{synthetic}{TRUE when the content is not a learned embedding; sets the
  header flag so a reader can say so.}
}
\value{
The path, invisibly.
}
\examples{
path <- tempfile(fileext = ".trk")
trick_matrix_write(path, id = 1:2,
                   signature = rbind(rep(0.1, 16), rep(0.2, 16)),
                   score = c(0.5, 0.6))
unlink(path)
}

