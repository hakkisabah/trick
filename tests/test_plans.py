"""Plans, devices and the entitlement webhook.

What is being tested is the difference between a promise and an enforcement.
A price table that nothing reads is marketing; so each row here is checked
against the numbers the store actually enforces, and each limit is exercised
against a running server rather than inspected in a dict.

Three claims matter most:

* ``local`` costs nothing and is never consulted by the engine - the free build
  cannot accidentally start billing because no engine module imports plans.
* a device token minted for a plan authenticates over real HTTP, and the plan's
  device limit is refused, not ignored.
* a signed entitlement applies once. A replayed webhook is a no-op, an unsigned
  or stale one is refused, and a downgrade really lowers the ceiling.
"""

from __future__ import annotations

import http.client
import json
import time
from pathlib import Path

import pytest

from trick.errors import ConfigError
from trick.plans import (
    PLANS,
    EntitlementLedger,
    limits_for,
    plan,
    plans_table,
    sign,
    verify_entitlement,
)
from trick.vaultremote import (
    Account,
    VaultServer,
    VaultStore,
    init_store,
    load_accounts,
    new_token,
    token_digest,
    write_accounts,
)

SECRET = "test-admin-secret"


def make_store(tmp_path: Path, *, plan_name: str = "backup") -> tuple[VaultStore, str, Path]:
    token = new_token()
    root = tmp_path / "vault"
    limits = limits_for(plan_name)
    accounts = [
        Account(
            name="owner",
            token_sha256=token_digest(token),
            plan=plan_name,
            max_bytes=limits["max_bytes"],
            max_objects=limits["max_objects"],
            max_rows=limits["max_rows"],
            max_devices=limits["max_devices"],
            retention_days=limits["retention_days"],
        )
    ]
    path = root / "accounts.json"
    write_accounts(path, accounts)
    return VaultStore(root, accounts, accounts_path=path), token, path


@pytest.fixture()
def running(tmp_path):
    store, token, path = make_store(tmp_path)
    server = VaultServer(store, host="127.0.0.1", port=0, allow_plaintext=True, admin_secret=SECRET)
    port = server.start()
    try:
        yield f"127.0.0.1:{port}", store, token, path
    finally:
        server.stop()


def post_entitlement(host: str, body: bytes, signature: str | None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, timeout=10)
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Trick-Signature"] = signature
    try:
        conn.request("POST", "/v1/admin/entitlement", body=body, headers=headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        conn.close()


def get_usage(host: str, token: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, timeout=10)
    try:
        conn.request("GET", "/v1/usage", headers={"Authorization": f"Bearer {token}"})
        response = conn.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        conn.close()


def claim(account: str = "owner", plan_name: str = "backup_plus", event_id: str = "evt-1") -> bytes:
    return json.dumps(
        {
            "account": account,
            "plan": plan_name,
            "event_id": event_id,
            "ts": int(time.time()),
        }
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------
def test_the_price_table_is_the_limit_table() -> None:
    """Every plan carries the five numbers the store enforces, and they agree."""
    for row in plans_table():
        limits = limits_for(row["name"])
        for name, value in limits.items():
            assert value == row[name], f"{row['name']}.{name} disagrees with the table"
    assert PLANS["backup"].max_rows == 5_000
    assert PLANS["backup"].max_objects == 20
    assert PLANS["backup"].max_devices == 3
    assert PLANS["backup_plus"].max_rows == 50_000
    assert PLANS["backup_plus"].max_devices == 10
    assert PLANS["backup"].price_usd_month < PLANS["backup_plus"].price_usd_month


def test_the_local_row_is_free_and_grants_nothing() -> None:
    """The free build is not a hidden tier: it has no server allowances at all."""
    local = plan("local")
    assert local.price_usd_month == 0.0
    assert (local.max_rows, local.max_objects, local.max_bytes, local.max_devices) == (0, 0, 0, 0)


def test_the_engine_never_imports_the_billing_layer() -> None:
    """Search must not be able to depend on a plan, by construction."""
    engine_modules = [
        "search.py",
        "retrieval.py",
        "transcript.py",
        "textindex.py",
        "laya.py",
        "render.py",
    ]
    root = Path(__file__).resolve().parents[1] / "src" / "trick"
    for name in engine_modules:
        text = (root / name).read_text(encoding="utf-8")
        assert "from .plans" not in text and "import plans" not in text, f"{name} imports plans"


def test_an_unknown_plan_is_refused() -> None:
    with pytest.raises(ConfigError):
        plan("platinum")


# ---------------------------------------------------------------------------
# applying a plan
# ---------------------------------------------------------------------------
def test_applying_a_plan_rewrites_the_file_and_the_memory(tmp_path) -> None:
    store, _token, path = make_store(tmp_path, plan_name="backup")
    result = store.apply_plan("owner", "backup_plus")
    assert result["plan"] == "backup_plus"
    assert result["max_rows"] == 50_000
    assert store.account("owner").max_devices == 10
    reloaded = load_accounts(path)[0]
    assert reloaded.plan == "backup_plus"
    assert reloaded.max_objects == 100
    assert reloaded.retention_days == 90


def test_a_downgrade_lowers_the_ceiling_immediately(tmp_path) -> None:
    store, _token, path = make_store(tmp_path, plan_name="backup_plus")
    assert store.account("owner").max_bytes == limits_for("backup_plus")["max_bytes"]
    store.apply_plan("owner", "backup")
    lowered = limits_for("backup")["max_bytes"]
    assert store.account("owner").max_bytes == lowered
    assert store.usage(store.account("owner"))["max_bytes"] == lowered
    assert load_accounts(path)[0].max_bytes == lowered
    assert load_accounts(path)[0].max_objects == limits_for("backup")["max_objects"]


def test_applying_a_plan_keeps_existing_device_tokens(tmp_path) -> None:
    store, _token, _path = make_store(tmp_path)
    store.add_device("owner")
    before = store.account("owner").token_digests()
    store.apply_plan("owner", "backup_plus")
    after = store.account("owner").token_digests()
    assert before == after


def test_an_unknown_account_is_refused_not_created(tmp_path) -> None:
    store, _token, _path = make_store(tmp_path)
    with pytest.raises(ConfigError):
        store.apply_plan("stranger", "backup")


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------
def test_a_minted_device_token_authenticates_over_http(running) -> None:
    host, store, original, _path = running
    minted = store.add_device("owner")["token"]
    assert minted != original
    status, payload = get_usage(host, minted)
    assert status == 200 and payload["ok"] is True
    status, _ = get_usage(host, original)
    assert status == 200, "adding a device must not invalidate the first token"
    status, _ = get_usage(host, new_token())
    assert status == 401


def test_the_device_limit_is_a_limit(tmp_path) -> None:
    store, _token, _path = make_store(tmp_path, plan_name="backup")
    assert store.account("owner").max_devices == 3
    store.add_device("owner")
    store.add_device("owner")
    assert len(store.account("owner").token_digests()) == 3
    with pytest.raises(ConfigError) as caught:
        store.add_device("owner")
    assert "allows 3" in str(caught.value)


def test_the_device_token_is_not_stored_in_clear(tmp_path) -> None:
    store, _token, path = make_store(tmp_path)
    minted = store.add_device("owner")["token"]
    raw = path.read_text(encoding="utf-8")
    assert minted not in raw
    assert token_digest(minted) in raw


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------
def test_retention_deletes_old_snapshots_before_the_quota_is_hit(tmp_path) -> None:
    store, _token, _path = make_store(tmp_path, plan_name="backup")
    store.apply_plan("owner", "backup")
    account = store.account("owner")
    assert account.retention_days == 30
    import hashlib

    old = b"old-snapshot"
    new = b"new-snapshot"
    old_sha = hashlib.sha256(old).hexdigest()
    store.put(account, old_sha, old, 10, int(time.time()) - 40 * 86_400)
    assert old_sha in {row["sha256"] for row in store.listing(store.account("owner"))}
    store.put(account, hashlib.sha256(new).hexdigest(), new, 10, int(time.time()))
    remaining = {row["sha256"] for row in store.listing(store.account("owner"))}
    assert old_sha not in remaining, "a 30-day plan must not keep a 40-day-old snapshot"
    assert hashlib.sha256(new).hexdigest() in remaining


def test_a_plan_without_retention_never_prunes(tmp_path) -> None:
    store, _token, _path = make_store(tmp_path, plan_name="owner")
    account = store.account("owner")
    assert account.retention_days == 0
    import hashlib

    old = b"ancient"
    sha = hashlib.sha256(old).hexdigest()
    store.put(account, sha, old, 1, int(time.time()) - 5_000 * 86_400)
    assert sha in {row["sha256"] for row in store.listing(store.account("owner"))}


# ---------------------------------------------------------------------------
# entitlement webhook
# ---------------------------------------------------------------------------
def test_a_signed_entitlement_applies_the_plan(running) -> None:
    host, store, _token, _path = running
    body = claim(plan_name="backup_plus")
    status, payload = post_entitlement(host, body, sign(body, SECRET))
    assert status == 200 and payload["ok"] is True
    assert payload["result"]["applied"] is True
    assert store.account("owner").max_rows == 50_000


def test_a_replayed_entitlement_is_a_no_op(running) -> None:
    host, store, _token, _path = running
    body = claim(event_id="evt-repeat")
    signature = sign(body, SECRET)
    assert post_entitlement(host, body, signature)[0] == 200
    store.apply_plan("owner", "backup")
    status, payload = post_entitlement(host, body, signature)
    assert status == 200
    assert payload["result"]["applied"] is False
    assert store.account("owner").max_rows == limits_for("backup")["max_rows"], (
        "a replayed webhook must not silently re-apply an old plan"
    )


def test_an_unsigned_entitlement_is_refused(running) -> None:
    host, store, _token, _path = running
    body = claim()
    status, payload = post_entitlement(host, body, None)
    assert status == 401 and payload["ok"] is False
    assert store.account("owner").plan == "backup"


def test_a_tampered_body_is_refused(running) -> None:
    host, _store, _token, _path = running
    body = claim(plan_name="backup")
    signature = sign(body, SECRET)
    swapped = claim(plan_name="backup_plus")
    status, _payload = post_entitlement(host, swapped, signature)
    assert status == 401


def test_a_stale_entitlement_is_refused(running) -> None:
    host, _store, _token, _path = running
    body = json.dumps(
        {"account": "owner", "plan": "backup", "event_id": "evt-old", "ts": int(time.time()) - 3_600}
    ).encode("utf-8")
    status, _payload = post_entitlement(host, body, sign(body, SECRET))
    assert status == 401


def test_an_entitlement_for_an_unknown_account_is_404(running) -> None:
    host, _store, _token, _path = running
    body = claim(account="stranger")
    status, _payload = post_entitlement(host, body, sign(body, SECRET))
    assert status == 404


def test_the_admin_route_is_the_only_post(running) -> None:
    host, _store, _token, _path = running
    conn = http.client.HTTPConnection(host, timeout=10)
    try:
        conn.request("POST", "/v1/objects/" + "a" * 64, body=b"x", headers={})
        assert conn.getresponse().status == 404
    finally:
        conn.close()


def test_verifying_signature_happens_before_parsing() -> None:
    with pytest.raises(ConfigError) as caught:
        verify_entitlement(b"not json at all", "sha256=" + "0" * 64, SECRET)
    assert "signature" in str(caught.value)


def test_a_store_without_an_admin_secret_cannot_apply_anything(running) -> None:
    host, store, _token, path = running
    bare = VaultStore(store.root, load_accounts(path), accounts_path=path)
    server = VaultServer(bare, host="127.0.0.1", port=0, allow_plaintext=True)
    port = server.start()
    try:
        body = claim()
        status, payload = post_entitlement(f"127.0.0.1:{port}", body, sign(body, SECRET))
        assert status == 401
        assert "admin secret" in payload["error"]["message"]
    finally:
        server.stop()


def test_the_ledger_round_trips_and_refuses_a_corrupt_file(tmp_path) -> None:
    path = tmp_path / "entitlements.json"
    ledger = EntitlementLedger.load(path)
    assert not ledger.seen("evt-1")
    ledger.record("evt-1", 1_700_000_000)
    assert EntitlementLedger.load(path).seen("evt-1")
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigError):
        EntitlementLedger.load(path)


def test_init_applies_the_owner_limits(tmp_path) -> None:
    result = init_store(tmp_path / "vault")
    accounts = load_accounts(Path(result["accounts"]))
    assert accounts[0].plan == "owner"
    assert accounts[0].max_devices == limits_for("owner")["max_devices"]


# ---------------------------------------------------------------------------
# the surfaces a person actually touches
# ---------------------------------------------------------------------------
def test_the_installed_unit_can_receive_the_admin_secret() -> None:
    """A documented webhook that the unit cannot authenticate is a dead route."""
    unit = (Path(__file__).resolve().parents[1] / "deploy" / "vault-server.sh").read_text(encoding="utf-8")
    assert "EnvironmentFile=-$ETC/admin.env" in unit, "the unit has no place for TRICK_ADMIN_SECRET"
    assert "ETC=/etc/trick-vault" in unit


def run_cli(args: list[str], cwd: Path) -> tuple[int, str, str]:
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "trick", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_the_cli_lists_plans_and_moves_an_account(tmp_path) -> None:
    root = tmp_path / "vault"
    code, out, err = run_cli(["vault", "init", "--root", str(root)], tmp_path)
    assert code == 0, err
    code, out, err = run_cli(["vault", "plans"], tmp_path)
    assert code == 0, err
    assert {row["name"] for row in json.loads(out)} == set(PLANS)
    code, out, err = run_cli(
        ["vault", "plan", "--root", str(root), "--account", "owner", "--set", "backup"], tmp_path
    )
    assert code == 0, err
    assert json.loads(out)["max_rows"] == 5_000
    code, out, err = run_cli(["vault", "plan", "--root", str(root), "--account", "owner"], tmp_path)
    assert json.loads(out)["plan"] == "backup"
    code, out, err = run_cli(["vault", "device", "--root", str(root), "--account", "owner"], tmp_path)
    assert code == 0, err
    assert json.loads(out)["device"] == 2
    code, _out, err = run_cli(
        ["vault", "plan", "--root", str(root), "--account", "stranger", "--set", "backup"], tmp_path
    )
    assert code == 1 and "no such account" in err
