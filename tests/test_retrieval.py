"""Source adapters: parsing, budgets, failure isolation and deduplication."""

from __future__ import annotations

import json

import pytest

from trick.errors import ContractViolation, SourceUnavailable
from trick.retrieval import (
    ArxivAdapter,
    BioconductorAdapter,
    CranCrandbAdapter,
    CrossrefAdapter,
    OpenAlexAdapter,
    SourceDoc,
    dedupe,
    invert_abstract,
    iter_json_objects,
    search_live,
)

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2101.00001v2</id>
    <published>2021-01-01T00:00:00Z</published>
    <title>Mixed Effects Models for   Longitudinal Data</title>
    <summary>We study random effects.</summary>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2101.00002v1</id>
    <published>2021-02-01T00:00:00Z</published>
    <title>Another Work</title>
    <summary>Unrelated.</summary>
  </entry>
</feed>
"""

OPENALEX = {
    "results": [
        {
            "id": "https://openalex.org/W123",
            "doi": "https://doi.org/10.1/abc",
            "title": "Mixed effects models in R",
            "publication_year": 2019,
            "primary_location": {"source": {"display_name": "Journal of Stats"}},
            "abstract_inverted_index": {"Mixed": [0], "effects": [1], "matter": [2]},
        }
    ]
}

CROSSREF = {
    "status": "ok",
    "message": {
        "items": [
            {
                "DOI": "10.1/xyz",
                "title": ["Spatial data analysis"],
                "URL": "https://doi.org/10.1/xyz",
                "abstract": "An abstract.",
                "container-title": ["Geoinformatica"],
            }
        ]
    },
}

CRAN_JSON = json.dumps(
    {
        "lme4": {
            "Package": "lme4",
            "Version": "1.1-35.5",
            "Title": "Linear Mixed-Effects Models using 'Eigen' and S4",
            "Description": "Fit linear and generalized linear mixed-effects models.\\nThe models and their components are represented using S4 classes.",
            "Date/Publication": "2026-10-23 08:12:04 UTC",
        },
        "data.table": {
            "Package": "data.table",
            "Version": "1.16.2",
            "Title": "Extension of 'data.frame'",
            "Description": "Fast aggregation of large data (e.g. 100GB in RAM), fast ordered joins, fast add/modify/delete of columns by group.",
            "Date/Publication": "2026-10-22 10:00:00 UTC",
        },
        "lme4.duplicate": {
            "Package": "lme4",
            "Version": "0.0.0",
            "Title": "Duplicate key must be ignored",
            "Description": "",
        },
    }
).encode()

BIOC = {
    "DESeq2": {
        "Package": "DESeq2",
        "Version": "1.44.0",
        "Title": "Differential expression analysis",
        "Description": "Negative binomial GLM for count data.",
    }
}


def transport(payload: bytes):
    def _get(url: str, timeout: float) -> bytes:
        _get.url = url
        _get.timeout = timeout
        return payload

    return _get


def failing_transport(exc: Exception):
    def _get(url: str, timeout: float) -> bytes:
        raise exc

    return _get


def test_cran_adapter_keeps_descriptions_and_dedupes():
    docs = CranCrandbAdapter(transport(CRAN_JSON)).fetch()
    assert [d.key for d in docs] == ["lme4", "data.table"]
    assert docs[0].title.startswith("lme4: Linear Mixed-Effects")
    assert docs[0].url.endswith("package=lme4")
    assert docs[0].meta["published"] == "2026-10-23"
    # The regression that mattered: a CRAN row without a description can only be
    # found by its name, and "fast aggregation of large data" lives in this text.
    assert "Fast aggregation of large data" in docs[1].summary
    assert docs[0].summary.startswith("Fit linear and generalized linear")


def test_cran_adapter_normalises_newlines_in_descriptions():
    docs = CranCrandbAdapter(transport(CRAN_JSON)).fetch()
    joined = " ".join(d.summary for d in docs)
    assert "\n" not in joined


def test_cran_adapter_ignores_payload_without_objects():
    assert CranCrandbAdapter(transport(b"<html><body>maintenance</body></html>")).fetch() == []


def test_iter_json_objects_streams_nested_and_escaped_values():
    payload = (
        b'{"a": {"Package": "a", "Description": "brace } and quote \\" inside"},'
        b' "b": {"Package": "b", "Depends": {"R": ">= 4.0"}},'
        b' "c": "not an object"}'
    )
    seen = list(iter_json_objects(payload))
    assert [obj.get("Package") for obj in seen] == ["a", "b"]
    assert "brace }" in seen[0]["Description"]


def test_iter_json_objects_stops_on_truncated_payload():
    payload = b'{"a": {"Package": "a"}, "b": {"Package": "b"'
    seen = list(iter_json_objects(payload))
    assert [obj["Package"] for obj in seen] == ["a"]


def test_iter_json_objects_survives_garbage():
    assert list(iter_json_objects(b"")) == []
    assert list(iter_json_objects(b"[]")) == []


def test_bioconductor_adapter_parses_json_snapshot():
    docs = BioconductorAdapter(transport(json.dumps(BIOC).encode())).fetch()
    assert len(docs) == 1
    assert docs[0].source == "bioconductor"
    assert docs[0].key == "DESeq2"
    assert docs[0].meta["version"] == "1.44.0"


def test_bioconductor_adapter_rejects_non_json():
    with pytest.raises(SourceUnavailable):
        BioconductorAdapter(transport(b"<html>")).fetch()


def test_arxiv_adapter_parses_atom_and_normalises_url():
    docs = ArxivAdapter(transport(ATOM.encode())).search("mixed effects", limit=2)
    assert len(docs) == 2
    assert docs[0].source == "arxiv"
    assert docs[0].key == "2101.00001v2"
    assert docs[0].title == "Mixed Effects Models for Longitudinal Data"
    assert docs[0].url.startswith("https://arxiv.org/abs/")
    assert docs[0].meta["published"] == "2021-01-01"


def test_arxiv_adapter_rejects_invalid_xml():
    with pytest.raises(SourceUnavailable):
        ArxivAdapter(transport(b"<feed>")).search("q", limit=1)


def test_openalex_adapter_uses_select_and_inverts_abstract():
    getter = transport(json.dumps(OPENALEX).encode())
    docs = OpenAlexAdapter(getter).search("mixed effects", limit=1)
    assert "select=" in getter.url
    assert docs[0].key == "W123"
    assert docs[0].summary == "Mixed effects matter"
    assert docs[0].url == "https://doi.org/10.1/abc"
    assert docs[0].meta["venue"] == "Journal of Stats"


def test_crossref_adapter_parses_items():
    docs = CrossrefAdapter(transport(json.dumps(CROSSREF).encode())).search("spatial", limit=1)
    assert docs[0].key == "10.1/xyz"
    assert docs[0].title == "Spatial data analysis"
    assert docs[0].meta["venue"] == "Geoinformatica"


def test_invert_abstract_orders_words_by_position():
    assert invert_abstract({"b": [1], "a": [0], "c": [2]}) == "a b c"
    assert invert_abstract(None) == ""
    assert invert_abstract({"x": "not a list"}) == ""


def test_empty_query_is_a_contract_violation():
    with pytest.raises(ContractViolation):
        CrossrefAdapter(transport(b"{}")).search("   ", limit=1)


def test_http_error_maps_to_source_unavailable():
    import urllib.error

    def _get(url, timeout):
        raise urllib.error.HTTPError(url, 503, "down", {}, None)

    with pytest.raises(SourceUnavailable) as info:
        ArxivAdapter(_get).search("q", limit=1)
    assert "503" in str(info.value)


def test_dedupe_prefers_the_higher_priority_source():
    docs = [
        SourceDoc("arxiv", "x", "Mixed Effects Models"),
        SourceDoc("cran", "lme4", "mixed   effects models!"),
        SourceDoc("crossref", "10.1/a", "Mixed Effects Models"),
    ]
    unique = dedupe(docs)
    assert len(unique) == 1
    assert unique[0].source == "cran"


def test_dedupe_keeps_distinct_titles_and_exact_duplicates_removed():
    docs = [
        SourceDoc("cran", "a", "Alpha package"),
        SourceDoc("cran", "a", "Alpha package"),
        SourceDoc("cran", "b", "Beta package"),
    ]
    assert [d.key for d in dedupe(docs)] == ["a", "b"]


def test_search_live_isolates_a_failing_adapter():
    ok = CrossrefAdapter(transport(json.dumps(CROSSREF).encode()))
    broken = ArxivAdapter(failing_transport(SourceUnavailable("arxiv: HTTP 503")))
    report = search_live("spatial data", adapters=[ok, broken], limit_per_source=1)
    assert report.answered == ["crossref"]
    assert "arxiv" in report.failed
    assert len(report.docs) == 1
    assert report.elapsed_ms >= 0


def test_search_live_with_no_adapters_returns_empty_report():
    report = search_live("q", adapters=[])
    assert report.docs == [] and report.answered == [] and report.failed == {}


def test_unexpected_adapter_exception_is_contained():
    class Boom:
        name = "boom"

        def search(self, query, limit, timeout):
            raise RuntimeError("adapter bug")

    report = search_live("q", adapters=[Boom()], limit_per_source=1)
    assert report.failed["boom"].startswith("RuntimeError")
    assert report.docs == []

def test_arxiv_url_encodes_terms_without_raw_quotes():
    url = ArxivAdapter(transport(ATOM.encode())).url_for("mixed effects model", 3)
    assert '"' not in url
    assert "%20" in url or "+" in url
    assert url.count("all:") == 3
    assert "max_results=3" in url


def test_contact_email_joins_the_polite_pool(monkeypatch):
    monkeypatch.setenv("TRICK_CONTACT_EMAIL", "trick@example.org")
    url = OpenAlexAdapter(transport(b"{}")).url_for("mixed effects", 3)
    assert "mailto=trick%40example.org" in url
    crossref = CrossrefAdapter(transport(b"{}")).url_for("mixed effects", 3)
    assert "mailto=trick%40example.org" in crossref


def test_contact_email_is_optional_and_validated(monkeypatch):
    monkeypatch.delenv("TRICK_CONTACT_EMAIL", raising=False)
    url = OpenAlexAdapter(transport(b"{}")).url_for("mixed effects", 3)
    assert "mailto" not in url
    for bad in ("", "not-an-email", "a@b" + "c" * 300):
        monkeypatch.setenv("TRICK_CONTACT_EMAIL", bad)
        assert "mailto" not in OpenAlexAdapter(transport(b"{}")).url_for("q", 3), bad
