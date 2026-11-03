#!/usr/bin/env python3
"""Cross-language parity: the R package must do what this package does.

The R package claims to be the same find, reimplemented: same passage
boundaries, same ranking behaviour, same on-disk score matrix. A claim is not a
measurement, so this script builds one fixture, runs it through both
implementations and compares:

  1. chunks   same count, same turns, same message ids, same text
  2. ranking  same ordered message ids for queries with a clear lexical winner
  3. matrix   Python writes and R reads it; R writes and the bytes are identical

It prints "parity skipped: no Rscript" and exits 0 when R is not installed, so
a host without R still gets a green gate. Set TRICK_RSCRIPT to point at a
specific interpreter.

Wire it into scripts/check.sh (gate 17).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Turkish text is written with escapes on purpose: the repository is ASCII-only
# (scripts/language_gate.py), and a parity fixture must still carry the letters
# whose folding the two implementations have to agree on.
MESSAGES = [
    {"id": "m1", "role": "user", "content": "Where is the taper set for low count genes?"},
    {
        "id": "m2",
        "role": "assistant",
        "content": (
            "Esik degeri 0.15 olarak ayarlandi. "
            "\u0130ki tur boyunca ayni sonucu aldik; dusuk sayimli genler icin "
            "\u00e7izimde bir daralma var."
        ),
    },
    {"id": "m3", "role": "user", "content": "Peki bellek limiti neydi?"},
    {
        "id": "m4",
        "role": "assistant",
        "content": (
            "The container hit MemoryMax during the build. "
            "The scheduler retried twice, then gave up."
        ),
    },
    {"id": "m5", "role": "user", "content": "Yasadigimiz yer neresi?"},
    {
        "id": "m6",
        "role": "assistant",
        "content": (
            "Ya\u015fad\u0131\u011f\u0131m\u0131z sorun bellek limitiydi: "
            "derleme s\u0131ras\u0131nda konteyner s\u0131n\u0131ra dayand\u0131."
        ),
    },
]

# Distinctive tokens only. An ambiguous query would compare two different BM25
# implementations on a tie, which measures nothing.
QUERIES = ["MemoryMax", "yasadigimiz", "taper"]

MATRIX_IDS = [101, 202, 303]
MATRIX_SIGNATURES = [
    [0.5] * 16,
    [1.0] + [0.0] * 15,
    [0.0] * 16,
]
MATRIX_SCORES = [0.0, 0.5, 1.0]
MATRIX_FLAGS = [0, 1, 2]


def find_rscript() -> str | None:
    explicit = os.environ.get("TRICK_RSCRIPT", "").strip()
    if explicit:
        return explicit if Path(explicit).exists() else None
    for candidate in (
        ROOT.parent / "trick-r-env" / "bin" / "Rscript",
        ROOT / ".venv-r" / "bin" / "Rscript",
    ):
        if candidate.exists():
            return str(candidate)
    return shutil.which("Rscript")


def r_string(text: str) -> str:
    """One R string literal, ASCII source, real characters at run time."""
    out = ['"']
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ord(ch) < 32:
            out.append("\\x%02x" % ord(ch))
        elif ord(ch) > 126:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def build_driver(fixture: Path, r_write: Path) -> str:
    lines = [
        "library(trick)",
        "messages <- list(" + ",".join(
            "list(id=%s, role=%s, content=%s)"
            % (r_string(m["id"]), r_string(m["role"]), r_string(m["content"]))
            for m in MESSAGES
        ) + ")",
        "chunks <- trick_chunks(messages)",
        'cat(sprintf("CHUNKS\\t%d\\n", length(chunks)))',
        "for (i in seq_along(chunks)) {",
        '  cat(sprintf("CHUNKROW\\t%d\\t%d\\t%s\\t%s\\n", i, chunks[[i]]$turn,'
        ' chunks[[i]]$message_id, gsub(\"[\\t\\n]\", \" \", chunks[[i]]$text)))',
        "}",
        "index <- trick_index(messages)",
    ]
    for position, query in enumerate(QUERIES):
        lines.extend(
            [
                "hits <- trick_search(index, %s, limit = 3L)" % r_string(query),
                'cat(sprintf("QUERY\\t%%d\\t%%d\\n", %d, nrow(hits)))' % position,
                "if (nrow(hits) > 0) {",
                "  for (i in seq_len(nrow(hits))) {",
                '    cat(sprintf("HITROW\\t%%d\\t%%d\\t%%s\\n", %d, i, hits$message_id[i]))' % position,
                "  }",
                "}",
            ]
        )
    lines.extend(
        [
            "md <- trick_matrix_read(%s)" % r_string(str(fixture)),
            'cat(sprintf("MATRIX\\t%d\\t%s\\t%s\\n", md$count,'
            ' tolower(as.character(md$synthetic)), tolower(as.character(md$crc32_ok))))',
            "for (i in seq_len(md$count)) {",
            '  cat(sprintf("MATRIXROW\\t%d\\t%.10f\\n", md$records$id[i], md$records$score[i]))',
            "}",
            "sig <- matrix(c(%s), nrow = %d, byrow = TRUE)"
            % (", ".join("%.6f" % value for row in MATRIX_SIGNATURES for value in row), len(MATRIX_SIGNATURES)),
            "trick_matrix_write(%s, id = c(%s), signature = sig, score = c(%s),"
            " flags = c(%s), synthetic = TRUE)"
            % (
                r_string(str(r_write)),
                ", ".join(str(value) for value in MATRIX_IDS),
                ", ".join("%.6f" % value for value in MATRIX_SCORES),
                ", ".join(str(value) for value in MATRIX_FLAGS),
            ),
            'cat("RWRITE\\tok\\n")',
        ]
    )
    return "\n".join(lines) + "\n"


def python_side(fixture: Path) -> dict:
    from trick.matrix import HF_SYNTHETIC, MatrixIndex, MatrixWriter
    from trick.transcript import Message, chunk_messages

    messages = [Message(role=m["role"], content=m["content"], mid=m["id"]) for m in MESSAGES]
    chunks = chunk_messages(messages)
    chunk_rows = [
        {
            "turn": chunk.turn,
            "mid": chunk.mid,
            "text": " ".join(str(chunk.text).replace("\t", " ").splitlines()),
        }
        for chunk in chunks
    ]

    import tempfile as _tempfile

    from trick.search import SearchEngine
    from trick.textindex import DocumentStore

    with _tempfile.TemporaryDirectory() as tmp:
        store = DocumentStore(str(Path(tmp) / "parity.db"))
        try:
            engine = SearchEngine(store, use_cache=False)
            rankings = {}
            for query in QUERIES:
                outcome = engine.search(
                    query,
                    limit=3,
                    messages=[{"id": m["id"], "role": m["role"], "content": m["content"]} for m in MESSAGES],
                    scope="chat",
                )
                rankings[query] = [row.mid for row in outcome.chat]
        finally:
            store.close()

    with MatrixWriter(str(fixture), header_flags=HF_SYNTHETIC) as writer:
        for record_id, signature, score, flags in zip(
            MATRIX_IDS, MATRIX_SIGNATURES, MATRIX_SCORES, MATRIX_FLAGS
        ):
            writer.add(record_id, signature, score=score, flags=flags)

    index = MatrixIndex(str(fixture))
    index.verify()
    matrix_rows = [(record_id, round(score, 4)) for record_id, _sig, score, _flags in index.iter_records()]
    return {
        "chunks": chunk_rows,
        "rankings": rankings,
        "matrix": {"count": index.count, "synthetic": index.synthetic, "rows": matrix_rows},
    }


def parse_driver(stdout: str) -> dict:
    chunks: list[dict] = []
    rankings: dict[int, list[str]] = {}
    matrix = {"count": 0, "synthetic": False, "rows": []}
    rwrote = False
    for line in stdout.splitlines():
        parts = line.split("\t")
        if parts[0] == "CHUNKROW":
            chunks.append({"turn": int(parts[2]), "mid": parts[3], "text": parts[4]})
        elif parts[0] == "QUERY":
            rankings[int(parts[1])] = []
        elif parts[0] == "HITROW":
            rankings.setdefault(int(parts[1]), []).append(parts[3])
        elif parts[0] == "MATRIX":
            matrix["count"] = int(parts[1])
            matrix["synthetic"] = parts[2] == "true"
            matrix["crc32_ok"] = parts[3] == "true"
        elif parts[0] == "MATRIXROW":
            matrix["rows"].append((int(parts[1]), round(float(parts[2]), 4)))
        elif parts[0] == "RWRITE":
            rwrote = True
    return {"chunks": chunks, "rankings": rankings, "matrix": matrix, "rwrote": rwrote}


def compare(expected: dict, actual: dict, problems: list[str]) -> None:
    py_chunks, r_chunks = expected["chunks"], actual["chunks"]
    if len(py_chunks) != len(r_chunks):
        problems.append(f"chunk count differs: python {len(py_chunks)}, R {len(r_chunks)}")
    for position, (left, right) in enumerate(zip(py_chunks, r_chunks), start=1):
        if left["turn"] != right["turn"] or left["mid"] != right["mid"]:
            problems.append(
                f"chunk {position} points at a different turn: "
                f"python turn {left['turn']}/{left['mid']}, R turn {right['turn']}/{right['mid']}"
            )
        if left["text"].strip() != right["text"].strip():
            problems.append(
                f"chunk {position} text differs: python {left['text'][:60]!r}, R {right['text'][:60]!r}"
            )

    for position, query in enumerate(QUERIES):
        left = expected["rankings"].get(query, [])
        right = actual["rankings"].get(position, [])
        if left != right:
            problems.append(f"ranking differs for {query!r}: python {left}, R {right}")

    if not actual["matrix"]["crc32_ok"]:
        problems.append("the R reader did not accept the Python file's checksum")
    if actual["matrix"]["count"] != expected["matrix"]["count"]:
        problems.append(
            f"matrix count differs: python {expected['matrix']['count']}, R {actual['matrix']['count']}"
        )
    if not actual["matrix"]["synthetic"]:
        problems.append("the R reader lost the synthetic flag")
    for left, right in zip(expected["matrix"]["rows"], actual["matrix"]["rows"]):
        if left[0] != right[0] or abs(left[1] - right[1]) > 1e-3:
            problems.append(f"matrix record differs: python {left}, R {right}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare the R package with the Python engine")
    parser.add_argument("--json", action="store_true", help="print the measurement as JSON")
    args = parser.parse_args()

    rscript = find_rscript()
    if not rscript:
        print("parity skipped: no Rscript (set TRICK_RSCRIPT to run it)")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        fixture = work / "parity.trk"
        r_write = work / "from-r.trk"
        driver = work / "driver.R"

        expected = python_side(fixture)
        driver.write_text(build_driver(fixture, r_write), encoding="utf-8")

        env = dict(os.environ)
        env["R_LIBS_USER"] = env.get("R_LIBS_USER", "")
        proc = subprocess.run(
            [rscript, "--vanilla", str(driver)],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        if proc.returncode != 0:
            print(proc.stdout[-2000:])
            print(proc.stderr[-2000:], file=sys.stderr)
            print("parity FAILED: the R driver did not run", file=sys.stderr)
            return 2

        actual = parse_driver(proc.stdout)
        problems: list[str] = []
        compare(expected, actual, problems)

        if not actual["rwrote"]:
            problems.append("the R package did not write a matrix")
        elif r_write.exists():
            if r_write.read_bytes() != fixture.read_bytes():
                problems.append("the R writer produced different bytes than the Python writer")

        if problems:
            for problem in problems:
                print("parity: " + problem, file=sys.stderr)
            return 1

        if args.json:
            print(json.dumps({"chunks": len(expected["chunks"]), "queries": QUERIES, "ok": True}))
        else:
            print(
                f"parity ok: {len(expected['chunks'])} chunks, "
                f"{len(QUERIES)} rankings identical, matrix byte-identical in both directions"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
