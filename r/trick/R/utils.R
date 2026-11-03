# Small internal helpers. No export: they exist so the rest of the package can
# read optional fields without four lines of NULL handling each time.

`%||%` <- function(x, y) {
  if (is.null(x) || length(x) == 0L) y else x
}

# Format a number with a fixed number of decimals, tolerating NA.
fmt_num <- function(x, digits = 2L) {
  if (length(x) == 0L || is.na(x)) {
    return("")
  }
  formatC(as.numeric(x), format = "f", digits = digits)
}
