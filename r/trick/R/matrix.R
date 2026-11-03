# The score matrix: a fixed-record file both Trick implementations can read.
#
# Layout (little endian), identical to the Python writer:
#
#   offset 0   48 byte header
#     magic      8 raw   "TRKMAT\001\000"
#     version    u16     1
#     dim        u8      16        signature components
#     rec_size   u8      24        bytes per record
#     count      u32     record count
#     crc32      u32     CRC32 of the body
#     flags      u32     bit 0: synthetic content
#     qscale     f32     127.0
#     pad                zero filled to 48 bytes
#   offset 48  count * 24 byte records
#     id         u32
#     sig        16 raw  int8 unit-vector signature
#     score      u16     0..65535 mapped to 0..1
#     flags      u8
#     pad        1 byte
#
# The record is 24 bytes because the target host is the cheapest one in the
# fleet: 24 MB per million passages. Sixteen dimensions is a deliberate loss of
# fidelity, recovered by rescoring the shortlist with the text index rather than
# by spending RAM.

TRK_MAGIC <- as.raw(c(0x54, 0x52, 0x4b, 0x4d, 0x41, 0x54, 0x01, 0x00)) # TRKMAT
TRK_VERSION <- 1L
TRK_DIM <- 16L
TRK_RECORD_SIZE <- 24L
TRK_HEADER_SIZE <- 48L
TRK_QSCALE <- 127
TRK_HF_SYNTHETIC <- 1L

# Four little-endian bytes for a non-negative number, without relying on
# 32-bit integer arithmetic (R integers are signed and a CRC overflows them).
u32_le <- function(value) {
  value <- as.numeric(value)
  as.raw(c(
    value %% 256,
    (value %/% 256) %% 256,
    (value %/% 65536) %% 256,
    (value %/% 16777216) %% 256
  ))
}

# Read four little-endian bytes as a double, so values above 2^31 stay exact.
read_u32_le <- function(con) {
  bytes <- as.numeric(readBin(con, "raw", n = 4L, size = 1L))
  bytes[1] + bytes[2] * 256 + bytes[3] * 65536 + bytes[4] * 16777216
}

# CRC32 (IEEE 802.3) over a raw vector.
#
# R has no unsigned 32-bit integers and no dependency-free bitwise XOR for them,
# so a 32-bit XOR is built from two 16-bit ones: each half fits in a signed
# integer, and bitwXor is exact there.
xor32 <- function(a, b) {
  a <- floor(as.numeric(a))
  b <- floor(as.numeric(b))
  hi <- bitwXor(as.integer(floor(a / 65536)), as.integer(floor(b / 65536)))
  lo <- bitwXor(as.integer(a %% 65536), as.integer(b %% 65536))
  as.numeric(hi) * 65536 + as.numeric(lo)
}

crc_table <- local({
  poly <- 3988292384
  table <- numeric(256)
  for (i in 0:255) {
    crc <- i
    for (bit in 1:8) {
      if (crc %% 2 == 1) {
        crc <- xor32(floor(crc / 2), poly)
      } else {
        crc <- floor(crc / 2)
      }
    }
    table[i + 1] <- crc
  }
  table
})

# The body is written once and is a few hundred kilobytes, so one table lookup
# per byte in plain R is fast enough that no compiled code is worth adding.
trk_crc32_bytes <- function(bytes) {
  if (length(bytes) == 0L) {
    return(0)
  }
  crc <- 4294967295
  for (byte in as.numeric(bytes)) {
    index <- bitwXor(as.integer(crc %% 256), as.integer(byte)) + 1L
    crc <- xor32(floor(crc / 256), crc_table[index])
  }
  xor32(crc, 4294967295)
}

#' Quantise one vector into a 16 byte signature
#'
#' Normalises the vector to unit length and stores each component as a signed
#' byte scaled by 127. Two signatures are compared with [trick_sig_cosine()].
#'
#' @param vector Numeric vector of length 16.
#' @return A raw vector of length 16.
#' @examples
#' sig <- trick_signature(c(1, rep(0, 15)))
#' length(sig)
#' @export
trick_signature <- function(vector) {
  if (!is.numeric(vector) || length(vector) != TRK_DIM) {
    stop(sprintf("a signature needs %d numeric components", TRK_DIM), call. = FALSE)
  }
  norm <- sqrt(sum(vector^2))
  if (!is.finite(norm) || norm == 0) {
    return(as.raw(rep(0L, TRK_DIM)))
  }
  scaled <- round(vector / norm * TRK_QSCALE)
  scaled[scaled > 127] <- 127
  scaled[scaled < -127] <- -127
  as.raw(ifelse(scaled < 0, scaled + 256, scaled))
}

# Signed value of one stored byte.
signed_byte <- function(value) {
  ifelse(value > 127, value - 256, value)
}

#' Cosine similarity of two stored signatures
#'
#' The dot product of the int8 signatures divided by the square of the
#' quantisation scale, clamped to `[-1, 1]`.
#'
#' @param a, b Raw vectors of length 16, from [trick_signature()].
#' @return A single number.
#' @examples
#' trick_sig_cosine(trick_signature(c(1, rep(0, 15))),
#'                  trick_signature(c(1, rep(0, 15))))
#' @export
trick_sig_cosine <- function(a, b) {
  if (length(a) != length(b)) {
    stop("signatures differ in length", call. = FALSE)
  }
  dot <- sum(signed_byte(as.numeric(a)) * signed_byte(as.numeric(b)))
  value <- dot / (TRK_QSCALE^2)
  max(-1, min(1, value))
}

#' Write a score matrix
#'
#' Writes the fixed-record matrix read by [trick_matrix_read()] and by the
#' companion Python implementation. The write is atomic: the file is written
#' beside the target and renamed, so a crash leaves either the old matrix or the
#' new one.
#'
#' @param path Destination path.
#' @param id Integer vector of record ids.
#' @param signature A list of raw vectors, or a numeric matrix with 16 columns,
#'   one row per record.
#' @param score Numeric vector in `[0, 1]`.
#' @param flags Optional integer vector of per-record flags.
#' @param synthetic `TRUE` when the content is not a learned embedding; sets the
#'   header flag so a reader can say so.
#' @return The path, invisibly.
#' @examples
#' path <- tempfile(fileext = ".trk")
#' trick_matrix_write(path, id = 1:2,
#'                    signature = rbind(rep(0.1, 16), rep(0.2, 16)),
#'                    score = c(0.5, 0.6))
#' unlink(path)
#' @export
trick_matrix_write <- function(path, id, signature, score, flags = NULL,
                               synthetic = FALSE) {
  n <- length(id)
  if (n != length(score)) {
    stop("id and score must have the same length", call. = FALSE)
  }
  if (is.matrix(signature)) {
    if (ncol(signature) != TRK_DIM || nrow(signature) != n) {
      stop(sprintf("signature must be a %d column matrix with one row per record", TRK_DIM),
           call. = FALSE)
    }
    signature <- lapply(seq_len(n), function(i) trick_signature(signature[i, ]))
  }
  if (length(signature) != n) {
    stop("one signature per record is required", call. = FALSE)
  }
  if (is.null(flags)) {
    flags <- rep(0L, n)
  }
  if (length(flags) != n) {
    stop("flags must have one value per record", call. = FALSE)
  }
  score <- as.numeric(score)
  if (any(is.na(score)) || any(score < 0) || any(score > 1)) {
    stop("score must be in [0, 1]", call. = FALSE)
  }

  body <- raw(n * TRK_RECORD_SIZE)
  position <- 1L
  for (i in seq_len(n)) {
    encoded <- round(score[i] * 65535)
    record <- c(
      u32_le(id[i]),
      signature[[i]],
      u32_le(encoded)[1:2],
      as.raw(as.integer(flags[i]) %% 256),
      as.raw(0L)
    )
    body[position:(position + TRK_RECORD_SIZE - 1L)] <- record
    position <- position + TRK_RECORD_SIZE
  }

  header <- c(
    TRK_MAGIC,
    u32_le(TRK_VERSION)[1:2],
    as.raw(TRK_DIM),
    as.raw(TRK_RECORD_SIZE),
    u32_le(n),
    u32_le(trk_crc32_bytes(body)),
    u32_le(if (isTRUE(synthetic)) TRK_HF_SYNTHETIC else 0),
    writeBin(as.numeric(TRK_QSCALE), raw(), size = 4L, endian = "little")
  )
  header <- c(header, raw(TRK_HEADER_SIZE - length(header)))

  temporary <- paste0(path, ".tmp")
  connection <- file(temporary, "wb")
  on.exit(close(connection), add = TRUE)
  writeBin(c(header, body), connection)
  close(connection)
  on.exit(NULL, add = FALSE)
  if (!file.rename(temporary, path)) {
    unlink(temporary)
    stop("could not move the matrix into place", call. = FALSE)
  }
  invisible(path)
}

#' Read a score matrix
#'
#' Reads the fixed-record matrix written by [trick_matrix_write()] or by the
#' companion Python implementation. A truncated, corrupted or foreign file is
#' refused with a message naming what was found, never returned half-read.
#'
#' @param path Path to the matrix.
#' @return A list with `count`, `dim`, `qscale`, `synthetic`, `crc32_ok` and
#'   `records` (a data frame with `id`, `score`, `flags` and a list column of
#'   signatures).
#' @examples
#' path <- tempfile(fileext = ".trk")
#' trick_matrix_write(path, id = 1:2, signature = rbind(rep(0, 16), rep(0, 16)),
#'                    score = c(0.1, 0.2))
#' trick_matrix_read(path)$count
#' unlink(path)
#' @export
trick_matrix_read <- function(path) {
  if (!file.exists(path)) {
    stop("no score matrix at ", path, call. = FALSE)
  }
  size <- file.info(path)$size
  if (is.na(size) || size < TRK_HEADER_SIZE) {
    stop("the score matrix is shorter than its header", call. = FALSE)
  }
  connection <- file(path, "rb")
  on.exit(close(connection), add = TRUE)
  header <- readBin(connection, "raw", n = TRK_HEADER_SIZE, size = 1L)

  magic <- header[1:8]
  if (!identical(magic[1:6], TRK_MAGIC[1:6]) || !identical(magic[7], as.raw(1))) {
    stop("not a Trick score matrix: the magic bytes do not match", call. = FALSE)
  }
  version <- as.numeric(header[9]) + as.numeric(header[10]) * 256
  dim <- as.numeric(header[11])
  record_size <- as.numeric(header[12])
  count <- as.numeric(header[13]) + as.numeric(header[14]) * 256 +
    as.numeric(header[15]) * 65536 + as.numeric(header[16]) * 16777216
  crc <- as.numeric(header[17]) + as.numeric(header[18]) * 256 +
    as.numeric(header[19]) * 65536 + as.numeric(header[20]) * 16777216
  flags <- as.numeric(header[21]) + as.numeric(header[22]) * 256 +
    as.numeric(header[23]) * 65536 + as.numeric(header[24]) * 16777216
  qscale <- readBin(header[25:28], "double", n = 1L, size = 4L, endian = "little")
  if (length(qscale) == 0L) {
    qscale <- TRK_QSCALE
  }

  if (version != TRK_VERSION) {
    stop(sprintf("matrix version %d written by another release", version), call. = FALSE)
  }
  if (dim != TRK_DIM || record_size != TRK_RECORD_SIZE) {
    stop(sprintf("matrix geometry %d x %d is not %d x %d", dim, record_size,
                 TRK_DIM, TRK_RECORD_SIZE), call. = FALSE)
  }
  expected <- TRK_HEADER_SIZE + count * TRK_RECORD_SIZE
  if (size < expected) {
    stop(sprintf("the score matrix is truncated: %d bytes, %d expected", size, expected),
         call. = FALSE)
  }

  body <- readBin(connection, "raw", n = count * TRK_RECORD_SIZE, size = 1L)
  crc_ok <- isTRUE(all.equal(as.numeric(crc), as.numeric(trk_crc32_bytes(body)),
                             tolerance = 0))

  ids <- numeric(count)
  scores <- numeric(count)
  rec_flags <- numeric(count)
  signatures <- vector("list", count)
  for (i in seq_len(count)) {
    start <- (i - 1L) * TRK_RECORD_SIZE + 1L
    record <- body[start:(start + TRK_RECORD_SIZE - 1L)]
    ids[i] <- as.numeric(record[1]) + as.numeric(record[2]) * 256 +
      as.numeric(record[3]) * 65536 + as.numeric(record[4]) * 16777216
    signatures[[i]] <- record[5:20]
    scores[i] <- (as.numeric(record[21]) + as.numeric(record[22]) * 256) / 65535
    rec_flags[i] <- as.numeric(record[23])
  }

  records <- data.frame(id = ids, score = scores, flags = rec_flags,
                        stringsAsFactors = FALSE)
  records$signature <- signatures

  list(
    count = count,
    dim = dim,
    qscale = qscale,
    synthetic = bitwAnd(as.integer(flags), TRK_HF_SYNTHETIC) != 0L,
    crc32_ok = crc_ok,
    records = records
  )
}
