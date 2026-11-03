"""The remote store: it must hold the same bytes, and prove it.

The claim being tested is narrow and checkable: an archive pushed to a store and
pulled back is byte-identical, decrypts to the same payload, and imports into an
empty installation as the same rows. Everything else here is the ways a remote
store betrays you - a wrong token, a swapped body, a full quota, a certificate
that is not the one you pinned - and each one must fail loudly rather than
produce a backup that is not one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("cryptography", reason="the optional backup extra is not installed")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from trick.errors import ConfigError, ContractViolation, StorageIO  # noqa: E402
from trick.vault import export_archive, import_archive, read_archive  # noqa: E402
from trick.vaultremote import (  # noqa: E402
    Account,
    RemoteVault,
    VaultServer,
    VaultStore,
    init_store,
    load_accounts,
    new_token,
    server_ssl_context,
    token_digest,
    write_accounts,
)

DDL = """
CREATE TABLE decisions (
    query_hash TEXT NOT NULL,
    scorer     TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (query_hash, scorer)
)
"""


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------
def make_home(tmp_path: Path, rows: list[tuple[str, str, int, str]] | None = None) -> Path:
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "trick.db")
    conn.execute(DDL)
    conn.executemany("INSERT INTO decisions VALUES (?, ?, ?, ?)", rows or [])
    conn.commit()
    conn.close()
    return home


def decisions(home: Path) -> list[tuple]:
    conn = sqlite3.connect(home / "trick.db")
    try:
        return sorted(conn.execute("SELECT query_hash, scorer, created_at, payload FROM decisions"))
    finally:
        conn.close()


def make_cert(tmp_path: Path) -> tuple[Path, Path, str]:
    """A self-signed certificate, and the fingerprint a client would pin."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    fingerprint = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return cert_path, key_path, fingerprint


@pytest.fixture()
def store(tmp_path):
    token = new_token()
    root = tmp_path / "vault"
    accounts = [Account(name="owner", token_sha256=token_digest(token), max_bytes=64 * 1024)]
    write_accounts(root / "accounts.json", accounts)
    return VaultStore(root, accounts), token, accounts[0]


@pytest.fixture()
def server(store, tmp_path):
    cert, key, fingerprint = make_cert(tmp_path)
    vault_store, token, _ = store
    srv = VaultServer(
        vault_store,
        host="127.0.0.1",
        port=0,
        tls=server_ssl_context(cert, key),
    )
    port = srv.start()
    try:
        yield f"https://127.0.0.1:{port}", token, fingerprint
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# parity: local == remote
# ---------------------------------------------------------------------------
def test_a_pushed_archive_is_byte_identical_to_the_local_one(tmp_path, server) -> None:
    url, token, fingerprint = server
    rows = [("h1", "remote", 100, "{}"), ("h2", "remote", 200, '{"n": 2}')]
    source = make_home(tmp_path / "src", rows)
    archive = tmp_path / "memory.trk"
    export_archive(source, archive, "pass phrase", {"LIMIT": 8})

    remote = RemoteVault(url, token, fingerprint=fingerprint)
    pushed = remote.put(archive.read_bytes(), rows=2, created_at=1234)
    assert pushed["verified"] is True
    assert pushed["bytes"] == archive.stat().st_size
    assert pushed["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()


def test_pull_returns_the_same_bytes_and_the_same_payload(tmp_path, server) -> None:
    url, token, fingerprint = server
    source = make_home(tmp_path / "src", [("h1", "remote", 100, "{}")])
    archive = tmp_path / "memory.trk"
    export_archive(source, archive, "pass phrase")

    remote = RemoteVault(url, token, fingerprint=fingerprint)
    remote.put(archive.read_bytes(), rows=1)
    back = tmp_path / "back.trk"
    result = remote.pull(remote.listing()[0]["sha256"], back, passphrase="pass phrase")

    assert back.read_bytes() == archive.read_bytes()
    assert result["contents"]["decisions"] == 1
    assert read_archive(back, "pass phrase")["decisions"] == read_archive(archive, "pass phrase")["decisions"]


def test_a_restored_remote_archive_rebuilds_the_same_rows(tmp_path, server) -> None:
    """The end of the story: a fresh install, a pulled archive, the same cache."""
    url, token, fingerprint = server
    rows = [
        ("h1", "remote", 100, '{"rank": [1, 2]}'),
        ("h1", "mock", 100, "{}"),
        ("h2", "remote", 300, "{}"),
    ]
    source = make_home(tmp_path / "src", rows)
    archive = tmp_path / "memory.trk"
    export_archive(source, archive, "pass phrase")

    remote = RemoteVault(url, token, fingerprint=fingerprint)
    remote.put(archive.read_bytes(), rows=len(rows))

    target = make_home(tmp_path / "dst", [])
    pulled = tmp_path / "pulled.trk"
    remote.pull(remote.listing()[0]["sha256"], pulled)
    merged = import_archive(pulled, target, "pass phrase")

    assert merged["added"] == len(rows)
    assert decisions(target) == decisions(source)


def test_pushing_the_same_archive_twice_stores_it_once(tmp_path, server) -> None:
    url, token, fingerprint = server
    source = make_home(tmp_path / "src", [("h1", "remote", 1, "{}")])
    archive = tmp_path / "memory.trk"
    export_archive(source, archive, "pass phrase")
    remote = RemoteVault(url, token, fingerprint=fingerprint)
    first = remote.put(archive.read_bytes(), rows=1)
    second = remote.put(archive.read_bytes(), rows=1)
    assert first["already_stored"] is False
    assert second["already_stored"] is True
    assert len(remote.listing()) == 1
    assert remote.usage()["objects"] == 1


def test_usage_reports_bytes_and_the_rows_unit_honestly(tmp_path, server) -> None:
    url, token, fingerprint = server
    source = make_home(tmp_path / "src", [("h1", "remote", 1, "{}")])
    archive = tmp_path / "memory.trk"
    export_archive(source, archive, "pass phrase")
    remote = RemoteVault(url, token, fingerprint=fingerprint)
    remote.put(archive.read_bytes(), rows=1)
    usage = remote.usage()
    assert usage["bytes"] == archive.stat().st_size
    assert usage["objects"] == 1
    assert "client-declared" in usage["rows_unit"]


def test_delete_removes_the_object_and_frees_the_quota(tmp_path, server) -> None:
    url, token, fingerprint = server
    source = make_home(tmp_path / "src", [("h1", "remote", 1, "{}")])
    archive = tmp_path / "memory.trk"
    export_archive(source, archive, "pass phrase")
    remote = RemoteVault(url, token, fingerprint=fingerprint)
    sha = remote.put(archive.read_bytes(), rows=1)["sha256"]
    remote.delete(sha)
    assert remote.listing() == []
    assert remote.usage()["bytes"] == 0
    with pytest.raises(ConfigError, match="no such snapshot"):
        remote.get(sha)


# ---------------------------------------------------------------------------
# the ways a store betrays you
# ---------------------------------------------------------------------------
def test_a_wrong_token_is_refused(tmp_path, server) -> None:
    url, _token, fingerprint = server
    wrong = RemoteVault(url, "x" * 40, fingerprint=fingerprint)
    with pytest.raises(ConfigError, match="401"):
        wrong.usage()


def test_no_token_is_refused(tmp_path, server) -> None:
    url, _token, fingerprint = server
    empty = RemoteVault(url, "y" * 40, fingerprint=fingerprint)
    with pytest.raises(ConfigError, match="401"):
        empty.listing()


def test_an_unknown_snapshot_is_404_not_an_empty_backup(tmp_path, server) -> None:
    url, token, fingerprint = server
    remote = RemoteVault(url, token, fingerprint=fingerprint)
    with pytest.raises(ConfigError, match="404"):
        remote.get("0" * 64)


def test_a_body_that_does_not_match_its_name_is_refused(store) -> None:
    vault_store, _token, account = store
    with pytest.raises(ContractViolation, match="hash mismatch"):
        vault_store.put(account, "a" * 64, b"not those bytes", 0, 0)


def test_an_object_over_the_quota_is_refused_before_it_is_written(store) -> None:
    vault_store, _token, account = store  # 64 KiB account
    blob = b"x" * (65 * 1024)
    sha = hashlib.sha256(blob).hexdigest()
    with pytest.raises(StorageIO, match="quota"):
        vault_store.put(account, sha, blob, 0, 0)
    assert vault_store.listing(account) == []
    assert not list((Path(vault_store.root) / "objects" / account.name).glob("*.trk"))


def test_a_stored_file_that_was_edited_on_disk_is_detected(store) -> None:
    vault_store, _token, account = store
    blob = b"TRICKV1\0real bytes"
    sha = hashlib.sha256(blob).hexdigest()
    vault_store.put(account, sha, blob, 0, 0)
    path = Path(vault_store.root) / "objects" / account.name / f"{sha}.trk"
    path.write_bytes(b"TRICKV1\0other bytes")
    with pytest.raises(StorageIO, match="no longer hashes"):
        vault_store.get(account, sha)


def test_a_certificate_that_is_not_the_pinned_one_is_refused(tmp_path, server) -> None:
    url, token, _fingerprint = server
    other = hashlib.sha256(b"some other certificate").hexdigest()
    remote = RemoteVault(url, token, fingerprint=other)
    with pytest.raises(ConfigError, match="fingerprint mismatch"):
        remote.usage()


def test_plaintext_http_is_refused_off_loopback() -> None:
    with pytest.raises(ConfigError, match="refusing plain http"):
        RemoteVault("http://vault.example.com:8790", "z" * 40)


def test_plaintext_is_allowed_on_loopback_only_with_the_flag() -> None:
    with pytest.raises(ConfigError, match="refusing plain http"):
        RemoteVault("http://127.0.0.1:8790", "z" * 40)
    RemoteVault("http://127.0.0.1:8790", "z" * 40, allow_plaintext=True)


def test_a_server_without_tls_refuses_to_start(store) -> None:
    vault_store, _token, _account = store
    with pytest.raises(ConfigError, match="only speaks TLS"):
        VaultServer(vault_store, port=0)


def test_a_wildcard_bind_is_refused(store, tmp_path) -> None:
    vault_store, _token, _account = store
    cert, key, _fp = make_cert(tmp_path)
    with pytest.raises(ConfigError, match="wildcard"):
        VaultServer(vault_store, host="0.0.0.0", port=0, tls=server_ssl_context(cert, key))


def test_a_wildcard_bind_is_allowed_only_with_tls(store, tmp_path) -> None:
    vault_store, _token, _account = store
    cert, key, _fp = make_cert(tmp_path)
    VaultServer(
        vault_store,
        host="0.0.0.0",
        port=0,
        tls=server_ssl_context(cert, key),
        expose_public=True,
    )
    with pytest.raises(ConfigError, match="requires TLS"):
        VaultServer(vault_store, host="0.0.0.0", port=0, allow_plaintext=True, expose_public=True)


def test_a_client_refuses_something_that_is_not_an_archive(server) -> None:
    url, token, fingerprint = server
    remote = RemoteVault(url, token, fingerprint=fingerprint)
    with pytest.raises(ConfigError, match="not a Trick archive"):
        remote.put(b"PK\x03\x04 a zip file")


def test_an_accounts_file_readable_by_others_is_refused(tmp_path) -> None:
    root = tmp_path / "vault"
    write_accounts(root / "accounts.json", [Account(name="owner", token_sha256="a" * 64)])
    (root / "accounts.json").chmod(0o644)
    with pytest.raises(ConfigError, match="readable by group"):
        load_accounts(root / "accounts.json")


def test_init_writes_one_account_and_shows_the_token_once(tmp_path) -> None:
    result = init_store(tmp_path / "vault", name="owner")
    assert len(result["token"]) >= 32
    accounts = load_accounts(Path(result["accounts"]))
    assert accounts[0].token_sha256 == token_digest(result["token"])
    assert result["token"] not in (Path(result["accounts"])).read_text()
    with pytest.raises(ConfigError, match="already exists"):
        init_store(tmp_path / "vault")


def test_the_token_can_come_from_the_environment(tmp_path, monkeypatch, server) -> None:
    from trick.vaultremote import read_token

    url, token, _fingerprint = server
    monkeypatch.setenv("TRICK_VAULT_TOKEN", token)
    assert read_token(tmp_path) == token


def test_a_token_file_readable_by_others_is_refused(tmp_path, monkeypatch) -> None:
    from trick.vaultremote import read_token

    monkeypatch.delenv("TRICK_VAULT_TOKEN", raising=False)
    path = tmp_path / "vault.token"
    path.write_text("t" * 40)
    path.chmod(0o644)
    with pytest.raises(ConfigError, match="readable by group"):
        read_token(tmp_path)


def test_the_store_never_sees_the_passphrase(tmp_path, server) -> None:
    """What is on the server's disk must not contain the secret or the plaintext."""
    url, token, fingerprint = server
    source = make_home(tmp_path / "src", [("h1", "remote", 1, '{"note": "haystack"}')])
    archive = tmp_path / "memory.trk"
    export_archive(source, archive, "correct horse battery staple")
    remote = RemoteVault(url, token, fingerprint=fingerprint)
    sha = remote.put(archive.read_bytes(), rows=1)["sha256"]
    on_disk = next(Path(tmp_path / "vault").rglob(f"{sha}.trk"))
    blob = on_disk.read_bytes()
    assert blob == archive.read_bytes()
    assert b"correct horse" not in blob
    assert b"haystack" not in blob
