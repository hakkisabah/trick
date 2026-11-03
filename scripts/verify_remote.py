"""Measure the remote store end to end, over real TLS, with the real CLI.

Not a unit test: this starts the server as its own process, generates a
certificate, exports a real archive from the real state directory, pushes it
through ``trick backup push``, pulls it back through ``trick backup pull``,
imports it into an empty installation and compares the rows. Then it reports the
numbers. If any of those three comparisons fail, the script exits non-zero and
the repository gate fails with it.

What is compared, and why these three:

  bytes   the pulled file must hash to the pushed file's sha256. Anything less
          means the store altered the backup.
  payload the decrypted JSON must be equal key for key. The archive is
          authenticated, so this also proves nothing on the wire flipped.
  rows    importing the pulled archive into an empty cache must produce exactly
          the rows the source cache holds. This is the only comparison a user
          actually cares about.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASSPHRASE = "verify-remote-passphrase"
DDL = """
CREATE TABLE decisions (
    query_hash TEXT NOT NULL,
    scorer     TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (query_hash, scorer)
)
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_cert(tmp: Path) -> tuple[Path, Path, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp / "cert.pem", tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return cert_path, key_path, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def run(args: list[str], *, env: dict[str, str], check: bool = True) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "trick", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and proc.returncode != 0:
        raise SystemExit(f"trick {' '.join(args)} failed ({proc.returncode}):\n{proc.stderr}")
    return proc.returncode, proc.stdout, proc.stderr


def seed_cache(home: Path, rows: list[tuple[str, str, int, str]]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "trick.db")
    conn.execute(DDL)
    conn.executemany("INSERT INTO decisions VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def rows_of(home: Path) -> list[tuple]:
    conn = sqlite3.connect(home / "trick.db")
    try:
        return sorted(conn.execute("SELECT query_hash, scorer, created_at, payload FROM decisions"))
    finally:
        conn.close()


def real_state() -> Path | None:
    """The installed state, if there is one: a real archive beats a synthetic one."""
    home = Path(os.environ.get("TRICK_HOME", Path.home() / ".local/share/trick"))
    db = home / "trick.db"
    if not db.exists():
        return None
    return home


def main() -> int:
    if shutil.which(sys.executable) is None:  # pragma: no cover
        raise SystemExit("no interpreter")
    tmp = Path(tempfile.mkdtemp(prefix="trick-verify-remote-"))
    server: subprocess.Popen | None = None
    try:
        # 1. a store, with one account and a TLS certificate
        root = tmp / "vault"
        _, out, _ = run(["vault", "init", "--root", str(root)], env=os.environ.copy())
        token = json.loads(out)["token"]
        cert, key, fingerprint = make_cert(tmp)
        port = free_port()
        base_url = f"https://127.0.0.1:{port}"

        env = os.environ.copy()
        env.update(
            {
                "TRICK_BACKUP_PASSPHRASE": PASSPHRASE,
                "TRICK_VAULT_TOKEN": token,
            }
        )
        server = subprocess.Popen(
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
        deadline = time.monotonic() + 20
        from trick.vaultremote import RemoteVault

        assert server is not None
        while True:
            if server.poll() is not None:
                raise SystemExit(f"the store died on start:\n{server.stderr.read()}")
            try:
                RemoteVault(base_url, token, fingerprint=fingerprint, timeout=2).ping()
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise SystemExit("the store did not answer within 20s")
                time.sleep(0.1)

        # 2. a source cache: the real one when this machine has one
        source = tmp / "source"
        state = real_state()
        if state is not None:
            source.mkdir(parents=True)
            shutil.copy2(state / "trick.db", source / "trick.db")
            origin = f"real state ({state})"
        else:
            seed_cache(
                source,
                [(f"hash{i:03d}", "remote", 1_700_000_000 + i, json.dumps({"i": i})) for i in range(25)],
            )
            origin = "synthetic (no installed state on this machine)"

        # 3. export, push, pull, import
        archive = tmp / "memory.trk"
        run(["backup", "export", str(archive), "--home", str(source), "--no-confirm"], env=env)

        _, out, _ = run(
            ["backup", "push", str(archive), "--home", str(source), "--remote", base_url, "--fingerprint", fingerprint],
            env=env,
        )
        pushed = json.loads(out)

        _, out, _ = run(
            ["backup", "list", "--home", str(source), "--remote", base_url, "--fingerprint", fingerprint],
            env=env,
        )
        listing = json.loads(out)
        sha = listing[0]["sha256"]

        pulled = tmp / "pulled.trk"
        _, out, _ = run(
            [
                "backup",
                "pull",
                sha,
                "--out",
                str(pulled),
                "--home",
                str(source),
                "--remote",
                base_url,
                "--fingerprint",
                fingerprint,
                "--verify",
            ],
            env=env,
        )
        pulled_info = json.loads(out)

        target = tmp / "target"
        seed_cache(target, [])
        run(["backup", "import", str(pulled), "--home", str(target)], env=env)

        # 4. three comparisons
        local_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        remote_sha = hashlib.sha256(pulled.read_bytes()).hexdigest()
        bytes_equal = local_sha == remote_sha == pushed["sha256"]

        from trick.vault import read_archive

        payload_equal = read_archive(archive, PASSPHRASE) == read_archive(pulled, PASSPHRASE)
        rows_equal = rows_of(source) == rows_of(target)
        same_count = len(rows_of(source))

        _, out, _ = run(
            ["backup", "usage", "--home", str(source), "--remote", base_url, "--fingerprint", fingerprint],
            env=env,
        )
        usage = json.loads(out)

        print(f"store           {base_url} (TLS, certificate pinned)")
        print(f"source cache    {origin}")
        print(f"archive         {archive.stat().st_size} bytes, sha256 {local_sha[:16]}…")
        print(f"push            {pushed['bytes']} bytes, verified={pushed['verified']}, rows={pushed['rows']}")
        print(f"pull            {pulled_info['bytes']} bytes, {pulled_info['contents']['decisions']} decisions")
        print(f"rows compared   {same_count}")
        print(f"usage           {usage['bytes']} bytes, {usage['objects']} object(s)")
        for name, ok in (
            ("bytes identical", bytes_equal),
            ("payload identical", payload_equal),
            ("rows identical", rows_equal),
        ):
            print(f"  {'ok  ' if ok else 'FAIL'} {name}")
        if not (bytes_equal and payload_equal and rows_equal):
            return 1
        print("remote store: parity holds")
        return 0
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                server.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
