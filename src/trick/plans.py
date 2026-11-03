"""Plans: the part of the commercial layer that is real logic.

The commercial split, stated once so the code cannot drift from it:

* The local build is free and complete. No plan, no token, no account, no
  network. ``local`` below exists only so the words "free tier" have a row in
  the table; nothing in the engine reads it.
* A paid plan buys *storage in someone else's rack* for the sealed archive. It
  cannot buy search quality, because the box never sees a query, a document or
  a score - it stores opaque bytes.

What is automatable and what is not, measured rather than assumed:

* AWS has no metering or invoicing API for resale. There is no call that says
  "charge this account two dollars". So the automated half is *entitlement*:
  a payment provider posts a signed webhook, this module verifies it and the
  store's limits change in one atomic write. The invoice itself stays with the
  payment provider, which is exactly what they are for.
* Limits are enforced in two places on purpose. ``max_bytes`` and
  ``max_objects`` are enforced by the server, because it can count bytes and
  files. ``max_rows`` and ``max_devices`` are policy: the server records a
  client-declared row count it cannot verify (the archive is sealed), and a
  device count it *can* see as distinct tokens. The docs say which is which
  instead of implying four equal guarantees.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import ConfigError

# Where the docs and the pricing page get every number from.
SIGNATURE_HEADER = "X-Trick-Signature"
SIGNATURE_PREFIX = "sha256="
DEFAULT_TOLERANCE_SECONDS = 300


@dataclass(frozen=True)
class Plan:
    """One row of the price table, and the limits it actually turns into."""

    name: str
    price_usd_month: float
    max_rows: int
    max_objects: int
    max_bytes: int
    max_devices: int
    retention_days: int
    blurb: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "price_usd_month": self.price_usd_month,
            "max_rows": self.max_rows,
            "max_objects": self.max_objects,
            "max_bytes": self.max_bytes,
            "max_devices": self.max_devices,
            "retention_days": self.retention_days,
            "blurb": self.blurb,
        }


# 512 MB / 20 GB / 1 TB transfer is the $5 Lightsail box. A measured archive is
# 380 KB for 77 decisions, so 5 000 rows is roughly 16 MB of ciphertext and
# 50 000 rows is roughly 160 MB - both comfortably inside one box per ~40 users.
PLANS: dict[str, Plan] = {
    "local": Plan(
        name="local",
        price_usd_month=0.0,
        max_rows=0,
        max_objects=0,
        max_bytes=0,
        max_devices=0,
        retention_days=0,
        blurb="no server; everything runs on your machine and nothing is billed",
    ),
    "backup": Plan(
        name="backup",
        price_usd_month=1.0,
        max_rows=5_000,
        max_objects=20,
        max_bytes=16 * 1024 * 1024,
        max_devices=3,
        retention_days=30,
        blurb="sealed memory backup: 5 000 decisions, 20 snapshots, 3 devices",
    ),
    "backup_plus": Plan(
        name="backup_plus",
        price_usd_month=3.0,
        max_rows=50_000,
        max_objects=100,
        max_bytes=160 * 1024 * 1024,
        max_devices=10,
        retention_days=90,
        blurb="sealed memory backup: 50 000 decisions, 100 snapshots, 10 devices",
    ),
    "owner": Plan(
        name="owner",
        price_usd_month=0.0,
        max_rows=1_000_000,
        max_objects=10_000,
        max_bytes=8 * 1024 * 1024 * 1024,
        max_devices=100,
        retention_days=0,
        blurb="self-hosted store; one operator, no billing",
    ),
}

DEFAULT_PLAN = "owner"

# Column order of the table in docs/PLANS.md. The gate parses that table and
# compares it cell by cell against ``plans_table()``, so this tuple is the
# contract between the pricing page and the code.
PLAN_LIMITS_COLUMNS = ("plan", "$/month", "rows", "snapshots", "bytes", "devices", "retention")


def plan(name: str) -> Plan:
    key = (name or "").strip()
    if key not in PLANS:
        known = ", ".join(sorted(PLANS))
        raise ConfigError(f"unknown plan {name!r}; known plans: {known}")
    return PLANS[key]


def plans_table() -> list[dict[str, Any]]:
    return [p.as_dict() for p in PLANS.values()]


def limits_for(name: str) -> dict[str, int]:
    """The five numbers an account entry carries, in accounts-file spelling."""
    p = plan(name)
    return {
        "max_rows": p.max_rows,
        "max_objects": p.max_objects,
        "max_bytes": p.max_bytes,
        "max_devices": p.max_devices,
        "retention_days": p.retention_days,
    }


# ---------------------------------------------------------------------------
# entitlement: the automatable half of resale
# ---------------------------------------------------------------------------
def sign(body: bytes, secret: str) -> str:
    """The signature a payment provider would send: HMAC over the raw body."""
    if not secret:
        raise ConfigError("an admin secret is required to sign an entitlement")
    return SIGNATURE_PREFIX + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_entitlement(
    body: bytes,
    header: str | None,
    secret: str,
    *,
    now: int | None = None,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
) -> dict[str, Any]:
    """Verify, then parse. A signature checked after parsing is theatre."""
    if not secret:
        raise ConfigError("the store has no admin secret; set TRICK_ADMIN_SECRET")
    if not header:
        raise ConfigError(f"missing {SIGNATURE_HEADER}")
    candidate = header.strip()
    if not candidate.startswith(SIGNATURE_PREFIX):
        raise ConfigError(f"{SIGNATURE_HEADER} must look like {SIGNATURE_PREFIX}<hex>")
    expected = sign(body, secret)
    if not hmac.compare_digest(candidate, expected):
        raise ConfigError("signature does not match the body")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigError(f"entitlement body is not JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ConfigError("entitlement body must be a JSON object")
    for key in ("account", "plan", "event_id"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"entitlement body needs a non-empty {key!r}")
    # A signature alone does not stop a replayed webhook from re-applying a
    # cancelled plan; the timestamp bounds that window, the event id closes it.
    stamp = payload.get("ts")
    if stamp is not None:
        try:
            age = abs(int(now if now is not None else time.time()) - int(stamp))
        except (TypeError, ValueError) as exc:
            raise ConfigError("entitlement 'ts' must be an integer epoch second") from exc
        if age > tolerance:
            raise ConfigError(f"entitlement is {age}s old, outside the {tolerance}s window")
    plan(str(payload["plan"]))  # fail before the store is touched
    return {
        "account": str(payload["account"]).strip(),
        "plan": str(payload["plan"]).strip(),
        "event_id": str(payload["event_id"]).strip(),
        "ts": int(stamp) if stamp is not None else None,
    }


@dataclass
class EntitlementLedger:
    """Applied event ids, so a retried webhook is a no-op instead of a refund."""

    path: Any
    events: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Any) -> "EntitlementLedger":
        from pathlib import Path

        path = Path(path)
        if not path.exists():
            return cls(path=path, events={})
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ConfigError(f"{path} is not JSON: {exc}") from exc
        events = raw.get("events") if isinstance(raw, dict) else None
        if not isinstance(events, dict):
            raise ConfigError(f"{path} has no events map")
        return cls(path=path, events={str(k): int(v) for k, v in events.items()})

    def seen(self, event_id: str) -> bool:
        return event_id in self.events

    def record(self, event_id: str, ts: int) -> None:
        import os
        from pathlib import Path

        self.events[event_id] = int(ts)
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"events": self.events}, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
