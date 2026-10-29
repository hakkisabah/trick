"""Live probe against the configured laya endpoint.

Reads credentials from ``~/.config/hermes-secrets/laya.credentials`` (or the
file named by ``TRICK_CREDENTIALS``), sends one small batch, and prints the
*shape* of the response: key names, types and the answer fields Trick needs.
The API key is never printed.

    LAYA_PROBE=1 .venv/bin/python scripts/laya_probe.py

Exits non-zero when the endpoint cannot be reached, so the gate can use it.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_CREDENTIALS = Path.home() / ".config" / "hermes-secrets" / "laya.credentials"


def read_credentials(path: Path) -> dict[str, str]:
    """Parse ``KEY=value`` lines, ignoring the example block and comments."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def shape(value, depth: int = 0):
    """Describe a JSON value without echoing user content."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in list(value.items())[:12]}
    if isinstance(value, list):
        if not value:
            return []
        return [f"list[{len(value)}]", shape(value[0], depth + 1)]
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return f"str[{len(value)}]"
    return type(value).__name__


def main() -> int:
    creds_path = Path(os.environ.get("TRICK_CREDENTIALS", DEFAULT_CREDENTIALS))
    creds = read_credentials(creds_path)
    base = os.environ.get("LAYA_BASE_URL") or creds.get("LAYA_BASE_URL", "")
    key = os.environ.get("LAYA_API_KEY") or creds.get("LAYA_API_KEY", "")
    timeout_ms = int(os.environ.get("LAYA_TIMEOUT_MS") or creds.get("LAYA_TIMEOUT_MS", "20000"))
    if not base or not key:
        print(f"probe: missing LAYA_BASE_URL or LAYA_API_KEY in {creds_path}", file=sys.stderr)
        return 2

    body = {
        "model": "laya",
        "states": [
            "Mixed effects model does not converge with lme4",
            "How do I compute a Kaplan-Meier estimator in R?",
        ],
        "questions": {
            "best": {
                "type": "choice",
                "instructions": "Which source best answers the state?",
                "criteria": {
                    "cran": "R package, statistics, modelling",
                    "bioconductor": "genomics, sequencing, biology",
                    "arxiv": "preprint, paper, theory",
                },
            }
        },
    }
    req = urllib.request.Request(
        base,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_ms / 1000.0) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        print(f"probe: HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - probe reports any transport failure
        print(f"probe: transport failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"probe: HTTP {status}, {len(raw)} bytes")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(f"probe: non-JSON body: {raw[:200]}", file=sys.stderr)
        return 1
    print(json.dumps(shape(payload), indent=2, ensure_ascii=False))

    results = payload.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        answers = first.get("answers", {}) if isinstance(first, dict) else {}
        answer = answers.get("best", {}) if isinstance(answers, dict) else {}
        print("probe: first answer fields ->", sorted(answer) if isinstance(answer, dict) else answer)
        print("probe: choice =", json.dumps(answer.get("choice")), "confidence =", answer.get("answer_confidence"))
        print("probe: result keys ->", sorted(first) if isinstance(first, dict) else type(first).__name__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
