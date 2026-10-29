"""Source adapters: bulk catalogues and live query APIs.

The scientific and engineering ecosystem splits into two shapes, and Trick treats
them differently on purpose.

**Bulk catalogues** (CRAN, Bioconductor) publish one large snapshot. They are
downloaded rarely and indexed locally in SQLite/FTS5, because a per-query API
call to them would be slow, rate limited, or simply unavailable.

    CRAN              .../web/packages/available_packages_by_date.html   4.9 MB
    Bioconductor      .../packages/json/3.21/bioc/packages.json          6.3 MB

**Live query APIs** (arXiv, OpenAlex, Crossref) answer a small query in well
under a second and are cheap enough to call per search.

Deliberate exclusions, verified against the live services:

* ``crandb /-/search`` returns 404 - there is no CRAN search endpoint;
  ``crandb /-/all`` is 307 MB, so the HTML index is the right snapshot.
* ``cran src/contrib/PACKAGES.gz`` parses into 25,608 blocks with no Title and
  no Description, so it cannot feed a text index; only the HTML index can.
* Papers with Code redirects to Hugging Face and its API is gone.
* Semantic Scholar answers 429 without a key; not usable as a default.
* PyPI's simple index is 47 MB of HTML; package JSON needs an exact name, so it
  is used only to enrich a name found by FTS, never as a query source.

Every adapter returns plain dicts (``source``, ``key``, ``title``, ``summary``,
``url``, ``meta``) so the store, the tests and the UI all see one shape. All of
them take an injectable transport, which is how the tests run without a network.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html import unescape
from typing import Callable, Iterable, Sequence

from .errors import ContractViolation, SourceUnavailable

USER_AGENT = "trick/0.1 (+https://github.com/hakkisabah/trick)"
DEFAULT_BUDGET_S = 1.5
CRAN_PACKAGES_URL = "https://cran.r-project.org/web/packages/available_packages_by_date.html"
BIOC_PACKAGES_URL = "https://bioconductor.org/packages/json/3.21/bioc/packages.json"
ARXIV_URL = "https://export.arxiv.org/api/query"
OPENALEX_URL = "https://api.openalex.org/works"
CROSSREF_URL = "https://api.crossref.org/works"

# Source priority: scientific package catalogues first, then papers, then
# preprints. Used to break ties when two adapters return the same title.
SOURCE_PRIORITY = {"cran": 0, "bioconductor": 1, "openalex": 2, "crossref": 3, "arxiv": 4}


@dataclass(frozen=True)
class SourceDoc:
    """One catalogue entry, in the shape the store and the UI expect."""

    source: str
    key: str
    title: str
    summary: str = ""
    url: str = ""
    meta: dict = field(default_factory=dict)

    def as_row(self) -> dict:
        return {
            "source": self.source,
            "key": self.key,
            "title": self.title,
            "summary": self.summary,
            "url": self.url,
            "meta": self.meta,
        }


def http_get(url: str, timeout: float) -> bytes:
    """Default transport: plain urllib with an honest user agent."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


Transport = Callable[[str, float], bytes]


class BulkAdapter:
    """A catalogue that is downloaded as one snapshot and indexed locally."""

    name = "bulk"
    url = ""

    def __init__(self, transport: Transport = http_get) -> None:
        self._transport = transport

    def snapshot(self, timeout: float = 60.0) -> bytes:
        try:
            return self._transport(self.url, timeout)
        except urllib.error.HTTPError as exc:
            raise SourceUnavailable(f"{self.name}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise SourceUnavailable(f"{self.name}: {exc}") from exc

    def documents(self, payload: bytes) -> list[SourceDoc]:
        raise NotImplementedError

    def fetch(self, timeout: float = 60.0) -> list[SourceDoc]:
        return self.documents(self.snapshot(timeout))


class CranAdapter(BulkAdapter):
    """CRAN via the "available packages by date" snapshot.

    The obvious source, ``src/contrib/PACKAGES.gz``, is a trap: it lists
    Package, Version, Depends, Imports, Suggests, License and MD5sum but **no
    Title and no Description**, so parsing it yields 25k rows that are worthless
    for text search (verified: 25,608 blocks, zero titles). The HTML index has
    the one field that matters, plus the publication date:

        <td>2026-10-23</td><td><a href=".../lme4/index.html">
        <span class="CRAN">lme4</span></a></td><td>Linear Mixed-Effects Models ...</td>
    """

    name = "cran"
    url = CRAN_PACKAGES_URL
    ROW = re.compile(
        r"<tr>\s*<td>\s*(\d{4}-\d{2}-\d{2})\s*</td>\s*"
        r'<td>\s*<a href="[^"]*/packages/([A-Za-z0-9._]+)/index\.html">\s*'
        r'<span class="CRAN">([^<]+)</span>\s*</a>\s*</td>\s*'
        r"<td>(.*?)</td>",
        re.S,
    )

    def documents(self, payload: bytes) -> list[SourceDoc]:
        text = payload.decode("utf-8", "replace")
        out: list[SourceDoc] = []
        seen: set[str] = set()
        for published, path_name, name, title_html in self.ROW.findall(text):
            name = name.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            title = unescape(re.sub(r"<[^>]+>", " ", title_html))
            title = " ".join(title.split())
            if not title:
                continue
            out.append(
                SourceDoc(
                    source="cran",
                    key=name,
                    title=f"{name}: {title}",
                    summary="",
                    url=f"https://CRAN.R-project.org/package={name}",
                    meta={"kind": "r-package", "published": published, "path": path_name},
                )
            )
        return out


class BioconductorAdapter(BulkAdapter):
    """Bioconductor release snapshot as JSON keyed by package name."""

    name = "bioconductor"
    url = BIOC_PACKAGES_URL

    def documents(self, payload: bytes) -> list[SourceDoc]:
        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except ValueError as exc:
            raise SourceUnavailable(f"bioconductor: body is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise SourceUnavailable("bioconductor: unexpected payload shape")
        revdeps = reverse_dependency_counts(data)
        out: list[SourceDoc] = []
        for name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            title = " ".join(str(entry.get("Title", "")).split())
            if not name or not title:
                continue
            count = int(revdeps.get(str(name), 0))
            out.append(
                SourceDoc(
                    source="bioconductor",
                    key=str(name),
                    title=f"{name}: {title}",
                    summary=" ".join(str(entry.get("Description", "")).split()),
                    url=f"https://bioconductor.org/packages/{name}",
                    meta={
                        "version": str(entry.get("Version", "")),
                        "kind": "bioc-package",
                        "status": str(entry.get("PackageStatus", "")),
                        "revdeps": count,
                        "weight": log_weight(count, BIOC_REVDEP_REF),
                    },
                )
            )
        return out


class QueryAdapter:
    """A live API queried per search, with a hard time budget."""

    name = "query"
    priority: int = 9

    def __init__(self, transport: Transport = http_get) -> None:
        self._transport = transport

    def url_for(self, query: str, limit: int) -> str:
        raise NotImplementedError

    def documents(self, payload: bytes, query: str) -> list[SourceDoc]:
        raise NotImplementedError

    def search(self, query: str, limit: int = 5, timeout: float = DEFAULT_BUDGET_S) -> list[SourceDoc]:
        query = " ".join(str(query).split())
        if not query:
            raise ContractViolation("query is empty")
        url = self.url_for(query, limit)
        started = time.monotonic()
        try:
            payload = self._transport(url, timeout)
        except urllib.error.HTTPError as exc:
            raise SourceUnavailable(f"{self.name}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise SourceUnavailable(f"{self.name}: {exc}") from exc
        if time.monotonic() - started > timeout * 2:
            raise SourceUnavailable(f"{self.name}: exceeded budget")
        return self.documents(payload, query)[:limit]


class ArxivAdapter(QueryAdapter):
    name = "arxiv"
    priority = SOURCE_PRIORITY["arxiv"]

    def url_for(self, query: str, limit: int) -> str:
        # arXiv rejects a raw '"' in the query string with HTTP 400; terms must be
        # AND-ed as all:word and percent-encoded.
        terms = " AND ".join(f"all:{token}" for token in _words(query)[:6])
        if not terms:
            terms = f"all:{urllib.parse.quote(query.strip()) or 'science'}"
        return (
            f"{ARXIV_URL}?search_query={urllib.parse.quote(terms, safe=':')}"
            f"&start=0&max_results={max(1, limit)}"
        )

    def documents(self, payload: bytes, query: str) -> list[SourceDoc]:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise SourceUnavailable(f"arxiv: invalid XML: {exc}") from exc
        ns = {"a": "http://www.w3.org/2005/Atom"}
        out: list[SourceDoc] = []
        for entry in root.findall("a:entry", ns):
            raw_id = (entry.findtext("a:id", default="", namespaces=ns) or "").strip()
            title = " ".join((entry.findtext("a:title", default="", namespaces=ns) or "").split())
            summary = " ".join((entry.findtext("a:summary", default="", namespaces=ns) or "").split())
            if not raw_id or not title:
                continue
            key = raw_id.rsplit("/", 1)[-1]
            out.append(
                SourceDoc(
                    source="arxiv",
                    key=key,
                    title=title,
                    summary=summary[:1500],
                    url=raw_id.replace("http://", "https://"),
                    meta={
                        "kind": "preprint",
                        "published": (entry.findtext("a:published", default="", namespaces=ns) or "")[:10],
                    },
                )
            )
        return out


class OpenAlexAdapter(QueryAdapter):
    name = "openalex"
    priority = SOURCE_PRIORITY["openalex"]

    def url_for(self, query: str, limit: int) -> str:
        terms = urllib.parse.quote_plus(query)
        fields = "id,doi,title,publication_year,primary_location,abstract_inverted_index"
        url = f"{OPENALEX_URL}?search={terms}&per-page={max(1, limit)}&select={fields}"
        # OpenAlex puts a caller that identifies itself in the polite pool, which
        # has a much higher rate limit. Without it the anonymous bucket answers
        # HTTP 429 after a handful of searches (measured: an evaluation run of 22
        # queries tripped it, and every later search degraded on S4).
        contact = _contact_email()
        if contact:
            url += f"&mailto={urllib.parse.quote(contact)}"
        return url

    def documents(self, payload: bytes, query: str) -> list[SourceDoc]:
        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except ValueError as exc:
            raise SourceUnavailable(f"openalex: body is not JSON: {exc}") from exc
        items = data.get("results") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise SourceUnavailable("openalex: no results list")
        out: list[SourceDoc] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = " ".join(str(item.get("title") or "").split())
            work_id = str(item.get("id") or "")
            if not title or not work_id:
                continue
            out.append(
                SourceDoc(
                    source="openalex",
                    key=work_id.rsplit("/", 1)[-1],
                    title=title,
                    summary=invert_abstract(item.get("abstract_inverted_index"))[:1500],
                    url=str(item.get("doi") or work_id),
                    meta={
                        "kind": "work",
                        "year": item.get("publication_year"),
                        "venue": _venue(item),
                    },
                )
            )
        return out


class CrossrefAdapter(QueryAdapter):
    name = "crossref"
    priority = SOURCE_PRIORITY["crossref"]

    def url_for(self, query: str, limit: int) -> str:
        terms = urllib.parse.quote_plus(query)
        url = f"{CROSSREF_URL}?query={terms}&rows={max(1, limit)}&select=DOI,title,URL,abstract,container-title,issued"
        contact = _contact_email()
        if contact:
            url += f"&mailto={urllib.parse.quote(contact)}"
        return url

    def documents(self, payload: bytes, query: str) -> list[SourceDoc]:
        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except ValueError as exc:
            raise SourceUnavailable(f"crossref: body is not JSON: {exc}") from exc
        message = data.get("message") if isinstance(data, dict) else None
        items = message.get("items") if isinstance(message, dict) else None
        if not isinstance(items, list):
            raise SourceUnavailable("crossref: no items list")
        out: list[SourceDoc] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            titles = item.get("title") or []
            title = " ".join(str(titles[0]).split()) if titles else ""
            doi = str(item.get("DOI") or "")
            if not title or not doi:
                continue
            out.append(
                SourceDoc(
                    source="crossref",
                    key=doi,
                    title=title,
                    summary=" ".join(str(item.get("abstract") or "").split())[:1500],
                    url=str(item.get("URL") or f"https://doi.org/{doi}"),
                    meta={"kind": "work", "venue": (item.get("container-title") or [""])[0]},
                )
            )
        return out


def _venue(item: dict) -> str:
    location = item.get("primary_location")
    if isinstance(location, dict):
        source = location.get("source")
        if isinstance(source, dict):
            return str(source.get("display_name") or "")
    return ""


def _contact_email(environ: dict[str, str] | None = None) -> str:
    """The address that identifies this caller to the scholarly APIs.

    Set ``TRICK_CONTACT_EMAIL`` to join the polite pools: OpenAlex then allows far
    more requests per second, and Crossref treats the caller as identifiable
    rather than anonymous. Empty means the header is simply not sent.
    """
    env = os.environ if environ is None else environ
    value = (env.get("TRICK_CONTACT_EMAIL") or "").strip()
    if "@" not in value or len(value) > 200:
        return ""
    return value


def _words(text: str) -> list[str]:
    return [w for w in "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in text.lower()).split() if len(w) > 2]


def invert_abstract(index) -> str:
    """Rebuild text from OpenAlex's inverted abstract index."""
    if not isinstance(index, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, where in index.items():
        if isinstance(where, list):
            for position in where:
                if isinstance(position, int):
                    positions.append((position, str(word)))
    positions.sort()
    return " ".join(word for _, word in positions)


@dataclass
class RetrievalReport:
    """What answered, what did not, and why."""

    docs: list[SourceDoc]
    answered: list[str]
    failed: dict[str, str]
    elapsed_ms: float

    def as_dict(self) -> dict:
        return {
            "answered": list(self.answered),
            "failed": dict(self.failed),
            "elapsed_ms": round(self.elapsed_ms, 1),
            "docs": len(self.docs),
        }


def dedupe(docs: Iterable[SourceDoc]) -> list[SourceDoc]:
    """Drop duplicate catalogue rows, keeping the highest-priority source.

    Two keys collapse: the exact ``(source, key)`` pair, and a normalised title
    so the same paper from OpenAlex and Crossref is shown once.
    """
    best: dict[str, SourceDoc] = {}
    seen_source_key: set[tuple[str, str]] = set()
    for doc in docs:
        pair = (doc.source, doc.key)
        if pair in seen_source_key:
            continue
        seen_source_key.add(pair)
        title_key = "".join(ch for ch in doc.title.lower() if ch.isalnum())[:80]
        if not title_key:
            title_key = f"{doc.source}:{doc.key}"
        current = best.get(title_key)
        if current is None or SOURCE_PRIORITY.get(doc.source, 9) < SOURCE_PRIORITY.get(current.source, 9):
            best[title_key] = doc
    return sorted(best.values(), key=lambda d: (SOURCE_PRIORITY.get(d.source, 9), d.key))


def live_adapters(transport: Transport = http_get) -> list[QueryAdapter]:
    return [OpenAlexAdapter(transport), CrossrefAdapter(transport), ArxivAdapter(transport)]


def bulk_adapters(transport: Transport = http_get) -> list[BulkAdapter]:
    return [CranAdapter(transport), BioconductorAdapter(transport)]


def search_live(
    query: str,
    *,
    adapters: Sequence[QueryAdapter] | None = None,
    limit_per_source: int = 5,
    budget_s: float = DEFAULT_BUDGET_S,
    max_workers: int = 3,
) -> RetrievalReport:
    """Query every live adapter in parallel, honouring a per-source budget."""
    adapters = list(adapters if adapters is not None else live_adapters())
    started = time.monotonic()
    docs: list[SourceDoc] = []
    answered: list[str] = []
    failed: dict[str, str] = {}
    if not adapters:
        return RetrievalReport([], [], {}, 0.0)

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="trick-src") as pool:
        futures = {
            pool.submit(adapter.search, query, limit_per_source, budget_s): adapter for adapter in adapters
        }
        for future in as_completed(futures, timeout=max(budget_s * 3, 5.0)):
            adapter = futures[future]
            try:
                rows = future.result()
            except SourceUnavailable as exc:
                failed[adapter.name] = str(exc)
            except Exception as exc:  # noqa: BLE001 - one bad adapter must not sink the search
                failed[adapter.name] = f"{type(exc).__name__}: {exc}"
            else:
                answered.append(adapter.name)
                docs.extend(rows)

    return RetrievalReport(
        docs=dedupe(docs),
        answered=sorted(answered),
        failed=dict(sorted(failed.items())),
        elapsed_ms=(time.monotonic() - started) * 1000.0,
    )


__all__ = [
    "ArxivAdapter",
    "BioconductorAdapter",
    "BulkAdapter",
    "CranAdapter",
    "CranDownloadsAdapter",
    "CrossrefAdapter",
    "OpenAlexAdapter",
    "QueryAdapter",
    "RetrievalReport",
    "SOURCE_PRIORITY",
    "SourceDoc",
    "bulk_adapters",
    "dedupe",
    "reverse_dependency_counts",
    "http_get",
    "invert_abstract",
    "live_adapters",
    "log_weight",
    "search_live",
]


CRANLOGS_URL = "https://cranlogs.r-pkg.org/downloads/total/last-month/"
CRAN_DOWNLOAD_REF = 2_000_000  # monthly downloads of the most used package, for log scaling
BIOC_REVDEP_REF = 200  # reverse dependencies of a central Bioconductor package


def log_weight(raw: float, reference: float) -> float:
    """Map a raw popularity count to 0..1 on a log scale, clipped at 1.0."""
    if raw is None or reference <= 1:
        return 0.0
    value = math.log1p(max(float(raw), 0.0)) / math.log1p(reference)
    return 1.0 if value > 1.0 else (0.0 if value < 0.0 else value)


class CranDownloadsAdapter:
    """Monthly CRAN download counts, in chunks of ``chunk`` package names.

    cranlogs caps ``/top/`` at 100 entries, but the per-package endpoint accepts a
    comma separated list (verified with 120 names in one request), so the whole
    catalogue costs ~214 requests. That is an ingest-time cost, paid once, not a
    per-query one.
    """

    name = "cran-downloads"

    def __init__(self, transport: Transport = http_get, chunk: int = 120) -> None:
        self._transport = transport
        self.chunk = max(1, min(int(chunk), 120))

    def downloads(self, names: Sequence[str], timeout: float = 30.0) -> dict[str, int]:
        out: dict[str, int] = {}
        clean = [str(n).strip() for n in names if str(n).strip()]
        for start in range(0, len(clean), self.chunk):
            batch = clean[start : start + self.chunk]
            url = CRANLOGS_URL + ",".join(urllib.parse.quote(n) for n in batch)
            try:
                payload = self._transport(url, timeout)
            except urllib.error.HTTPError as exc:
                raise SourceUnavailable(f"cran-downloads: HTTP {exc.code}") from exc
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                raise SourceUnavailable(f"cran-downloads: {exc}") from exc
            try:
                rows = json.loads(payload.decode("utf-8", "replace"))
            except ValueError as exc:
                raise SourceUnavailable(f"cran-downloads: body is not JSON: {exc}") from exc
            if not isinstance(rows, list):
                raise SourceUnavailable("cran-downloads: unexpected payload")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("package") or "")
                count = row.get("downloads")
                if name and isinstance(count, (int, float)):
                    out[name] = int(count)
        return out


def reverse_dependency_counts(records: dict[str, dict]) -> dict[str, int]:
    """Count how many packages list a name in Depends/Imports/Suggests.

    Bioconductor has no public download API, but the snapshot carries the
    dependency graph, so reverse dependencies are a free canonicality proxy that
    is computed locally and refreshed with every snapshot.
    """
    counts: dict[str, int] = {}
    for entry in records.values():
        if not isinstance(entry, dict):
            continue
        seen: set[str] = set()
        for field_name in ("Depends", "Imports", "Suggests", "LinkingTo"):
            value = entry.get(field_name)
            items = value if isinstance(value, list) else str(value or "").split(",")
            for item in items:
                name = str(item).split("(")[0].strip()
                if name and name not in seen:
                    seen.add(name)
                    counts[name] = counts.get(name, 0) + 1
    return counts
