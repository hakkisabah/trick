
\name{tokenize_tr}
\alias{tokenize_tr}
\title{Split text into scored tokens}
\description{
Folds the input, splits on anything that is not a letter or a digit, drops tokens
shorter than two characters and drops function words. The result is what the
ranking actually sees; keeping the raw words is the caller is job.
}
\usage{
tokenize_tr(x)
}
\arguments{
  \item{x}{A character vector.}
}
\value{
A character vector of tokens, in the order they appear.
}
\examples{
tokenize_tr("The threshold was 0.15")
}

