"""laya scorer client.

laya is not a search engine: it is a single-pass decision scorer. Trick therefore
retrieves candidates locally (see :mod:`trick.retrieval`) and asks laya one
question per query:

    "here are N candidate sources, which one best answers the state?"

That is a ``choice`` question whose ``criteria`` are the candidates. The endpoint
returns one probability per candidate, which is exactly the graded relevance a
ranking needs, in a single round trip (measured ~0.23 s for 12 candidates).

Two properties matter for correctness:

* **opaque labels.** laya's choice head has a measurable label-position bias, so
  candidates are shown as ``A``..``Z`` in a shuffled order derived from the query
  hash, and the probabilities are mapped back to the caller's indices. The
  shuffle is deterministic per query, so runs are reproducible and tests can
  assert on it.
* **one degraded mode, many causes.** every transport or protocol failure
  becomes ``ScorerUnavailable`` with a ``reason``, so the engine can fall back to
  retrieval order and the UI can say *why* no score is shown.

Nothing in this module logs, prints or raises the API key.
"""

from __future__ import annotations

import hashlib
import json
import random
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, Sequence

from .credentials import LayaCredentials, load_laya_credentials
from .errors import ConfigError, ContractViolation, ScorerUnavailable

MAX_LABELS = 26
LABEL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
DEFAULT_MAX_CANDIDATES = 12
DEFAULT_MAX_PROFILE_CHARS = 320
# Measured on scripts/eval_ranking.py: a short instruction ranks better than a
# long one (nDCG@10 0.760 vs 0.650), and a long "be expert" prompt adds nothing.
DEFAULT_INSTRUCTIONS = "Which source answers the state best?"


@dataclass(frozen=True)
class Ranked:
    """One candidate's probability, addressed by its index in the caller's list."""

    index: int
    probability: float


@dataclass
class ScorerStats:
    """Cheap counters used by the CLI and the cost report."""

    calls: int = 0
    failures: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_latency_ms: float = 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "retries": self.retries,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "last_latency_ms": round(self.last_latency_ms, 1),
        }


class Scorer(Protocol):
    """What the search engine needs from any scorer implementation."""

    name: str

    def rank(self, query: str, candidates: Sequence[str], instructions: str = ...) -> list[Ranked]:
        """Return candidates ordered best-first with probabilities in [0, 1]."""
        ...


def _profile(text: str, limit: int) -> str:
    """Collapse whitespace and bound the text so a batch stays small."""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "\u2026"


def label_order(query: str, count: int, salt: str = "trick") -> list[int]:
    """Deterministic shuffle: candidate index -> display position.

    Built from a hash of the query and the salt, not from ``hash()``, so it is
    stable across processes and platforms.
    """
    if count > MAX_LABELS:
        raise ContractViolation(f"at most {MAX_LABELS} candidates per question, got {count}")
    seed = hashlib.blake2s(f"{salt}\x00{query}".encode("utf-8"), digest_size=8).digest()
    order = list(range(count))
    random.Random(int.from_bytes(seed, "big")).shuffle(order)
    return order


class LayaClient:
    """HTTP client for the laya batch endpoint (``/v1/systemone/batch``)."""

    name = "laya-remote"

    def __init__(
        self,
        credentials: LayaCredentials | None = None,
        *,
        opener: Callable[[urllib.request.Request, float], bytes] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = 2,
        backoff_s: float = 0.25,
        breaker_threshold: int = 5,
        breaker_cooldown_s: float = 30.0,
        label_salt: str = "trick",
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        max_profile_chars: int = DEFAULT_MAX_PROFILE_CHARS,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.credentials = credentials or load_laya_credentials()
        if not self.credentials.configured:
            raise ConfigError("laya credentials are empty")
        self._opener = opener or self._urllib_opener
        self._clock = clock
        self._sleep = sleep
        self._retries = max(0, retries)
        self._backoff_s = backoff_s
        self._breaker_threshold = max(1, breaker_threshold)
        self._breaker_cooldown_s = breaker_cooldown_s
        self._label_salt = label_salt
        self._max_candidates = min(max_candidates, MAX_LABELS)
        self._max_profile_chars = max(40, max_profile_chars)
        self._jitter = jitter
        self.stats = ScorerStats()
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        self._last_usage: dict[str, object] = {}

    # -- transport ---------------------------------------------------------
    @staticmethod
    def _urllib_opener(request: urllib.request.Request, timeout: float) -> bytes:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def _build_request(self, payload: Mapping[str, object]) -> urllib.request.Request:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return urllib.request.Request(
            self.credentials.base_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.credentials.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "trick/0.1",
            },
            method="POST",
        )

    def _breaker_blocked(self) -> bool:
        if self._breaker_open_until <= 0.0:
            return False
        if self._clock() >= self._breaker_open_until:
            self._breaker_open_until = 0.0
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        self.stats.failures += 1
        if self._consecutive_failures >= self._breaker_threshold:
            self._breaker_open_until = self._clock() + self._breaker_cooldown_s

    def _post(self, payload: Mapping[str, object]) -> dict:
        """POST with bounded retries; every path ends in data or S5."""
        if self._breaker_blocked():
            raise ScorerUnavailable("circuit-open", "too many recent failures, cooling down")

        request = self._build_request(payload)
        last_reason = "unreachable"
        last_detail = ""
        attempt = 0
        while True:
            started = self._clock()
            try:
                raw = self._opener(request, self.credentials.timeout_s)
            except urllib.error.HTTPError as exc:
                code = int(exc.code)
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except Exception:  # noqa: BLE001 - body is best effort
                    pass
                detail = self.credentials.redact(detail)
                if code in (401, 403):
                    self._record_failure()
                    raise ScorerUnavailable("auth", f"HTTP {code}: {detail}") from exc
                if code == 429:
                    last_reason, last_detail = "rate-limited", detail
                    retry_after = _retry_after_seconds(exc.headers)
                elif code >= 500:
                    last_reason, last_detail = f"http-{code}", detail
                    retry_after = None
                else:
                    # 4xx other than auth/429 is a contract problem, not a blip.
                    self._record_failure()
                    raise ScorerUnavailable("bad-request", f"HTTP {code}: {detail}") from exc
                self.stats.retries += 1
                if attempt >= self._retries:
                    self._record_failure()
                    raise ScorerUnavailable(last_reason, last_detail) from exc
                attempt += 1
                self._sleep(self._backoff(attempt, retry_after))
                continue
            except (socket.timeout, TimeoutError) as exc:
                last_reason, last_detail = "timeout", str(exc)
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, (socket.timeout, TimeoutError)):
                    last_reason = "timeout"
                else:
                    last_reason = "unreachable"
                last_detail = str(reason)
            except OSError as exc:
                last_reason, last_detail = "unreachable", str(exc)
            else:
                self.stats.last_latency_ms = (self._clock() - started) * 1000.0
                self._record_success()
                self.stats.calls += 1
                return self._decode(raw)
            self.stats.retries += 1
            if attempt >= self._retries:
                self._record_failure()
                raise ScorerUnavailable(last_reason, last_detail)
            attempt += 1
            self._sleep(self._backoff(attempt, None))

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(max(retry_after, 0.0), 5.0)
        base = self._backoff_s * (2 ** (attempt - 1))
        return base + base * 0.25 * self._jitter()

    def _decode(self, raw: bytes) -> dict:
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ScorerUnavailable("invalid-response", "body is not JSON") from exc
        if not isinstance(payload, dict) or "results" not in payload:
            raise ScorerUnavailable("invalid-response", "missing 'results'")
        self._track_usage(payload)
        return payload

    def _track_usage(self, payload: Mapping[str, object]) -> None:
        usage = payload.get("total_usage") or payload.get("usage") or {}
        if isinstance(usage, dict):
            self._last_usage = dict(usage)
            for key, attr in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens")):
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    setattr(self.stats, attr, getattr(self.stats, attr) + int(value))

    # -- questions ---------------------------------------------------------
    def rank(
        self,
        query: str,
        candidates: Sequence[str],
        instructions: str = DEFAULT_INSTRUCTIONS,
    ) -> list[Ranked]:
        """Ask laya which candidate best answers ``query``; return all probabilities."""
        texts = [_profile(c, self._max_profile_chars) for c in candidates]
        count = len(texts)
        if count == 0:
            return []
        if count == 1:
            return [Ranked(0, 1.0)]
        if count > self._max_candidates:
            raise ContractViolation(
                f"rank() takes at most {self._max_candidates} candidates, got {count}; "
                "prefilter first"
            )

        order = label_order(query, count, self._label_salt)
        criteria = {LABEL_ALPHABET[position]: texts[source] for position, source in enumerate(order)}
        payload = {
            "model": self.credentials.model,
            "states": [_profile(query, 2000)],
            "questions": {
                "best": {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": criteria,
                }
            },
        }
        answer = self._answer(self._post(payload), 0, "best")
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or not probabilities:
            raise ScorerUnavailable("invalid-response", "choice answer has no probabilities")
        if len(probabilities) != count:
            raise ScorerUnavailable(
                "invalid-response",
                f"expected {count} probabilities, got {len(probabilities)}",
            )

        by_source = [0.0] * count
        for position, source in enumerate(order):
            label = LABEL_ALPHABET[position]
            value = probabilities.get(label)
            if not isinstance(value, (int, float)):
                raise ScorerUnavailable("invalid-response", f"missing probability for {label}")
            by_source[source] = _clamp01(float(value))

        ranked = [Ranked(index=i, probability=p) for i, p in enumerate(by_source)]
        ranked.sort(key=lambda r: (-r.probability, r.index))
        return ranked

    def score_levels(
        self,
        query: str,
        levels: Sequence[str],
        instructions: str = "Rate how well the state matches the topic.",
    ) -> float:
        """Graded 0..1 relevance from a ``score`` question over ordered levels."""
        if len(levels) < 2:
            raise ContractViolation("score_levels needs at least two levels")
        if len(levels) > MAX_LABELS:
            raise ContractViolation(f"at most {MAX_LABELS} levels, got {len(levels)}")
        payload = {
            "model": self.credentials.model,
            "states": [_profile(query, 2000)],
            "questions": {
                "fit": {
                    "type": "score",
                    "instructions": instructions,
                    "criteria": list(levels),
                }
            },
        }
        answer = self._answer(self._post(payload), 0, "fit")
        probabilities = answer.get("probabilities")
        if isinstance(probabilities, dict) and probabilities:
            total = 0.0
            weight = 0.0
            last = max(len(probabilities) - 1, 1)
            for key, value in probabilities.items():
                if not isinstance(value, (int, float)):
                    continue
                try:
                    index = int(str(key))
                except ValueError:
                    continue
                total += float(value) * (index / last)
                weight += float(value)
            if weight > 0:
                return _clamp01(total / weight)
        score = answer.get("score")
        if isinstance(score, (int, float)):
            return _clamp01(float(score) / max(len(levels) - 1, 1))
        raise ScorerUnavailable("invalid-response", "score answer has neither probabilities nor score")

    def _answer(self, payload: Mapping[str, object], state_index: int, question_id: str) -> dict:
        results = payload.get("results")
        if not isinstance(results, list) or len(results) <= state_index:
            raise ScorerUnavailable("invalid-response", "results shorter than states")
        item = results[state_index]
        if not isinstance(item, dict):
            raise ScorerUnavailable("invalid-response", "result entry is not an object")
        answers = item.get("answers")
        if not isinstance(answers, dict):
            raise ScorerUnavailable("invalid-response", "result has no answers")
        answer = answers.get(question_id)
        if not isinstance(answer, dict):
            raise ScorerUnavailable("invalid-response", f"missing answer {question_id!r}")
        return answer

    # -- diagnostics -------------------------------------------------------
    def health(self) -> dict[str, object]:
        """One tiny authenticated call so the CLI can prove the link works."""
        try:
            value = self.score_levels("trick health probe", ["unrelated", "related"])
        except ScorerUnavailable as exc:
            return {"ok": False, "reason": exc.reason, "detail": exc.detail}
        return {"ok": True, "probe_score": round(value, 4), "stats": self.stats.as_dict()}

    @property
    def last_usage(self) -> dict[str, object]:
        return dict(self._last_usage)


class MockScorer:
    """Deterministic offline scorer for tests and for hosts without a key.

    Ranking is IDF-weighted token overlap. It is explicitly *not* laya: the
    results it produces are labelled synthetic so the UI never claims a model
    score.
    """

    name = "mock"

    def __init__(self) -> None:
        self.stats = ScorerStats()
        self.last_usage: dict[str, object] = {}

    def rank(self, query: str, candidates: Sequence[str], instructions: str = "") -> list[Ranked]:
        q = _tokens(query)
        if not candidates:
            return []
        scores: list[float] = []
        for text in candidates:
            c = _tokens(text)
            if not q or not c:
                scores.append(0.0)
                continue
            overlap = len(q & c)
            scores.append(overlap / (len(q) ** 0.5 * len(c) ** 0.5) if overlap else 0.0)
        total = sum(scores)
        if total <= 0:
            probabilities = [1.0 / len(scores)] * len(scores)
        else:
            probabilities = [s / total for s in scores]
        ranked = [Ranked(i, p) for i, p in enumerate(probabilities)]
        ranked.sort(key=lambda r: (-r.probability, r.index))
        self.stats.calls += 1
        return ranked


def _tokens(text: str) -> set[str]:
    out = set()
    for token in str(text).lower().replace("-", " ").split():
        token = "".join(ch for ch in token if ch.isalnum())
        if len(token) > 2:
            out.add(token)
    return out


def _clamp01(value: float) -> float:
    if value != value:  # NaN
        return 0.0
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _retry_after_seconds(headers) -> float | None:
    if headers is None:
        return None
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if not raw:
        return None
    try:
        return float(str(raw).strip())
    except ValueError:
        return None


def build_scorer(
    mode: str = "auto",
    credentials: LayaCredentials | None = None,
) -> Scorer:
    """Return a live laya client, or the offline scorer when there is no key.

    ``auto`` prefers laya and degrades to the mock only when credentials are
    missing; a configured endpoint that is down must surface as S5, never as a
    silent downgrade to fake scores.
    """
    mode = (mode or "auto").lower()
    if mode in ("mock", "offline"):
        return MockScorer()
    if mode in ("remote", "laya", "auto"):
        try:
            return LayaClient(credentials)
        except ConfigError:
            if mode == "auto":
                return MockScorer()
            raise
    raise ConfigError(f"unknown scorer mode {mode!r}")


__all__ = [
    "DEFAULT_MAX_CANDIDATES",
    "LABEL_ALPHABET",
    "LayaClient",
    "MockScorer",
    "Ranked",
    "Scorer",
    "ScorerStats",
    "build_scorer",
    "label_order",
]
