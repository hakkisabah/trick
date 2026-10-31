"""Conversation scope: find the passage in a long chat that the user means.

The catalogue search answers "which package or paper is this". The conversation
search answers the other half of the product: "where in this long chat did we say
that". Both are one pipeline (lexical retrieval, then laya as a tie-breaker), but
the corpora are different enough that they get their own module:

* a chat transcript is small (tens of thousands of words), ephemeral and
  different on every request, so it is indexed in memory and never written to
  the persistent store;
* there is no canonicality prior in a conversation - a chat message is not more
  or less "popular" - so the ranking here is plain BM25 plus the model;
* the unit of the answer is a *passage*, not a document: the caller needs the
  turn number, the role and the character offsets so a UI can highlight it.

Chunking is sentence-aligned and never splits a word: a query term therefore
always lands inside exactly one chunk. Chunks are at most ``MAX_CHARS`` long so
that a 12-way choice question to laya stays inside its context budget; a chunk
that long is also the smallest unit worth showing a user.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Sequence

from .errors import ContractViolation, ResourceExhausted, ScorerUnavailable, TrickError
from .quant import SIG_DIM, feature_hash_signature, sig_cosine

MIN_CHARS = 120
MAX_CHARS = 600
# A transcript bigger than this is refused instead of silently truncated: the
# caller can send a window of the conversation (see ``window_messages``), and
# saying so is better than ranking half a chat and pretending it was all there.
MAX_MESSAGES = 4000
MAX_TOTAL_CHARS = 2_000_000
MAX_CACHED_TRANSCRIPTS = 8
SNIPPET_CHARS = 220
# The same blend as the catalogue search, for the same reason: the model is a
# tie-breaker over a 12-way choice, not a ranker.
DEFAULT_WEIGHTS = (0.2, 0.8)
DEFAULT_GATE = 0.15
SHORTLIST = 12
PREFILTER = 120
# A literal occurrence of the whole query is strong evidence in a conversation
# ("the query is a word the user remembers"), while BM25 alone happily ranks a
# chunk that merely repeats one term. Kept as a multiplier so it cannot rescue a
# chunk that no term matched.
PHRASE_BONUS = 1.25
# Signature cosine above which two passages are treated as the same passage said
# twice. Measured on a 30-turn chat: copies of one repeated sentence score
# 0.944-0.968 against each other, distinct passages 0.08-0.60.
NEAR_DUP_COSINE = 0.94

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?\u2026])\s+|\n{2,}")
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")


@dataclass(frozen=True)
class Message:
    """One turn, already reduced to what the index needs.

    ``mid`` is the id the chat application gave the message. It is carried only
    so a result can point back at the turn it came from (Open WebUI scrolls to
    ``#message-<chat>-<mid>``); the ranking never reads it.
    """

    role: str
    content: str
    mid: str = ""

    def as_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class Chunk:
    chunk_id: int
    turn: int
    role: str
    text: str
    char_start: int
    char_end: int
    signature: bytes
    mid: str = ""


@dataclass(frozen=True)
class ChatRow:
    """One ranked passage, with the provenance of its score."""

    rank: int
    turn: int
    role: str
    text: str
    char_start: int
    char_end: int
    retrieval_score: float
    laya_probability: float | None
    final_score: float
    scored_by_laya: bool
    from_cache: bool = False
    mid: str = ""
    # Which query terms actually occur in this passage. A find result that does
    # not say why it matched reads like a wrong answer ("I asked where the
    # problem was and got a turn that only mentions i5"), so every row carries
    # the terms it shares with the query.
    matched: tuple[str, ...] = ()

    @property
    def score_label(self) -> str:
        return "laya" if self.scored_by_laya else "retrieval-only"

    def as_dict(self) -> dict:
        return {
            "rank": self.rank,
            "turn": self.turn,
            "role": self.role,
            "text": self.text,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "retrieval_score": round(self.retrieval_score, 4),
            "laya_probability": None if self.laya_probability is None else round(self.laya_probability, 4),
            "final_score": round(self.final_score, 4),
            "score_label": self.score_label,
            "scored_by_laya": self.scored_by_laya,
            "from_cache": self.from_cache,
            "message_id": self.mid,
            "matched": list(self.matched),
        }

    @classmethod
    def from_dict(cls, rank: int, item: dict) -> "ChatRow":
        return cls(
            rank=rank,
            turn=int(item.get("turn") or 0),
            role=str(item.get("role") or "user"),
            text=str(item.get("text") or ""),
            char_start=int(item.get("char_start") or 0),
            char_end=int(item.get("char_end") or 0),
            retrieval_score=float(item.get("retrieval_score") or 0.0),
            laya_probability=None if item.get("laya_probability") is None else float(item["laya_probability"]),
            final_score=float(item.get("final_score") or 0.0),
            scored_by_laya=bool(item.get("scored_by_laya")),
            from_cache=bool(item.get("from_cache")),
            mid=str(item.get("message_id") or ""),
            matched=tuple(str(term) for term in (item.get("matched") or ())),
        )


def normalise_messages(raw: Iterable[object]) -> list[Message]:
    """Accept what chat UIs actually send and refuse the rest loudly.

    Open WebUI and MCP clients both hand over ``[{"role":..., "content":...}]``,
    but content may be a string, a list of parts, or ``None``. Anything else is a
    contract violation (S8) rather than an empty search, because a silent empty
    result reads like "the chat does not contain it".
    """
    out: list[Message] = []
    for position, item in enumerate(raw):
        if isinstance(item, Message):
            out.append(item)
            continue
        if isinstance(item, str):
            # A minimal client may send plain strings; treating one as a user
            # turn is the only reading that loses nothing.
            out.append(Message(role="user", content=item))
            continue
        if not isinstance(item, dict):
            raise ContractViolation(f"message {position} is not an object")
        role = str(item.get("role") or "user")
        content = item.get("content")
        if content is None:
            content = ""
        elif isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            content = "\n".join(parts)
        elif not isinstance(content, str):
            raise ContractViolation(f"message {position} has unsupported content type")
        if len(content) > MAX_TOTAL_CHARS:
            raise ContractViolation(f"message {position} is larger than the transcript contract")
        out.append(Message(role=role, content=content, mid=str(item.get("id") or "")))
    if len(out) > MAX_MESSAGES:
        raise ResourceExhausted(f"transcript has {len(out)} messages, limit is {MAX_MESSAGES}")
    total = sum(len(message.content) for message in out)
    if total > MAX_TOTAL_CHARS:
        raise ResourceExhausted(f"transcript has {total} characters, limit is {MAX_TOTAL_CHARS}")
    return out


def window_messages(messages: Sequence[Message], *, tail: int) -> list[Message]:
    """Keep the last ``tail`` messages, which is what a long chat needs.

    A conversation can be longer than the contract above; the answer to that is
    an explicit window, chosen by the caller and reported in the outcome, not a
    silent cut.
    """
    if tail <= 0 or tail >= len(messages):
        return list(messages)
    return list(messages[-tail:])


def fingerprint(messages: Sequence[Message]) -> str:
    """Stable id of a transcript, used as the index and cache key.

    The message id is part of the key even though the ranking never reads it: a
    cached row carries the id of the turn it came from, so two transcripts with
    the same text but different ids must not share an entry.
    """
    digest = hashlib.blake2s(digest_size=12)
    for message in messages:
        digest.update(message.role.encode("utf-8", "replace"))
        digest.update(b"\x1f")
        digest.update(message.mid.encode("utf-8", "replace"))
        digest.update(b"\x1f")
        digest.update(message.content.encode("utf-8", "replace"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def _clean(text: str) -> str:
    return _WHITESPACE.sub(" ", text)


def split_message(message: Message, *, turn: int, next_id: int) -> list[Chunk]:
    """Sentence-aligned chunks, each between ``MIN_CHARS`` and ``MAX_CHARS``."""
    text = message.content
    if not text.strip():
        return []

    pieces: list[tuple[int, int]] = []
    cursor = 0
    for match in _SENTENCE_SPLIT.finditer(text):
        end = match.start()
        if end > cursor:
            pieces.append((cursor, end))
        cursor = match.end()
    if cursor < len(text):
        pieces.append((cursor, len(text)))
    if not pieces:
        pieces = [(0, len(text))]

    start: int | None = None
    end = 0
    spans: list[tuple[int, int]] = []
    for piece_start, piece_end in pieces:
        if start is None:
            start = piece_start
        end = piece_end
        if end - start < MIN_CHARS:
            continue
        spans.append((start, end))
        start = None
    if start is not None:
        spans.append((start, end))

    # A single sentence longer than MAX_CHARS is split on word boundaries so the
    # choice question never receives a candidate it cannot fit. Both passes feed
    # one counter: reusing ``next_id`` here collided with the ids the first pass
    # had already handed out, and a duplicate rowid made the whole transcript
    # index fail to build ("constraint failed") on any chat with a long message.
    ordered: list[tuple[int, int]] = []
    for span_start, span_end in spans:
        # Raw text again: the offsets reported to a UI must index the original
        # message, not the whitespace-collapsed copy that is indexed.
        offset = span_start
        body = text[offset:span_end]
        while body:
            if len(body) <= MAX_CHARS:
                ordered.append((offset, offset + len(body)))
                break
            cut = body.rfind(" ", 0, MAX_CHARS)
            cut = MAX_CHARS if cut <= 0 else cut
            ordered.append((offset, offset + cut))
            offset += cut
            body = text[offset:span_end]

    return [
        _make_chunk(next_id + position, turn, message.role, text, start_at, end_at, message.mid)
        for position, (start_at, end_at) in enumerate(ordered)
    ]


def _make_chunk(
    chunk_id: int, turn: int, role: str, text: str, start: int, end: int, mid: str = ""
) -> Chunk:
    body = _clean(text[start:end]).strip()
    return Chunk(
        chunk_id=chunk_id,
        turn=turn,
        role=role,
        text=body,
        char_start=start,
        char_end=end,
        signature=feature_hash_signature(body),
        mid=mid,
    )


def chunk_messages(messages: Sequence[Message]) -> list[Chunk]:
    chunks: list[Chunk] = []
    # Turns are numbered from 1: the number is shown to a reader ("turn 24"),
    # and a first message labelled "turn 0" reads like a missing value.
    for turn, message in enumerate(messages, start=1):
        chunks.extend(split_message(message, turn=turn, next_id=len(chunks)))
    return [chunk for chunk in chunks if chunk.text]


def snippet(text: str, query: str, *, width: int = SNIPPET_CHARS) -> str:
    """Window of ``text`` around the first query term, for a one-line UI row."""
    lowered = text.lower()
    position = -1
    for term in _terms(query):
        found = lowered.find(term)
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0 or len(text) <= width:
        return text if len(text) <= width else text[: width - 1].rstrip() + "\u2026"
    half = max(1, (width - 20) // 2)
    start = max(0, position - half)
    end = min(len(text), start + width)
    start = max(0, end - width)
    lead = "\u2026" if start else ""
    tail = "\u2026" if end < len(text) else ""
    return f"{lead}{text[start:end].strip()}{tail}"


def _terms(query: str) -> list[str]:
    return [term for term in re.split(r"[^\w]+", query.lower()) if len(term) >= 2]


class TranscriptIndex:
    """In-memory FTS5 index over one transcript. Rebuilt, never persisted."""

    def __init__(self, messages: Sequence[Message]) -> None:
        self.messages = list(messages)
        self.key = fingerprint(self.messages)
        self.chunks = chunk_messages(self.messages)
        self._conn: sqlite3.Connection | None = None
        if self.chunks:
            self._build()

    def _build(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute(
                "CREATE VIRTUAL TABLE chunks USING fts5("
                "body, tokenize=\"unicode61 remove_diacritics 2\")"
            )
            conn.executemany(
                "INSERT INTO chunks (rowid, body) VALUES (?, ?)",
                [(chunk.chunk_id, chunk.text) for chunk in self.chunks],
            )
        except sqlite3.Error as exc:
            conn.close()
            ids = [chunk.chunk_id for chunk in self.chunks]
            duplicates = len(ids) - len(set(ids))
            raise ResourceExhausted(
                f"cannot build the transcript index: {exc} "
                f"(chunks={len(ids)} duplicate_ids={duplicates} "
                f"first={ids[:3]} last={ids[-3:]})"
            ) from exc
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def count(self) -> int:
        return len(self.chunks)

    def bm25(self, query: str, *, limit: int = PREFILTER) -> list[tuple[int, float]]:
        """Rank chunks by BM25, best first, as positive scores."""
        if self._conn is None:
            return []
        expression = _fts_expression(query)
        if not expression:
            return []
        try:
            rows = self._conn.execute(
                "SELECT rowid, bm25(chunks) AS score FROM chunks "
                "WHERE chunks MATCH ? ORDER BY score LIMIT ?",
                (expression, max(1, int(limit))),
            ).fetchall()
        except sqlite3.Error:
            return []
        # bm25() returns negative numbers; flip and shift so the scale is positive
        # and a phrase bonus can never turn a zero into a hit.
        scored = [(int(row[0]), -float(row[1])) for row in rows if float(row[1]) < 0.0]
        return scored

    def candidates(self, query: str, *, limit: int = PREFILTER) -> dict[int, float]:
        """Union of BM25 hits and signature hits, with the phrase bonus applied.

        A signature hit must also share a query term with the passage. The
        signature is 16 bytes of feature hash: on a long conversation its cosine
        reaches 0.25 for passages about something else entirely, and those pure
        hash hits were exactly the irrelevant matches a reader reported ("find
        the phrase" returning a turn about anything). The signature still breaks
        ties among lexically plausible passages, which is what it is for.
        """
        terms = _terms(query)
        lexical = [
            chunk for chunk in self.chunks if any(term in chunk.text.lower() for term in terms)
        ]
        scores: dict[int, float] = {}
        for chunk_id, score in self.bm25(query, limit=limit):
            scores[chunk_id] = max(scores.get(chunk_id, 0.0), score)
        if lexical and terms:
            query_sig = feature_hash_signature(query)
            for chunk in lexical:
                cosine = sig_cosine(query_sig, chunk.signature)
                if cosine > 0.25:
                    scores.setdefault(chunk.chunk_id, cosine)
        phrase = " ".join(query.lower().split())
        if phrase:
            for chunk in self.chunks:
                if chunk.chunk_id in scores and phrase in chunk.text.lower():
                    scores[chunk.chunk_id] *= PHRASE_BONUS
        return scores


def _fts_expression(query: str) -> str:
    terms = _terms(query)
    if not terms:
        return ""
    return " OR ".join('"' + term.replace('"', "") + '"' for term in terms[:24])


class TranscriptCache:
    """Small LRU of built indices, so a follow-up query does not re-index."""

    def __init__(self, capacity: int = MAX_CACHED_TRANSCRIPTS) -> None:
        self.capacity = max(1, int(capacity))
        self._items: dict[str, TranscriptIndex] = {}
        self._order: list[str] = []

    def get(self, messages: Sequence[Message]) -> TranscriptIndex:
        key = fingerprint(messages)
        hit = self._items.pop(key, None)
        if hit is not None:
            self._order.remove(key)
            self._order.append(key)
            self._items[key] = hit
            return hit
        index = TranscriptIndex(messages)
        self._items[key] = index
        self._order.append(key)
        while len(self._order) > self.capacity:
            oldest = self._order.pop(0)
            self._items.pop(oldest).close()
        return index

    def clear(self) -> None:
        for index in self._items.values():
            index.close()
        self._items.clear()
        self._order.clear()

    def __len__(self) -> int:
        return len(self._items)


class ConversationSearch:
    """Retrieve passages, ask laya to break ties, return ranked rows."""

    def __init__(
        self,
        scorer,
        *,
        shortlist: int = SHORTLIST,
        prefilter: int = PREFILTER,
        weights: tuple[float, float] = DEFAULT_WEIGHTS,
        gate_margin: float = DEFAULT_GATE,
        cache: TranscriptCache | None = None,
    ) -> None:
        self.scorer = scorer
        self.shortlist = max(2, min(int(shortlist), 26))
        self.prefilter = max(self.shortlist, int(prefilter))
        total = sum(weights) or 1.0
        self.weights = (weights[0] / total, weights[1] / total)
        self.gate_margin = max(0.0, float(gate_margin))
        self.cache = cache or TranscriptCache()

    def search(
        self,
        query: str,
        messages: Sequence[Message],
        *,
        limit: int = 8,
        index: TranscriptIndex | None = None,
    ) -> tuple[list[ChatRow], dict]:
        """Return ``(rows, report)``. An empty transcript is not an error."""
        query = " ".join(str(query).split())
        if not query:
            raise TrickError("query is empty")
        started = time.monotonic()
        degraded: list[str] = []
        if index is None:
            index = self.cache.get(messages)
        if index.count == 0:
            return [], {
                "turn_count": len(messages),
                "chunk_count": 0,
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                "reason": "no text in the transcript window",
            }

        stage = time.monotonic()
        candidates = index.candidates(query, limit=self.prefilter)
        bm25_ms = (time.monotonic() - stage) * 1000.0
        if not candidates:
            return [], {
                "turn_count": len(messages),
                "chunk_count": index.count,
                "candidates": 0,
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                "timings": {"bm25_ms": round(bm25_ms, 1)},
                "reason": "no lexical match",
            }

        by_id = {chunk.chunk_id: chunk for chunk in index.chunks}
        ordered = sorted(candidates, key=lambda cid: (-candidates[cid], cid))[: self.shortlist]
        ordered = [cid for cid in ordered if cid in by_id]

        stage = time.monotonic()
        probabilities: dict[int, float] = {}
        scored = False
        gated = self._gate_closed({cid: candidates[cid] for cid in ordered})
        if len(ordered) >= 2 and not gated:
            texts = [by_id[cid].text for cid in ordered]
            try:
                ranked = self.scorer.rank(query, texts)
            except (ScorerUnavailable, TrickError) as exc:
                degraded.append(exc.s_code)
            else:
                scored = True
                for item in ranked:
                    if 0 <= item.index < len(ordered):
                        probabilities[ordered[item.index]] = item.probability
        laya_ms = (time.monotonic() - stage) * 1000.0

        model_weight, retrieval_weight = self.weights
        terms = _terms(query)
        rows: list[ChatRow] = []
        blended: list[tuple[float, ChatRow, Chunk]] = []
        for cid in ordered:
            chunk = by_id[cid]
            retrieval = candidates[cid]
            probability = probabilities.get(cid)
            final = retrieval_weight * retrieval
            if probability is not None:
                final += model_weight * probability
            blended.append(
                (
                    final,
                    ChatRow(
                        rank=0,
                        turn=chunk.turn,
                        role=chunk.role,
                        text=snippet(chunk.text, query),
                        char_start=chunk.char_start,
                        char_end=chunk.char_end,
                        retrieval_score=retrieval,
                        laya_probability=probability,
                        final_score=final,
                        scored_by_laya=scored,
                        mid=chunk.mid,
                        matched=tuple(term for term in terms if term in chunk.text.lower())[:6],
                    ),
                    chunk,
                )
            )
        blended.sort(key=lambda item: (-item[0], item[1].turn, item[1].char_start))
        # A long chat repeats itself: the same sentence comes back in several
        # turns ("let us check the PCA" appears four times). Showing it four times
        # spends the reader's attention on one passage, so repeats collapse to
        # their best-ranked occurrence. Two tests, because exact equality is free
        # and near-equality is what real chats produce:
        #
        #   * identical after case/whitespace normalisation -> collapse;
        #   * signature cosine >= NEAR_DUP_COSINE with a kept passage -> collapse.
        #
        # The threshold is measured, not guessed: on a 30-turn chat the copies of
        # one repeated sentence scored 0.944-0.968 against each other while
        # distinct passages scored 0.08-0.60 (same-stem filler groups reached
        # 0.885-0.923, and those are repeats too - they should collapse). 0.94
        # sits in the gap. Near-duplicate work is capped by the shortlist (<= 12
        # passages, <= 66 comparisons), so it cannot become a cost.
        collapsed = 0
        seen: set[str] = set()
        kept_chunks: list[Chunk] = []
        for _, row, chunk in blended:
            key = " ".join(row.text.lower().split())
            if key in seen:
                collapsed += 1
                continue
            if any(sig_cosine(chunk.signature, other.signature) >= NEAR_DUP_COSINE for other in kept_chunks):
                collapsed += 1
                continue
            seen.add(key)
            kept_chunks.append(chunk)
            rows.append(
                ChatRow(
                    rank=len(rows) + 1,
                    turn=row.turn,
                    role=row.role,
                    text=row.text,
                    char_start=row.char_start,
                    char_end=row.char_end,
                    retrieval_score=row.retrieval_score,
                    laya_probability=row.laya_probability,
                    final_score=row.final_score,
                    scored_by_laya=row.scored_by_laya,
                    mid=row.mid,
                    matched=row.matched,
                )
            )
            if len(rows) >= max(1, int(limit)):
                break

        report = {
            "turn_count": len(messages),
            "chunk_count": index.count,
            "candidates": len(candidates),
            "shortlist": len(ordered),
            "scored_by_laya": scored,
            "gated": gated,
            "collapsed_duplicates": collapsed,
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
            "timings": {"bm25_ms": round(bm25_ms, 1), "laya_ms": round(laya_ms, 1)},
            "degraded": sorted(set(degraded)),
        }
        return rows, report

    def _gate_closed(self, scores: dict[int, float]) -> bool:
        if self.gate_margin <= 0.0 or len(scores) < 2:
            return False
        ordered = sorted(scores.values(), reverse=True)
        best, second = ordered[0], ordered[1]
        if best <= 0.0:
            return False
        return (best - second) / best >= self.gate_margin


__all__ = [
    "ChatRow",
    "Chunk",
    "ConversationSearch",
    "Message",
    "TranscriptCache",
    "TranscriptIndex",
    "chunk_messages",
    "fingerprint",
    "normalise_messages",
    "snippet",
    "window_messages",
]
