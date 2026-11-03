# Generate the man pages for the R package. Run once at release time, from the
# package root:  Rscript tools/make_rd.R
# Hand-written Rd is deliberate: roxygen2 would add a build-time dependency to a
# package whose whole point is that it has none.

dir.create("man", showWarnings = FALSE)

write_rd <- function(name, text) {
  writeLines(text, file.path("man", paste0(name, ".Rd")), useBytes = TRUE)
}

write_rd("tr_fold", '
\\name{tr_fold}
\\alias{tr_fold}
\\title{Fold Turkish letters and case to ASCII}
\\description{
Lowercases text and maps the Turkish letters that carry a diacritic, plus the
dotless i, to their ASCII counterparts. Matching is done on folded text on both
sides, so a query typed without Turkish letters finds the same passages as one
typed with them. The mapping is one character to one character, which keeps
offsets valid: a match position in the folded string is a position in the
original.
}
\\usage{
tr_fold(x)
}
\\arguments{
  \\item{x}{A character vector.}
}
\\value{
A character vector of the same length, folded.
}
\\examples{
tr_fold("Yasadigimiz yer")
tr_fold("Ya\\u015fad\\u0131\\u011f\\u0131m\\u0131z yer")
}
')

write_rd("tokenize_tr", '
\\name{tokenize_tr}
\\alias{tokenize_tr}
\\title{Split text into scored tokens}
\\description{
Folds the input, splits on anything that is not a letter or a digit, drops tokens
shorter than two characters and drops function words. The result is what the
ranking actually sees; keeping the raw words is the caller is job.
}
\\usage{
tokenize_tr(x)
}
\\arguments{
  \\item{x}{A character vector.}
}
\\value{
A character vector of tokens, in the order they appear.
}
\\examples{
tokenize_tr("The threshold was 0.15")
}
')

write_rd("trick_chunks", '
\\name{trick_chunks}
\\alias{trick_chunks}
\\title{Split a conversation into scored passages}
\\description{
Turns a list of chat messages into sentence-aligned passages. Each message is cut
at sentence boundaries and packed greedily until it holds at least min_chars, and
no passage exceeds max_chars without being cut at a word boundary. Turn numbers
start at 1 and follow the order of messages.
}
\\usage{
trick_chunks(messages, min_chars = 120L, max_chars = 600L)
}
\\arguments{
  \\item{messages}{A list of messages. Each message is a list with role and
  content, and optionally id, which is carried through so a result can point back
  at its message.}
  \\item{min_chars}{Minimum passage length; shorter tails are merged backwards.}
  \\item{max_chars}{Maximum passage length.}
}
\\value{
A list of passages, each with turn, role, message_id, text, char_start and
char_end.
}
\\examples{
messages <- list(
  list(id = "u1", role = "user", content = "Where did we set the threshold?"),
  list(id = "a1", role = "assistant", content = "At 0.15, after the second run.")
)
length(trick_chunks(messages))
}
')

write_rd("trick_index", '
\\name{trick_index}
\\alias{trick_index}
\\alias{print.trick_index}
\\title{Index a conversation for scored search}
\\description{
Chunks the messages and computes the term statistics the ranking needs: term
frequency per passage, document frequency per term and an Okapi BM25 inverse
document frequency. The index is an ordinary R object; it is not written to disk
and it is not shared between sessions.
}
\\usage{
trick_index(messages, min_chars = 120L, max_chars = 600L)
}
\\arguments{
  \\item{messages}{A list of chat messages, as accepted by trick_chunks.}
  \\item{min_chars}{Minimum passage length.}
  \\item{max_chars}{Maximum passage length.}
}
\\value{
An object of class trick_index with the passages, their tokens, lengths and the
term statistics.
}
\\examples{
index <- trick_index(list(
  list(role = "user", content = "Where is the taper set?"),
  list(role = "assistant", content = "The taper is 0.15 for low counts.")
))
index
}
')

write_rd("trick_search", '
\\name{trick_search}
\\alias{trick_search}
\\title{Find scored passages in a conversation}
\\description{
Ranks the passages of an index against a query. Scoring is Okapi BM25 over folded
tokens, multiplied by a phrase bonus when the whole query appears verbatim in a
passage. When laya is supplied it is blended in as a probability, but only when
the lexical ranking is ambiguous: if the best passage beats the second by more
than gate, the model is ignored and the attribute laya_used is FALSE. That gate
keeps a weak model from displacing a passage that literally contains the words.
}
\\usage{
trick_search(index, query, limit = 3L, laya = NULL,
             weights = c(0.2, 0.8), gate = 0.15)
}
\\arguments{
  \\item{index}{An index from trick_index.}
  \\item{query}{A search string.}
  \\item{limit}{Maximum number of passages to return.}
  \\item{laya}{Optional named numeric vector of probabilities in zero to one,
  keyed by passage number as a character, in the order of the passages.}
  \\item{weights}{Numeric vector of length two: weight of the model and weight of
  the lexical score.}
  \\item{gate}{Relative margin above which the model is not consulted.}
}
\\value{
A data frame with one row per returned passage and the columns rank, turn, role,
message_id, matched, coverage, lexical, model, score and text. Attributes: weak,
TRUE when no passage carries half of the question is weight, and laya_used.
}
\\examples{
index <- trick_index(list(
  list(role = "user", content = "Where is the taper set?"),
  list(role = "assistant", content = "The taper is 0.15 for low counts.")
))
trick_search(index, "taper", limit = 1)$turn
}
')

write_rd("trick_signature", '
\\name{trick_signature}
\\alias{trick_signature}
\\title{Quantise one vector into a 16 byte signature}
\\description{
Normalises the vector to unit length and stores each component as a signed byte
scaled by 127, the form used in the score matrix.
}
\\usage{
trick_signature(vector)
}
\\arguments{
  \\item{vector}{Numeric vector of length 16.}
}
\\value{
A raw vector of length 16.
}
\\examples{
length(trick_signature(c(1, rep(0, 15))))
}
')

write_rd("trick_sig_cosine", '
\\name{trick_sig_cosine}
\\alias{trick_sig_cosine}
\\title{Cosine similarity of two stored signatures}
\\description{
The dot product of the int8 signatures divided by the square of the quantisation
scale, clamped to the interval from minus one to one.
}
\\usage{
trick_sig_cosine(a, b)
}
\\arguments{
  \\item{a, b}{Raw vectors of length 16, from trick_signature.}
}
\\value{
A single number.
}
\\examples{
sig <- trick_signature(c(1, rep(0, 15)))
trick_sig_cosine(sig, sig)
}
')

write_rd("trick_matrix_write", '
\\name{trick_matrix_write}
\\alias{trick_matrix_write}
\\title{Write a score matrix}
\\description{
Writes the fixed-record matrix read by trick_matrix_read and by the companion
Python implementation. The write is atomic: the file is written beside the target
and renamed, so a crash leaves either the old matrix or the new one.
}
\\usage{
trick_matrix_write(path, id, signature, score, flags = NULL,
                   synthetic = FALSE)
}
\\arguments{
  \\item{path}{Destination path.}
  \\item{id}{Integer vector of record ids.}
  \\item{signature}{A list of raw vectors, or a numeric matrix with 16 columns,
  one row per record.}
  \\item{score}{Numeric vector in the interval from zero to one.}
  \\item{flags}{Optional integer vector of per-record flags.}
  \\item{synthetic}{TRUE when the content is not a learned embedding; sets the
  header flag so a reader can say so.}
}
\\value{
The path, invisibly.
}
\\examples{
path <- tempfile(fileext = ".trk")
trick_matrix_write(path, id = 1:2,
                   signature = rbind(rep(0.1, 16), rep(0.2, 16)),
                   score = c(0.5, 0.6))
unlink(path)
}
')

write_rd("trick_matrix_read", '
\\name{trick_matrix_read}
\\alias{trick_matrix_read}
\\title{Read a score matrix}
\\description{
Reads the fixed-record matrix written by trick_matrix_write or by the companion
Python implementation. A truncated, corrupted or foreign file is refused with a
message naming what was found, never returned half-read.
}
\\usage{
trick_matrix_read(path)
}
\\arguments{
  \\item{path}{Path to the matrix.}
}
\\value{
A list with count, dim, qscale, synthetic, crc32_ok and records, the last being a
data frame with id, score, flags and a list column of signatures.
}
\\examples{
path <- tempfile(fileext = ".trk")
trick_matrix_write(path, id = 1:2, signature = rbind(rep(0, 16), rep(0, 16)),
                   score = c(0.1, 0.2))
trick_matrix_read(path)$count
unlink(path)
}
')

cat("wrote", length(list.files("man")), "man pages\n")
