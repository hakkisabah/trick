#!/usr/bin/env python3
"""Gate: the price table, the docs and the enforcement must be the same table.

Three things can drift apart and each one has bitten a pricing page somewhere:

1. the numbers in the code and the numbers in ``docs/PLANS.md`` - the markdown
   table is parsed and compared, so a plan change that forgets the docs fails;
2. the promise and the enforcement - a signed entitlement is posted to a real
   loopback store and the *applied* limits are read back over HTTP;
3. the guard - an unsigned, tampered or replayed entitlement must not change
   anything. A webhook that applies twice is a refund.

Exit code 0 means all three agree.
"""

from __future__ import annotations

import http.client
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trick.plans import PLAN_LIMITS_COLUMNS, sign, verify_entitlement  # noqa: E402
from trick.vaultremote import (  # noqa: E402
    Account,
    VaultServer,
    VaultStore,
    load_accounts,
    new_token,
    token_digest,
    write_accounts,
)

SECRET = "gate-admin-secret"
DOC = ROOT / "docs" / "PLANS.md"


def parse_doc_table(text: str) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if len(cells) != len(PLAN_LIMITS_COLUMNS) or cells[0] in ("plan", "---"):
            continue
        if cells[0].startswith("-"):
            continue
        rows[cells[0]] = cells
    return rows


def main() -> int:
    from trick.plans import PLANS

    if not DOC.exists():
        print(f"gate: {DOC} is missing", file=sys.stderr)
        return 1
    documented = parse_doc_table(DOC.read_text(encoding="utf-8"))
    missing = sorted(set(PLANS) - set(documented))
    if missing:
        print(f"gate: docs/PLANS.md does not document: {', '.join(missing)}", file=sys.stderr)
        return 1
    for name, plan in PLANS.items():
        cells = documented[name]
        expected = [
            name,
            f"{plan.price_usd_month:g}",
            f"{plan.max_rows:,}",
            f"{plan.max_objects:,}",
            f"{plan.max_bytes // (1024 * 1024):,} MiB",
            f"{plan.max_devices:,}",
            ("never" if plan.retention_days == 0 else f"{plan.retention_days} days"),
        ]
        if cells != expected:
            print(
                f"gate: docs/PLANS.md row {name!r} says {cells}, the code says {expected}",
                file=sys.stderr,
            )
            return 1

    # enforcement, over real HTTP, against a real store
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "vault"
        token = new_token()
        accounts = [Account(name="owner", token_sha256=token_digest(token))]
        path = root / "accounts.json"
        write_accounts(path, accounts)
        store = VaultStore(root, load_accounts(path), accounts_path=path)
        server = VaultServer(store, host="127.0.0.1", port=0, allow_plaintext=True, admin_secret=SECRET)
        port = server.start()

        def post(body: bytes, header: str | None) -> tuple[int, dict]:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            headers = {"Content-Type": "application/json"}
            if header:
                headers["X-Trick-Signature"] = header
            try:
                conn.request("POST", "/v1/admin/entitlement", body=body, headers=headers)
                response = conn.getresponse()
                return response.status, json.loads(response.read().decode("utf-8"))
            finally:
                conn.close()

        def usage() -> dict:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request("GET", "/v1/usage", headers={"Authorization": f"Bearer {token}"})
                return json.loads(conn.getresponse().read().decode("utf-8"))["result"]
            finally:
                conn.close()

        try:
            claim = json.dumps(
                {"account": "owner", "plan": "backup", "event_id": "gate-1", "ts": int(time.time())}
            ).encode("utf-8")
            status, _ = post(claim, None)
            if status != 401:
                print(f"gate: an unsigned entitlement returned {status}, not 401", file=sys.stderr)
                return 1
            status, payload = post(claim, sign(claim, SECRET))
            if status != 200 or payload["result"]["applied"] is not True:
                print(f"gate: a signed entitlement did not apply ({status})", file=sys.stderr)
                return 1
            live = usage()
            if live["max_rows"] != PLANS["backup"].max_rows or live["plan"] != "backup":
                print(f"gate: applied limits are {live['max_rows']}, table says {PLANS['backup'].max_rows}", file=sys.stderr)
                return 1
            status, payload = post(claim, sign(claim, SECRET))
            if status != 200 or payload["result"]["applied"] is not False:
                print("gate: a replayed entitlement was applied twice", file=sys.stderr)
                return 1
            stale = json.dumps(
                {"account": "owner", "plan": "backup_plus", "event_id": "gate-2", "ts": int(time.time()) - 3600}
            ).encode("utf-8")
            status, _ = post(stale, sign(stale, SECRET))
            if status != 401:
                print(f"gate: a stale entitlement returned {status}, not 401", file=sys.stderr)
                return 1
            if verify_entitlement(claim, sign(claim, SECRET), SECRET)["plan"] != "backup":
                print("gate: signature verification changed the claim", file=sys.stderr)
                return 1
        finally:
            server.stop()

    print(
        f"plans ok: {len(PLANS)} rows match docs/PLANS.md, entitlement applies once "
        f"({PLANS['backup'].max_rows:,} rows), unsigned/stale refused"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
