# Remote memory

Optional. Nothing in Trick requires a server, and no local feature is paid. This
document is only about the one thing a local installation cannot do for itself:
keeping the decision cache somewhere else, so a dead disk is not a lost memory.

## What leaves the machine, and what does not

| thing | leaves? | why |
| --- | --- | --- |
| the catalogue (CRAN, Bioconductor, arXiv, OpenAlex) | no | 44 MB of public data; one ingest rebuilds it, and backing it up would be the only reason to need a bigger box |
| the score matrix | header only | the file is re-derivable; the backup records its count, dimensions and hash so a restore can say whether it belongs to the catalogue you now have |
| the decision cache | yes, sealed | a few hundred rows that cost model calls to produce; not re-derivable without paying again |
| the settings | yes, credentials replaced | every secret-shaped name is replaced by the environment variable that holds it |
| the passphrase | never | it is read from `TRICK_BACKUP_PASSPHRASE` or the terminal, and is not written to the archive or to any log |
| the API keys | never | see above; an archive that carries a key is a liability, not a backup |

The archive is `TRICKV1 | salt | nonce | AES-256-GCM(payload)`, keyed by
scrypt over your passphrase. The store receives that blob and nothing else, so
"the server can read my memory" is not a policy here, it is arithmetic: it has
no key.

## What the store learns anyway

Refusing to pretend: an encrypted blob still has a size and a timestamp. A store
operator can see that you backed up, when, and how large the archive was. That is
the whole leak, and it is why the store is not asked to hold anything else.

## Cost, with the arithmetic shown

One real archive from this installation: **380,781 bytes, 77 decisions**. That
number is the entire reason a $5 instance is enough.

| line | value | measured or derived |
| --- | --- | --- |
| instance | 2 vCPU (shared), 512 MB, 20 GB SSD, 1 TB transfer | Lightsail nano_3_0, $5/month with a public IPv4 (verified against the AWS bundle table) |
| store process resident memory | ~30 MB | derived: one Python process, no index, no model |
| one archive | 380,781 bytes | measured (`scripts/verify_remote.py`) |
| requests per backup | 4 (ping, put, read-back, usage) | derived from the client code |
| one account at the default quota | 64 MB, 200 snapshots | code default in `src/trick/vaultremote.py` |
| users that fit comfortably | 100 | derived: 100 x 64 MB = 6.4 GB of 20 GB, 100 users x ~1.5 MB/day = 150 MB/day of 1 TB/month |
| users at the edge | 400 | derived: 400 x 64 MB = 25.6 GB, above the 20 GB disk |
| cost per user at 100 users | $0.05/month | $5 / 100 |

These are arithmetic on a measured archive size, not a load test on a rented
box. No Lightsail instance was created while building this: renting one costs
money, and that is a decision for the person paying. When one exists, the same
`scripts/verify_remote.py` points at it and the numbers become measurements.

## Provisioning, in order

1. Create a Lightsail instance, Ubuntu 24.04, the $5 plan, and attach a static
   IP. Open only the store port in the instance firewall (Networking tab), for
   example 8790.
2. Copy this repository to the instance (any method: `scp`, `git clone`).
3. On the instance, as root:

   ```sh
   sudo sh deploy/vault-server.sh /opt/trick-src
   ```

   It creates a system user, a virtualenv, a 0700 store root, one account,
   a self-signed certificate valid ten years, a hardened systemd unit, and it
   prints the account token once and the certificate fingerprint.
4. Start it: `sudo systemctl start trick-vault`. It is enabled, not started, on
   purpose: an install that starts a listener without being asked is a surprise.
5. On the client, store the token where only you can read it:

   ```sh
   install -m 600 /dev/null ~/.local/share/trick/vault.token
   printf '%s' 'THE-TOKEN-PRINTED-ON-THE-SERVER' > ~/.local/share/trick/vault.token
   ```

## Where the bytes are kept

The store is a Linux process; where it writes is a choice. `<store-root>/storage.json`
names the backend, and nothing else about the deployment changes -- same routes,
same token, same quota, same TLS, same client commands.

* **`fs`** (default): blobs on the store's own disk, 0600, atomic write via a
  per-writer temporary file, fsync and rename. One box serving about a thousand
  snapshots fits in a 20 GB disk.
* **`s3`**: any S3-compatible endpoint (AWS S3, MinIO, Cloudflare R2, Backblaze
  B2) with path-style or virtual-host addressing. The signing is SigV4 over the
  real payload hash, implemented on the standard library, so no boto3 is needed
  on the smallest instance. Plain `http://` is accepted only for a loopback
  endpoint such as a MinIO on the same host; anything else must be TLS.

The index (`accounts.json`, `objects/<account>/index.json`) stays on local disk in
both cases. It is what the store locks while it enforces quota; S3 has no atomic
read-modify-write, so moving the index there would replace a lock with a race.

Credentials are not in that file. The store reads `TRICK_S3_ACCESS_KEY` and
`TRICK_S3_SECRET_KEY` from its environment (a systemd `EnvironmentFile`), or from a
file named by `credentials_file` that must be mode 0600 -- a world-readable
credential file is refused, not warned about.

```sh
# prove the bucket works before the store depends on it: write, read back, delete
.venv/bin/python -m trick vault storage test --root /var/lib/trick-vault \
    --backend s3 --endpoint https://s3.eu-central-1.amazonaws.com \
    --bucket YOUR-BUCKET --region eu-central-1 \
    --credentials-file /etc/trick/s3.credentials

# what the store is actually using
.venv/bin/python -m trick vault storage show --root /var/lib/trick-vault
```

### Moving existing snapshots to another backend

Content-addressed keys (`objects/<account>/<sha256>.trk`) are the same string in
every backend, so a migration is a copy plus a check, not a reformat.

```sh
systemctl --user stop trick-vault
.venv/bin/python -m trick vault storage migrate --root /var/lib/trick-vault \
    --backend s3 --endpoint https://s3.eu-central-1.amazonaws.com \
    --bucket YOUR-BUCKET --region eu-central-1 \
    --credentials-file /etc/trick/s3.credentials
systemctl --user start trick-vault
```

Order matters and is enforced: each object is read from the source, hashed,
written, read back from the target and hashed again; `storage.json` is rewritten
only after every object has passed. A refused write stops the run and leaves the
config alone, so the store never points at a half-copied target. `--dry-run`
counts without writing; `--keep-source` is the default, which is what makes the
reverse command (`--backend fs`) a migration back rather than a loss.

Deleting the old copy is a separate, deliberate step: do it after a real restore
from the new backend has been imported and compared, not after a green exit code.

The measured proof, run by `scripts/verify_storage.py` as part of the repository
gate: one real archive (380,781 bytes, 77 decisions) pushed through three legs --
local disk, S3, local disk again -- pulled back each time to the same sha256 and
imported to the same rows.

## Client use

```sh
.venv/bin/python -m trick backup export memory.trk
.venv/bin/python -m trick backup push memory.trk \
    --remote https://YOUR-STATIC-IP:8790 --fingerprint THE-FINGERPRINT
.venv/bin/python -m trick backup list --remote https://YOUR-STATIC-IP:8790 --fingerprint THE-FINGERPRINT
.venv/bin/python -m trick backup usage --remote https://YOUR-STATIC-IP:8790 --fingerprint THE-FINGERPRINT
.venv/bin/python -m trick backup pull SNAPSHOT-ID --out restored.trk \
    --remote https://YOUR-STATIC-IP:8790 --fingerprint THE-FINGERPRINT --verify
.venv/bin/python -m trick backup import restored.trk
```

`push` reads the object back and compares the hash before it reports success. An
upload that was never read back is a hope, not a backup.

`pull --verify` decrypts the pulled file and prints what is inside, so a restore
is proven before it is merged.

## Scheduling

A manual push is a backup that happens when someone remembers. The timer runs one
command, `.venv/bin/python -m trick backup run`, which exports into a temporary
file, pushes it, verifies the read-back and deletes the local copy:

```sh
sh deploy/backup-timer.sh --remote=https://YOUR-STATIC-IP:8790 \
    --fingerprint=THE-FINGERPRINT --start
```

The passphrase lives in `~/.config/trick/backup.env` at mode 600, not in the unit
and not on the command line: an argument is visible in `ps` and in shell history.
The script refuses to enable the timer while that file has no passphrase, because
a timer that fails every night still looks scheduled.

## The token and the fingerprint are two different locks

* the **token** proves the client may write to the account. It is 32 random
  bytes, compared in constant time, stored on the server only as a SHA-256.
* the **fingerprint** proves the server is the one you meant. The client pins
  the SHA-256 of the server's DER certificate and refuses anything else, which is
  why a self-signed certificate is enough: the identity is the hash, not a name
  in a public CA's book. There is no renewal to forget and no CA to be tricked.

Neither replaces the passphrase. The store cannot decrypt the archive even with
both.

## Quotas, stated as they are enforced

| limit | enforced by | effect when exceeded |
| --- | --- | --- |
| `max_bytes` (default 64 MB) | server | 507, with the numbers in the message |
| `max_objects` (default 200) | server | 507 |
| `max_rows` (default 50000) | client | the client refuses to push |

The row limit cannot be enforced by the server: it cannot count rows inside a
payload it cannot decrypt. It is recorded for display and enforced on the client,
and the `usage` output says so in the `rows_unit` field rather than implying an
enforcement that does not exist.

## When the box dies

The archive is a file. If the instance is lost, the last snapshot is gone with
it, so back up what you care about twice: keep a local export as well, or run the
same store on two hosts and push to both. Trick does not pretend a single $5 box
is durable storage; it makes the second copy cheap.

## Loss of the passphrase

There is no recovery. The store has no key, the archive has no key, and nobody
can open it. That is the same property that makes the design safe: write the
passphrase down somewhere that is not the disk being backed up.

## Rotation

* **token**: delete the account from the server's `accounts.json`, run
  `python -m trick vault init` against a fresh root, and put the new token in
  the client's `vault.token`.
* **certificate**: regenerate the pair, restart the service, and update the
  client's `--fingerprint`. The old fingerprint stops working immediately, which
  is the point.
