
\name{tr_fold}
\alias{tr_fold}
\title{Fold Turkish letters and case to ASCII}
\description{
Lowercases text and maps the Turkish letters that carry a diacritic, plus the
dotless i, to their ASCII counterparts. Matching is done on folded text on both
sides, so a query typed without Turkish letters finds the same passages as one
typed with them. The mapping is one character to one character, which keeps
offsets valid: a match position in the folded string is a position in the
original.
}
\usage{
tr_fold(x)
}
\arguments{
  \item{x}{A character vector.}
}
\value{
A character vector of the same length, folded.
}
\examples{
tr_fold("Yasadigimiz yer")
tr_fold("Ya\u015fad\u0131\u011f\u0131m\u0131z yer")
}

