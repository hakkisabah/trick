# Turkish-aware folding and tokenisation.
#
# The contract, measured against three real chats: a reader who types ASCII for
# a word that carries Turkish letters must find the same passages as a reader
# who types the letters. ASCII queries returned 0 of 36 passages before this
# fold existed, while exact-surface queries returned 35 of 36.
#
# One character maps to one character, so an offset into the folded string is an
# offset into the original and a passage can be cut from what the reader wrote.

# Folded once at load time: \u0131 is dotless i, \u015f is s-cedilla, and so on.
TR_FOLD_MAP <- c(
  "\u0131" = "i", "\u0130" = "i", "I" = "i",
  "\u015f" = "s", "\u015e" = "s",
  "\u00e7" = "c", "\u00c7" = "c",
  "\u011f" = "g", "\u011e" = "g",
  "\u00f6" = "o", "\u00d6" = "o",
  "\u00fc" = "u", "\u00dc" = "u",
  "\u00e2" = "a", "\u00c2" = "a",
  "\u00ee" = "i", "\u00ce" = "i",
  "\u00fb" = "u", "\u00db" = "u"
)

# Function words, kept in folded form. A question is mostly function words:
# they match every passage, so they carry no signal and are dropped before
# scoring.
TR_STOPWORDS <- c(
  "acaba", "ama", "ancak", "artik", "aslinda", "az", "bana", "bazi", "belki",
  "ben", "beni", "benim", "bes", "bile", "bir", "biraz", "bircok", "bize",
  "biz", "bu", "buna", "bunda", "bundan", "bunu", "bunun", "burada", "cok",
  "cunku", "da", "daha", "de", "defa", "degil", "diger", "diye", "dolayi",
  "eden", "ederek", "edilen", "edilmesi", "eger", "elbette", "en", "fakat",
  "falan", "farkli", "gibi", "gore", "hala", "hangi", "hatta", "hem", "hemen",
  "hep", "hepsi", "her", "herhangi", "herkes", "hic", "hicbir", "icin",
  "icinde", "iki", "ile", "ilgili", "ise", "iste", "kadar", "kendi", "kez",
  "ki", "kim", "kimi", "kimse", "mi", "mu", "nasil", "ne", "neden", "nedenle",
  "nerede", "nereye", "nicin", "niye", "o", "olan", "olarak", "oldu",
  "oldugu", "oldugunu", "oldukca", "olmad", "olmadan", "olmak", "olmasi",
  "olmayan", "olmaz", "olsa", "olsun", "on", "ona", "ondan", "onlar",
  "onlari", "onun", "orada", "oysa", "oyle", "once", "ozellikle", "pek",
  "ragmen", "sadece", "sanki", "sen", "seni", "senin", "siz", "size", "son",
  "sonra", "sey", "seye", "seyleri", "si", "su", "suna", "sunda", "sundan",
  "sunu", "sunun", "tarafindan", "tum", "tumunu", "uzere", "uzerine", "var",
  "vardi", "ve", "veya", "veyahut", "ya", "yani", "yapacak", "yapilan",
  "yapilmasi", "yapti", "yine", "yok", "yoksa", "zaten",
  # English, for the mixed chats that exist in practice.
  "a", "about", "after", "all", "also", "an", "and", "any", "are", "as", "at",
  "be", "been", "before", "but", "by", "can", "did", "do", "does", "for",
  "from", "had", "has", "have", "he", "her", "his", "how", "i", "if", "in",
  "into", "is", "it", "its", "me", "my", "no", "not", "of", "on", "or", "our",
  "she", "so", "than", "that", "the", "their", "them", "then", "there",
  "these", "they", "this", "to", "was", "we", "were", "what", "when", "where",
  "which", "who", "why", "will", "with", "you", "your"
)

#' Fold Turkish letters and case to ASCII
#'
#' Lowercases text and maps the Turkish letters that carry a diacritic, plus the
#' dotless \u0131, to their ASCII counterparts. Matching is done on folded text
#' on both sides, so a query typed without Turkish letters finds the same
#' passages as one typed with them. The mapping is one character to one
#' character, which keeps offsets valid: a match position in the folded string
#' is a position in the original.
#'
#' @param x A character vector.
#' @return A character vector of the same length, folded.
#' @examples
#' tr_fold("Ya\u015fad\u0131\u011f\u0131m\u0131z yer")
#' @export
tr_fold <- function(x) {
  if (!is.character(x)) {
    x <- as.character(x)
  }
  y <- chartr(paste(names(TR_FOLD_MAP), collapse = ""),
              paste(unname(TR_FOLD_MAP), collapse = ""), x)
  tolower(y)
}

#' Split text into scored tokens
#'
#' Folds the input, splits on anything that is not a letter or a digit, drops
#' tokens shorter than two characters and drops function words. The result is
#' what the ranking actually sees; keeping the raw words is the caller's job.
#'
#' @param x A character vector.
#' @return A character vector of tokens, in the order they appear.
#' @examples
#' tokenize_tr("The threshold was 0.15")
#' @export
tokenize_tr <- function(x) {
  if (!is.character(x)) {
    x <- as.character(x)
  }
  parts <- unlist(regmatches(tr_fold(x), gregexpr("[a-z0-9]+", tr_fold(x))),
                  use.names = FALSE)
  if (length(parts) == 0L) {
    return(character(0))
  }
  parts <- parts[nchar(parts) >= 2L]
  parts[!(parts %in% TR_STOPWORDS)]
}
