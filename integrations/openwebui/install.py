#!/usr/bin/env python3
"""Install or update the Trick Action in a running Open WebUI.

Needs an Open WebUI API key with admin rights (Settings -> Account -> API Keys).
The key is read from the environment, never from an argument, so it does not end
up in the shell history or in ``ps`` output.

    TRICK_OPENWEBUI_API_KEY=... python3 integrations/openwebui/install.py
    TRICK_OPENWEBUI_API_KEY=... python3 integrations/openwebui/install.py --dry-run

What it does, in order: create or update the function, check that Open WebUI
stored the exact bytes and derived the right type (an Action, not a Pipe),
make it global so the button shows on every model, and write the bridge token
into the function's TRICK_TOKEN valve when the token file exists.

The token is never printed: only its length is reported.
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
ACTION_FILE = HERE / "trick_action.py"
FUNCTION_ID = "trick"
FUNCTION_NAME = "Trick"
EXPECTED_TYPE = "action"


def request(url: str, key: str, payload: dict | None = None, method: str | None = None) -> tuple[int, dict | str]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    verb = method or ("GET" if payload is None else "POST")
    req = urllib.request.Request(
        url,
        data=data,
        method=verb,
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
    parser.add_argument(
        "--no-token",
        action="store_true",
        help="do not write the bridge token into the TRICK_TOKEN valve",
    )
    args = parser.parse_args()

    key = os.environ.get("TRICK_OPENWEBUI_API_KEY", "").strip()
    if not key:
        print("set TRICK_OPENWEBUI_API_KEY to an Open WebUI admin API key", file=sys.stderr)
        return 2

    content = ACTION_FILE.read_text(encoding="utf-8")
    payload = {
        "id": FUNCTION_ID,
        "name": FUNCTION_NAME,
        "content": content,
        "meta": {
            "description": "Find a passage in a long conversation, and scored sources beside it.",
            "manifest": {},
        },
    }

    base = args.url.rstrip("/")
    status, body = request(f"{base}/api/v1/functions/", key)
    if status == 401:
        print(
            "the API key was rejected (401); create one under Settings -> Account -> API Keys "
            "while signed in as an admin",
            file=sys.stderr,
        )
        return 1
    if status != 200:
        print(f"listing functions failed: {status} {body}", file=sys.stderr)
        return 1
    listed = body if isinstance(body, list) else []
    existing = next((item for item in listed if item.get("id") == FUNCTION_ID), None)
    verb = "update" if existing else "create"
    if existing:
        print(f"function '{FUNCTION_ID}' exists as type={existing.get('type')}, updating to {EXPECTED_TYPE}")
    else:
        print(f"function '{FUNCTION_ID}' is missing, creating it as type={EXPECTED_TYPE}")

    if args.dry_run:
        endpoint = f"/api/v1/functions/id/{FUNCTION_ID}/update" if existing else "/api/v1/functions/create"
        print(f"would POST {base}{endpoint}")
        print(f"content: {len(content)} bytes")
        return 0

    if existing:
        status, body = request(f"{base}/api/v1/functions/id/{FUNCTION_ID}/update", key, payload)
    else:
        status, body = request(f"{base}/api/v1/functions/create", key, payload)
    if status not in (200, 201):
        print(f"{verb} failed: {status} {body}", file=sys.stderr)
        return 1

    status, stored = request(f"{base}/api/v1/functions/id/{FUNCTION_ID}", key)
    if status != 200 or not isinstance(stored, dict):
        print(f"installed, but reading it back failed: {status} {stored}", file=sys.stderr)
        return 1
    stored_content = stored.get("content") or ""
    if len(stored_content) != len(content):
        print(
            f"installed, but the stored content differs ({len(stored_content)} vs {len(content)} bytes); "
            "re-run the installer before using the button",
            file=sys.stderr,
        )
        return 1
    if stored.get("type") != EXPECTED_TYPE:
        print(
            f"installed, but Open WebUI stored type={stored.get('type')!r} instead of {EXPECTED_TYPE!r}. "
            f"Delete it under Admin -> Functions and re-run this script.",
            file=sys.stderr,
        )
        return 1

    if not stored.get("is_global"):
        status, body = request(f"{base}/api/v1/functions/id/{FUNCTION_ID}/toggle/global", key, {})
        if status not in (200, 201):
            print(f"could not make the action global: {status} {body}", file=sys.stderr)
            return 1
        status, stored = request(f"{base}/api/v1/functions/id/{FUNCTION_ID}", key)
        if status != 200 or not isinstance(stored, dict) or not stored.get("is_global"):
            print("the action is installed but not global; enable it in Admin -> Functions", file=sys.stderr)

    print(f"installed and verified: {len(content)} bytes, type={stored.get('type')}, "
          f"is_global={stored.get('is_global')}, is_active={stored.get('is_active')}")

    token_path = Path(args.token_file)
    if not args.no_token and token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            print(f"{token_path} is empty; start 'trick serve' once to create the token", file=sys.stderr)
            return 1
        status, body = request(
            f"{base}/api/v1/functions/id/{FUNCTION_ID}/valves/update", key, {"TRICK_TOKEN": token}
        )
        if status not in (200, 201):
            print(f"could not write the TRICK_TOKEN valve: {status} {body}", file=sys.stderr)
            return 1
        status, valves = request(f"{base}/api/v1/functions/id/{FUNCTION_ID}/valves", key)
        got = (valves or {}).get("TRICK_TOKEN") if isinstance(valves, dict) else None
        if got != token:
            print("the TRICK_TOKEN valve did not store the token; paste it by hand", file=sys.stderr)
            return 1
        print(f"TRICK_TOKEN valve set from {token_path} ({len(token)} characters, not printed)")
    else:
        print(f"next: paste the contents of {token_path} into the action's TRICK_TOKEN valve")

    # Valves are stored in Open WebUI's database, so a value written by an older
    # release outlives the code that defaulted it. Report what is actually stored
    # instead of assuming the new default took effect.
    status, valves = request(f"{base}/api/v1/functions/id/{FUNCTION_ID}/valves", key)
    if status == 200 and isinstance(valves, dict):
        sources = str(valves.get("SOURCES") or "always")
        print(f"SOURCES valve: {sources}")
        if sources.strip().lower() == "auto":
            print(
                "  note: 'auto' is the optional quiet mode (sources hidden when the chat "
                "matched). Set SOURCES to 'always' in Admin Settings -> Functions -> Trick "
                "to list them beside the conversation hits."
            )

    print("done: open a chat, press the Trick button under any answer, type what you are looking for")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
