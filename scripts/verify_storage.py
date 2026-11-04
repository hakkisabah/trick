"""Prove that the storage backend can be swapped under a running product.

Not a unit test. This starts the real store as its own process over real TLS,
pushes a real archive, then moves the blobs to an S3-compatible server, restarts
the store, pulls the archive back and compares it, then moves the blobs back to
local disk and does it once more. The user-visible contract does not change:
same routes, same token, same archive, same bytes.

The S3 half is a server that *verifies* SigV4 signatures (tests/s3fake.py), not
AWS itself. That is deliberate: it is the strongest check that can run without
credentials, and it rejects a wrong secret, scope or payload hash the way S3
would. What it cannot prove is an account's IAM policy -- that is what
``trick vault storage test`` is for on the real box.

Every leg compares sha256 of the pulled file against the archive that was pushed,
and the final leg also compares decrypted payload and row counts.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import verify_remote as vr  # noqa: E402
from s3fake import ACCESS_KEY, BUCKET, REGION, SECRET_KEY, FakeS3  # noqa: E402


def storage_show(root: Path, env: dict | None = None) -> dict:
    code, out, err = vr.run(
        ["vault", "storage", "show", "--root", str(root)], env=env or os.environ.copy()
    )
    if code != 0:
        raise SystemExit(f"storage show failed: {err}")
    return json.loads(out)


def start_server(root: Path, cert: Path, key: Path, port: int, env: dict) -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "trick",
            "vault",
            "serve",
            "--root",
            str(root),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--cert",
            str(cert),
            "--key",
            str(key),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc


def wait_up(proc: subprocess.Popen, base_url: str, token: str, fingerprint: str) -> None:
    from trick.vaultremote import RemoteVault

    deadline = time.monotonic() + 25
    while True:
        if proc.poll() is not None:
            raise SystemExit(f"the store died on start:\n{proc.stderr.read() if proc.stderr else ''}")
        try:
            RemoteVault(base_url, token, fingerprint=fingerprint, timeout=2).ping()
            return
        except Exception:
            if time.monotonic() > deadline:
                raise SystemExit("the store did not answer within 25s")
            time.sleep(0.1)


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


def pull_and_hash(source: Path, sha: str, out: Path, base_url: str, fingerprint: str, env: dict) -> tuple[str, dict]:
    _, stdout, _ = vr.run(
        [
            "backup", "pull", sha,
            "--out", str(out),
            "--home", str(source),
            "--remote", base_url,
            "--fingerprint", fingerprint,
            "--verify",
        ],
        env=env,
    )
    return hashlib.sha256(out.read_bytes()).hexdigest(), json.loads(stdout)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="trick-verify-storage-"))
    server: subprocess.Popen | None = None
    legs: list[tuple[str, bool]] = []
    try:
        # 1. a store on local disk, with a real certificate
        root = tmp / "vault"
        _, out, _ = vr.run(["vault", "init", "--root", str(root)], env=os.environ.copy())
        token = json.loads(out)["token"]
        cert, key, fingerprint = vr.make_cert(tmp)
        port = vr.free_port()
        base_url = f"https://127.0.0.1:{port}"
        env = os.environ.copy()
        env.update(
            {
                "TRICK_BACKUP_PASSPHRASE": vr.PASSPHRASE,
                "TRICK_VAULT_TOKEN": token,
                "TRICK_S3_ACCESS_KEY": ACCESS_KEY,
                "TRICK_S3_SECRET_KEY": SECRET_KEY,
            }
        )

        # 2. a source cache: the real one if this machine has one
        source = tmp / "source"
        state = vr.real_state()
        if state is not None:
            source.mkdir(parents=True)
            shutil.copy2(state / "trick.db", source / "trick.db")
            origin = f"real state ({state})"
        else:
            vr.seed_cache(
                source,
                [(f"hash{i:03d}", "storage", 1_700_000_000 + i, json.dumps({"i": i})) for i in range(25)],
            )
            origin = "synthetic (no installed state on this machine)"
        archive = tmp / "memory.trk"
        vr.run(["backup", "export", str(archive), "--home", str(source), "--no-confirm"], env=env)
        archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()

        def leg(name: str, expect_backend: str, push: bool = False) -> str:
            """Start the store, read it back, compare. Returns the snapshot id."""
            nonlocal server
            info = storage_show(root, env)
            backend = info.get("config", {}).get("backend")
            print(f"  backend on disk   {backend} (objects listed: {info.get('objects')})")
            server = start_server(root, cert, key, port, env)
            wait_up(server, base_url, token, fingerprint)
            if push:
                vr.run(
                    ["backup", "push", str(archive), "--home", str(source),
                     "--remote", base_url, "--fingerprint", fingerprint],
                    env=env,
                )
            _, stdout, _ = vr.run(
                ["backup", "list", "--home", str(source), "--remote", base_url, "--fingerprint", fingerprint],
                env=env,
            )
            listing = json.loads(stdout)
            sha = listing[0]["sha256"]
            pulled = tmp / f"pulled-{name}.trk"
            digest, info_pulled = pull_and_hash(source, sha, pulled, base_url, fingerprint, env)
            ok = digest == archive_sha == sha
            legs.append((f"{name}: pulled bytes identical to the pushed archive", ok))
            print(f"  pull              {info_pulled['bytes']} bytes, {info_pulled['contents']['decisions']} decisions")
            print(f"  {'ok  ' if ok else 'FAIL'} sha256 {digest[:16]}…")
            assert backend == expect_backend, f"{name}: config says {backend}, expected {expect_backend}"
            stop(server)
            server = None
            return sha

        print(f"store           {base_url} (TLS, certificate pinned)")
        print(f"source cache    {origin}")
        print(f"archive         {archive.stat().st_size} bytes, sha256 {archive_sha[:16]}…")

        # 3. leg one: local disk
        print("leg 1  local disk")
        sha = leg("fs", "fs", push=True)

        # 4. leg two: S3-compatible object storage, which verifies signatures
        with FakeS3() as fake:
            print(f"leg 2  s3 ({fake.url}, SigV4 verified by the server)")
            _, stdout, err = vr.run(
                [
                    "vault", "storage", "migrate",
                    "--root", str(root),
                    "--backend", "s3",
                    "--endpoint", fake.url,
                    "--bucket", BUCKET,
                    "--region", REGION,
                ],
                env=env,
            )
            migrated = json.loads(stdout)
            print(f"  migrate           verified={migrated['verified']} bytes={migrated['bytes']} switched={migrated['switched']}")
            legs.append(("migrate fs -> s3 verified every object", migrated["switched"] and migrated["verified"] == 1))
            legs.append(("the bucket holds exactly one object", len(fake.objects) == 1))
            leg("s3", "s3")

            # 5. leg three: back to local disk, with the object store as the source
            print("leg 3  back to local disk")
            _, stdout, err = vr.run(
                ["vault", "storage", "migrate", "--root", str(root), "--backend", "fs"],
                env=env,
            )
            back = json.loads(stdout)
            print(f"  migrate           verified={back['verified']} bytes={back['bytes']} switched={back['switched']}")
            legs.append(("migrate s3 -> fs verified every object", back["switched"] and back["verified"] == 1))
            leg("fs-back", "fs")

        # 6. the last pull, imported into an empty installation
        target = tmp / "target"
        vr.seed_cache(target, [])
        vr.run(["backup", "import", str(tmp / "pulled-fs-back.trk"), "--home", str(target)], env=env)
        rows_equal = vr.rows_of(source) == vr.rows_of(target)
        legs.append(("rows identical after three backend legs", rows_equal))

        print(f"rows compared   {len(vr.rows_of(source))}")
        for name, ok in legs:
            print(f"  {'ok  ' if ok else 'FAIL'} {name}")
        if not all(ok for _, ok in legs):
            return 1
        print("storage backend: swappable, and the data survives every switch")
        return 0
    finally:
        if server is not None:
            stop(server)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
