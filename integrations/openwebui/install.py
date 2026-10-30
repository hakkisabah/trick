#!/usr/bin/env python3
"""Install or update the Trick Pipe in a running Open WebUI.

Needs an Open WebUI API key with admin rights (Settings -> Account -> API Keys).
The key is read from the environment, never from an argument, so it does not end
up in the shell history or in ``ps`` output.

    TRICK_OPENWEBUI_API_KEY=... python3 integrations/openwebui/install.py
    TRICK_OPENWEBUI_API_KEY=... TRICK_OPENWEBUI_URL=http://localhost:3000 \
        python3 integrations/openwebui/install.py --token-file ~/.local/share/trick/bridge.token

The bridge token is only *reported*: the Pipe stores it in its own valve inside
Open WebUI, and writing it there from here would mean sending a secret through a
second service for no reason.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPE_FILE = HERE / "trick_pipe.py"
FUNCTION_ID = "trick"
FUNCTION_NAME = "Trick"


def request(url: str, key: str, payload: dict | None = None) -> tuple[int, dict | str]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="GET" if payload is None else "POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw
    except urllib.error.URLError as exc:
        print(f"cannot reach {url}: {exc.reason}", file=sys.stderr)
        raise SystemExit(2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("TRICK_OPENWEBUI_URL", "http://localhost:3000"))
    parser.add_argument("--token-file", default=str(Path.home() / ".local" / "share" / "trick" / "bridge.token"))
    parser.add_argument("--dry-run", action="store_true", help="print what would be sent, change nothing")
    args = parser.parse_args()

    key = os.environ.get("TRICK_OPENWEBUI_API_KEY", "").strip()
    if not key:
        print("set TRICK_OPENWEBUI_API_KEY to an Open WebUI admin API key", file=sys.stderr)
        return 2

    content = PIPE_FILE.read_text(encoding="utf-8")
    payload = {
        "id": FUNCTION_ID,
        "name": FUNCTION_NAME,
        "content": content,
        "meta": {
            "description": "Scored retrieval over scientific catalogues, ranked by laya.",
            "manifest": {},
        },
    }

    base = args.url.rstrip("/")
    status, body = request(f"{base}/api/v1/functions/", key)
    if status == 401:
        print("the API key was rejected (401); create one under Settings -> Account -> API Keys", file=sys.stderr)
        return 1
    if status != 200:
        print(f"listing functions failed: {status} {body}", file=sys.stderr)
        return 1
    existing = {item.get("id") for item in body} if isinstance(body, list) else set()
    action = "update" if FUNCTION_ID in existing else "create"
    print(f"function '{FUNCTION_ID}' {'exists, updating' if action == 'update' else 'is missing, creating'}")

    if args.dry_run:
        print(f"would POST {base}/api/v1/functions/{'id/' + FUNCTION_ID + '/update' if action == 'update' else 'create'}")
        print(f"content: {len(content)} bytes")
        return 0

    if action == "update":
        status, body = request(f"{base}/api/v1/functions/id/{FUNCTION_ID}/update", key, payload)
    else:
        status, body = request(f"{base}/api/v1/functions/create", key, payload)
    if status not in (200, 201):
        print(f"{action} failed: {status} {body}", file=sys.stderr)
        return 1

    status, stored = request(f"{base}/api/v1/functions/id/{FUNCTION_ID}", key)
    if status != 200 or not isinstance(stored, dict):
        print(f"installed, but reading it back failed: {status} {stored}", file=sys.stderr)
        return 1
    stored_content = stored.get("content") or ""
    if len(stored_content) != len(content):
        print(
            f"installed, but the stored content differs ({len(stored_content)} vs {len(content)} bytes)",
            file=sys.stderr,
        )
        return 1

    token_path = Path(args.token_file)
    print(f"installed and verified: {len(content)} bytes, enabled={stored.get('is_active', 'unknown')}")
    if token_path.exists():
        print(f"next: paste the contents of {token_path} into the Pipe's TRICK_TOKEN valve")
    else:
        print(f"next: start 'trick serve' once, then paste {token_path} into the TRICK_TOKEN valve")
    print("the model list should now show 'Trick - scored scientific sources'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
