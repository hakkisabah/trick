# Plans, devices and the entitlement webhook

This document is the price table, and the gate (`scripts/check_plans.py`)
parses the table below and compares it cell by cell with `src/trick/plans.py`.
A price change that forgets this file fails the build, and so does a plan change
that this file still describes in the old numbers.

## What a plan buys, and what it cannot buy

A plan buys **space for a sealed archive on a server you do not own**. Nothing
else. The store never receives a key, so it cannot read a decision, a setting or
a query; it cannot improve a ranking, because it never sees one. Search quality
is identical on every plan, including the free one, because search never asks
the server anything.

The local build stays free and complete: no account, no token, no network. The
`local` row exists so that "free tier" has a row in the same table as the paid
ones, and the gate asserts that no engine module imports the billing layer.

## The table

The column order is `PLAN_LIMITS_COLUMNS` in `src/trick/plans.py`:
plan, $/month, rows, snapshots, bytes, devices, retention.

| plan | $/month | rows | snapshots | bytes | devices | retention |
| --- | --- | --- | --- | --- | --- | --- |
| local | 0 | 0 | 0 | 0 MiB | 0 | never |
| backup | 1 | 5,000 | 20 | 16 MiB | 3 | 30 days |
| backup_plus | 3 | 50,000 | 100 | 160 MiB | 10 | 90 days |
| owner | 0 | 1,000,000 | 10,000 | 8,192 MiB | 100 | never |

`owner` is the self-hosted row: one operator, no billing, high ceilings so a
self-hoster never meets a limit that exists only to price a stranger's disk.

## Which limits are enforced where

This distinction is not cosmetic; it is the difference between a limit and a
slogan.

| limit | enforced by | verifiable? |
| --- | --- | --- |
| bytes | the server, before a byte is written | yes: `VaultStore.put` counts the index |
| snapshots | the server, before a file is created | yes: object count in the index |
| devices | the server, at token mint time | yes: digests in `accounts.json` |
| rows | the client, before it uploads | no: the store cannot count rows inside ciphertext |
| retention | the server, pruned on the next write | yes: `created_at` in the index |

The server records the client-declared row count for display and says so in
`/v1/usage` (`rows_unit`). A store that claimed to enforce rows would be lying,
because the archive is sealed.

## Applying a plan

Two paths, and they write the same file:

```sh
# an operator, by hand
.venv/bin/python -m trick vault plan --root ~/.local/share/trick/vault-store \
    --account owner --set backup

# what a payment provider does: a signed webhook
curl -sS -X POST https://vault.example.com:8790/v1/admin/entitlement \
  -H 'Content-Type: application/json' \
  -H "X-Trick-Signature: $(.venv/bin/python -c 'import sys;from trick.plans import sign;print(sign(sys.stdin.buffer.read(), "ADMIN_SECRET"))' < body.json)" \
  --data-binary @body.json
```

The body is four fields:

```json
{"account": "owner", "plan": "backup", "event_id": "evt_1234", "ts": 1770000000}
```

* `account` must already exist. The webhook never creates an account, because a
  signed message from a payment provider should not be able to conjure a disk.
* `plan` must be one of the rows above; anything else is refused before the
  store is touched.
* `event_id` is recorded in `<root>/entitlements.json`. A replayed event is a
  no-op that returns `applied: false`, not a second downgrade or upgrade.
* `ts` is an epoch second and must be within 300 seconds of the server clock.
  The signature stops a stranger; the timestamp stops a replay of yesterday's
  cancellation.

Set the secret on the server with `TRICK_ADMIN_SECRET` (or `--admin-secret`).
Without it the route answers 401 and no plan can change.

## Devices

One account, one token per device, up to the plan's `devices`:

```sh
.venv/bin/python -m trick vault device --root ~/.local/share/trick/vault-store --account owner
```

The token is printed once and only its digest is stored, exactly like the first
one. Minting a fourth token on a three-device plan is refused with the limit in
the message, and the gate proves a minted token authenticates over HTTP while
the original keeps working.

## Cost

Measured, not estimated: one archive is 380,781 bytes for 77 decisions. At
64 MiB a user that is 168 archives; the $5 Lightsail box (nano_3_0: 2 shared
vCPUs, 512 MB RAM, 20 GB disk, 1 TB transfer) therefore holds roughly 100 users
with room for snapshots,
and 400 only if every user keeps a single snapshot. Per-user cost at 100 users
is $0.05/month. The box stores ciphertext and serves about 50 requests a day per
user; it runs no model, so CPU is never the limit.

## What is automated and what is not

AWS has no metering or invoicing API for resale: there is no call that says
"charge this account two dollars". So:

* automated: instance creation and destruction (`deploy/lightsail.sh`), and the
  entitlement above, which a payment provider drives with a webhook;
* not automated, by design: the customer's invoice, which belongs to the payment
  provider, and the plan-to-price mapping, which is this table.

`deploy/lightsail.sh --plan` prints the exact AWS commands without running them.
See the header of that script for the IAM policy it needs; the credentials this
repository ships with (`aws-yayin`) are publish-only and are refused by
Lightsail, which is the correct outcome rather than a workaround.
