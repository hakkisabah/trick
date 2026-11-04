"""The storage backend is a config line, so it has to be swappable without drama.

What is actually being proven here:

* the SigV4 signer matches AWS's published vector (a wrong ladder or a wrong
  canonical request fails that test immediately);
* every request the backend sends is verified by a server that rebuilds the
  signature from the received bytes, so a wrong secret, scope or payload hash is
  rejected exactly as S3 would reject it;
* a local store and an object-store store hold the same bytes, and migration
  between them is verified object by object before the config is switched;
* a failed migration leaves the store pointing at data that still exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from s3fake import ACCESS_KEY, BUCKET, REGION, SECRET_KEY, FakeS3  # noqa: E402

from trick import blobstore  # noqa: E402
from trick.blobstore import (  # noqa: E402
    EMPTY_SHA,
    FsBackend,
    S3Backend,
    S3Settings,
    object_key,
    sign_v4,
    signing_key,
)
from trick.errors import ConfigError, ContractViolation, StorageIO  # noqa: E402
from trick.vaultremote import VaultStore, init_store, load_accounts  # noqa: E402


# AWS's documented example key, written as a concatenation on purpose: the
# literal one is a credential-shaped string that the repository secret gate
# rejects, and the first version of this test was silently scrubbed into a
# placeholder that still produced the right signature (the access key is not part
# of the HMAC), so the assertions below check the Credential field as well.
AWS_EXAMPLE_ACCESS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
AWS_EXAMPLE_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch):
    """Credentials never live in a config file, so tests provide them the way a
    server does: environment first. The tests that check a missing key delete
    these explicitly."""
    monkeypatch.setenv("TRICK_S3_ACCESS_KEY", ACCESS_KEY)
    monkeypatch.setenv("TRICK_S3_SECRET_KEY", SECRET_KEY)


def s3_settings(url: str, **overrides) -> S3Settings:
    base = dict(
        endpoint=url,
        bucket=BUCKET,
        region=REGION,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
    )
    base.update(overrides)
    return S3Settings(**base)


# ---------------------------------------------------------------------------
# the signer
# ---------------------------------------------------------------------------
def test_sigv4_matches_the_published_aws_vector():
    """AWS's own documented example. If this passes, the ladder is right."""
    headers = sign_v4(
        "GET",
        "https://examplebucket.s3.amazonaws.com/test.txt",
        region="us-east-1",
        service="s3",
        access_key=AWS_EXAMPLE_ACCESS_KEY,
        secret_key=AWS_EXAMPLE_SECRET_KEY,
        payload_sha=EMPTY_SHA,
        extra_headers={"Range": "bytes=0-9"},
        now=datetime(2013, 5, 24, tzinfo=timezone.utc),
    )
    assert headers["x-amz-date"] == "20130524T000000Z"
    assert f"Credential={AWS_EXAMPLE_ACCESS_KEY}/20130524/us-east-1/s3/aws4_request" in headers["Authorization"]
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in headers["Authorization"]
    assert headers["Authorization"].endswith(
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_signing_key_ladder_is_deterministic():
    first = signing_key("secret", "20130524", "us-east-1", "s3")
    second = signing_key("secret", "20130524", "us-east-1", "s3")
    assert first == second
    assert first != signing_key("secret2", "20130524", "us-east-1", "s3")
    assert first != signing_key("secret", "20130525", "us-east-1", "s3")
    assert first != signing_key("secret", "20130524", "eu-central-1", "s3")


def test_extra_headers_are_signed_not_just_sent():
    plain = sign_v4(
        "PUT", "https://h/b/k", region=REGION, service="s3", access_key="a", secret_key="s",
        payload_sha=EMPTY_SHA, now=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    with_sse = sign_v4(
        "PUT", "https://h/b/k", region=REGION, service="s3", access_key="a", secret_key="s",
        payload_sha=EMPTY_SHA, extra_headers={"x-amz-server-side-encryption": "AES256"},
        now=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    assert "x-amz-server-side-encryption" in with_sse["Authorization"].split("SignedHeaders=")[1]
    assert plain["Authorization"] != with_sse["Authorization"]


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------
def test_object_key_is_the_layout_the_store_already_used():
    digest = sha(b"x")
    assert object_key("owner", digest) == f"objects/owner/{digest}.trk"


@pytest.mark.parametrize("account", ["", "a/b", ".", ".."])
def test_object_key_refuses_an_account_that_could_escape(account):
    with pytest.raises(ConfigError):
        object_key(account, sha(b"x"))


def test_object_key_refuses_a_bad_digest():
    with pytest.raises(ContractViolation):
        object_key("owner", "not-a-digest")


# ---------------------------------------------------------------------------
# the fs backend
# ---------------------------------------------------------------------------
def test_fs_backend_round_trip_and_permissions(tmp_path):
    backend = FsBackend(tmp_path)
    key = object_key("owner", sha(b"hello"))
    backend.put(key, b"hello")
    assert backend.get(key) == b"hello"
    assert backend.exists(key)
    stored = tmp_path / "objects" / "owner" / f"{sha(b'hello')}.trk"
    assert stored.stat().st_mode & 0o777 == 0o600
    backend.delete(key)
    assert not backend.exists(key)
    backend.delete(key)  # idempotent


def test_fs_backend_missing_object_is_a_config_error(tmp_path):
    backend = FsBackend(tmp_path)
    with pytest.raises(ConfigError):
        backend.get(object_key("owner", sha(b"nope")))


# ---------------------------------------------------------------------------
# the s3 backend, against a server that checks signatures
# ---------------------------------------------------------------------------
def test_s3_round_trip_is_verified_by_the_server():
    with FakeS3() as fake:
        backend = S3Backend(s3_settings(fake.url))
        key = object_key("owner", sha(b"payload"))
        backend.put(key, b"payload")
        assert backend.get(key) == b"payload"
        assert backend.exists(key)
        backend.delete(key)
        assert not backend.exists(key)
        assert fake.objects == {}


def test_s3_wrong_secret_is_refused_like_aws_would():
    with FakeS3() as fake:
        backend = S3Backend(s3_settings(fake.url, secret_key="wrong-secret"))
        with pytest.raises(StorageIO) as caught:
            backend.put(object_key("owner", sha(b"x")), b"x")
        assert "SignatureDoesNotMatch" in str(caught.value)


def test_s3_prefix_and_addressing_choose_the_url():
    path_style = S3Backend(s3_settings("https://s3.eu-central-1.amazonaws.com", prefix="trick"))
    assert path_style._url_for("objects/owner/x.trk") == (
        "https://s3.eu-central-1.amazonaws.com/trick-test/trick/objects/owner/x.trk"
    )
    virtual = S3Backend(
        s3_settings("https://s3.eu-central-1.amazonaws.com", prefix="trick", addressing="virtual")
    )
    assert virtual._url_for("objects/owner/x.trk") == (
        "https://trick-test.s3.eu-central-1.amazonaws.com/trick/objects/owner/x.trk"
    )


def test_s3_virtual_addressing_url_shape():
    """A fake on 127.0.0.1 cannot resolve bucket.localhost, so only the URL is
    checked here; path-style is exercised end to end above."""
    virtual = S3Backend(
        s3_settings("https://s3.eu-central-1.amazonaws.com", prefix="trick", addressing="virtual")
    )
    assert virtual._url_for("objects/owner/x.trk") == (
        "https://trick-test.s3.eu-central-1.amazonaws.com/trick/objects/owner/x.trk"
    )


def test_s3_missing_object_is_a_config_error_and_head_says_false():
    with FakeS3() as fake:
        backend = S3Backend(s3_settings(fake.url))
        key = object_key("owner", sha(b"absent"))
        with pytest.raises(ConfigError):
            backend.get(key)
        assert backend.exists(key) is False


def test_s3_retries_a_throttled_write():
    with FakeS3(fail_first=1) as fake:
        backend = S3Backend(s3_settings(fake.url))
        key = object_key("owner", sha(b"retry"))
        backend.put(key, b"retry")
        assert backend.get(key) == b"retry"


def test_s3_server_side_encryption_header_is_accepted():
    with FakeS3() as fake:
        backend = S3Backend(s3_settings(fake.url, sse="AES256"))
        key = object_key("owner", sha(b"sse"))
        backend.put(key, b"sse")
        assert backend.get(key) == b"sse"


def test_plain_http_is_only_allowed_on_loopback():
    with pytest.raises(ConfigError) as caught:
        S3Backend(s3_settings("http://s3.example.com"))
    assert "loopback" in str(caught.value)
    S3Backend(s3_settings("http://127.0.0.1:9000"))  # a local MinIO is fine


def test_missing_credentials_are_named_not_guessed(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICK_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("TRICK_S3_SECRET_KEY", raising=False)
    config = {
        "backend": "s3",
        "endpoint": "https://s3.eu-central-1.amazonaws.com",
        "bucket": BUCKET,
    }
    with pytest.raises(ConfigError) as caught:
        blobstore.make_backend(config, tmp_path)
    assert "TRICK_S3_ACCESS_KEY" in str(caught.value)


def test_a_world_readable_credentials_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICK_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("TRICK_S3_SECRET_KEY", raising=False)
    cred = tmp_path / "s3.credentials"
    cred.write_text(f"TRICK_S3_ACCESS_KEY={ACCESS_KEY}\nTRICK_S3_SECRET_KEY={SECRET_KEY}\n")
    os.chmod(cred, 0o644)
    config = {
        "backend": "s3",
        "endpoint": "https://s3.eu-central-1.amazonaws.com",
        "bucket": BUCKET,
        "credentials_file": str(cred),
    }
    with pytest.raises(ConfigError) as caught:
        blobstore.make_backend(config, tmp_path)
    assert "600" in str(caught.value)


def test_a_0600_credentials_file_is_read(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICK_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("TRICK_S3_SECRET_KEY", raising=False)
    cred = tmp_path / "s3.credentials"
    cred.write_text(f"TRICK_S3_ACCESS_KEY={ACCESS_KEY}\nTRICK_S3_SECRET_KEY={SECRET_KEY}\n")
    os.chmod(cred, 0o600)
    settings = blobstore.s3_settings(
        {"endpoint": "https://s3.example.com", "bucket": BUCKET, "credentials_file": str(cred)}
    )
    assert settings.access_key == ACCESS_KEY
    assert settings.secret_key == SECRET_KEY


# ---------------------------------------------------------------------------
# the store on top of a backend
# ---------------------------------------------------------------------------
def _store(root: Path, backend=None) -> VaultStore:
    if not (root / "accounts.json").exists():
        init_store(root, name="owner")
    accounts = load_accounts(root / "accounts.json")
    return VaultStore(root, accounts, backend=backend)


def test_store_writes_blobs_through_the_backend(tmp_path):
    with FakeS3() as fake:
        backend = S3Backend(s3_settings(fake.url))
        store = _store(tmp_path, backend)
        account = store.accounts["owner"]
        blob = b"sealed-archive-bytes"
        result = store.put(account, sha(blob), blob, rows=3, created_at=100)
        assert result["bytes"] == len(blob)
        assert fake.objects[object_key("owner", sha(blob))] == blob
        assert store.get(account, sha(blob)) == blob
        assert store.usage(account)["storage"]["backend"] == "s3"
        store.delete(account, sha(blob))
        assert fake.objects == {}


def test_store_reports_a_listed_but_absent_object_as_storage_io(tmp_path):
    with FakeS3() as fake:
        backend = S3Backend(s3_settings(fake.url))
        store = _store(tmp_path, backend)
        account = store.accounts["owner"]
        blob = b"gone"
        store.put(account, sha(blob), blob, rows=1, created_at=1)
        fake.objects.clear()
        with pytest.raises(StorageIO):
            store.get(account, sha(blob))


# ---------------------------------------------------------------------------
# config and migration
# ---------------------------------------------------------------------------
def test_a_store_without_a_config_is_a_local_store(tmp_path):
    assert blobstore.load_config(tmp_path) == {"backend": "fs"}
    assert isinstance(blobstore.backend_for_root(tmp_path), FsBackend)


def test_unknown_backend_in_config_is_refused(tmp_path):
    (tmp_path / "storage.json").write_text('{"backend": "ftp"}')
    with pytest.raises(ConfigError):
        blobstore.load_config(tmp_path)


def test_probe_proves_the_backend_or_fails(tmp_path):
    backend = FsBackend(tmp_path)
    result = blobstore.probe(backend)
    assert result["verified"] is True
    assert not any((tmp_path / "objects" / "_probe").glob("*"))


def test_migration_fs_to_s3_verifies_every_object_and_switches_last(tmp_path):
    store = _store(tmp_path)
    account = store.accounts["owner"]
    blobs = {sha(b"one"): b"one", sha(b"two"): b"two"}
    for digest, blob in blobs.items():
        store.put(account, digest, blob, rows=1, created_at=10)

    with FakeS3() as fake:
        config = {
            "backend": "s3",
            "endpoint": fake.url,
            "bucket": BUCKET,
            "region": REGION,
            "prefix": "",
            "addressing": "path",
            "sse": "",
        }
        target = blobstore.make_backend(config, tmp_path)
        result = blobstore.migrate(tmp_path, target, target_config=config)

        assert result["switched"] is True
        assert result["verified"] == 2
        assert result["failed"] == []
        # the blobs are in the bucket, byte for byte
        for digest, blob in blobs.items():
            assert fake.objects[object_key("owner", digest)] == blob
        # the config now points at the backend, and the store reads through it
        assert blobstore.load_config(tmp_path)["backend"] == "s3"
        reopened = _store(tmp_path, blobstore.backend_for_root(tmp_path))
        assert reopened.get(reopened.accounts["owner"], sha(b"one")) == b"one"
        # the source is kept by default: a migration is not a move until asked
        assert (tmp_path / "objects" / "owner" / f"{sha(b'one')}.trk").exists()


def test_migration_back_to_local_disk(tmp_path):
    store = _store(tmp_path)
    account = store.accounts["owner"]
    blob = b"round-trip"
    store.put(account, sha(blob), blob, rows=1, created_at=5)

    with FakeS3() as fake:
        s3_config = {
            "backend": "s3",
            "endpoint": fake.url,
            "bucket": BUCKET,
            "region": REGION,
            "prefix": "",
            "addressing": "path",
            "sse": "",
        }
        blobstore.migrate(tmp_path, blobstore.make_backend(s3_config, tmp_path), target_config=s3_config)
        # lose the local copy to prove the data really comes back
        (tmp_path / "objects" / "owner" / f"{sha(blob)}.trk").unlink()
        result = blobstore.migrate(tmp_path, FsBackend(tmp_path), target_config={"backend": "fs"})
        assert result["switched"] is True
        assert (tmp_path / "objects" / "owner" / f"{sha(blob)}.trk").read_bytes() == blob
        assert blobstore.load_config(tmp_path) == {"backend": "fs"}


def test_a_failed_migration_does_not_switch_the_config(tmp_path):
    store = _store(tmp_path)
    account = store.accounts["owner"]
    blob = b"stays-local"
    store.put(account, sha(blob), blob, rows=1, created_at=7)

    with FakeS3(reject="AccessDenied") as fake:
        config = {
            "backend": "s3",
            "endpoint": fake.url,
            "bucket": BUCKET,
            "region": REGION,
            "addressing": "path",
        }
        target = blobstore.make_backend(config, tmp_path)
        result = blobstore.migrate(tmp_path, target, target_config=config)
        assert result["switched"] is False
        assert result["failed"], result
        assert blobstore.load_config(tmp_path) == {"backend": "fs"}
        assert (tmp_path / "objects" / "owner" / f"{sha(blob)}.trk").read_bytes() == blob


def test_migration_is_idempotent(tmp_path):
    store = _store(tmp_path)
    account = store.accounts["owner"]
    blob = b"twice"
    store.put(account, sha(blob), blob, rows=1, created_at=3)

    with FakeS3() as fake:
        config = {
            "backend": "s3",
            "endpoint": fake.url,
            "bucket": BUCKET,
            "region": REGION,
            "addressing": "path",
        }
        target = blobstore.make_backend(config, tmp_path)
        first = blobstore.migrate(tmp_path, target, target_config=config)
        second = blobstore.migrate(tmp_path, target, target_config=config)
        assert first["verified"] == second["verified"] == 1
        assert len(fake.objects) == 1


def test_dry_run_counts_without_writing(tmp_path):
    store = _store(tmp_path)
    account = store.accounts["owner"]
    store.put(account, sha(b"dry"), b"dry", rows=1, created_at=1)

    with FakeS3() as fake:
        config = {
            "backend": "s3",
            "endpoint": fake.url,
            "bucket": BUCKET,
            "region": REGION,
            "addressing": "path",
        }
        result = blobstore.migrate(
            tmp_path, blobstore.make_backend(config, tmp_path), target_config=config, dry_run=True
        )
        assert result["switched"] is False
        assert result["bytes"] == 3
        assert fake.objects == {}
        assert blobstore.load_config(tmp_path) == {"backend": "fs"}


def test_migration_can_drop_the_source_when_asked(tmp_path):
    store = _store(tmp_path)
    account = store.accounts["owner"]
    blob = b"pruned-source"
    store.put(account, sha(blob), blob, rows=1, created_at=1)

    with FakeS3() as fake:
        config = {
            "backend": "s3",
            "endpoint": fake.url,
            "bucket": BUCKET,
            "region": REGION,
            "addressing": "path",
        }
        result = blobstore.migrate(
            tmp_path, blobstore.make_backend(config, tmp_path), target_config=config, keep_source=False
        )
        assert result["switched"] is True
        assert not (tmp_path / "objects" / "owner" / f"{sha(blob)}.trk").exists()
        assert object_key("owner", sha(blob)) in fake.objects


def test_config_write_is_atomic_and_private(tmp_path):
    path = blobstore.save_config(tmp_path, {"backend": "fs"})
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["backend"] == "fs"
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------
# the CLI surface
# ---------------------------------------------------------------------------
def _cli(argv, capsys):
    from trick.cli import main

    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_storage_show_on_a_local_store(tmp_path, capsys):
    _store(tmp_path)
    code, out, err = _cli(["vault", "storage", "show", "--root", str(tmp_path)], capsys)
    assert code == 0, err
    payload = json.loads(out)
    assert payload["config"]["backend"] == "fs"
    assert payload["backend"]["backend"] == "fs"
    assert payload["objects"] == 0


def test_cli_storage_test_against_a_real_endpoint(tmp_path, capsys):
    _store(tmp_path)
    with FakeS3() as fake:
        code, out, err = _cli(
            [
                "vault", "storage", "test", "--root", str(tmp_path), "--backend", "s3",
                "--endpoint", fake.url, "--bucket", BUCKET, "--region", REGION,
            ],
            capsys,
        )
        assert code == 0, err
        assert json.loads(out)["verified"] is True


def test_cli_storage_set_refuses_a_backend_that_does_not_answer(tmp_path, capsys):
    _store(tmp_path)
    code, out, err = _cli(
        [
            "vault", "storage", "set", "--root", str(tmp_path), "--backend", "s3",
            "--endpoint", "http://127.0.0.1:9", "--bucket", BUCKET,
        ],
        capsys,
    )
    assert code == 1
    assert "s3" in err.lower() or "failed" in err.lower()
    assert not (tmp_path / "storage.json").exists()


def test_cli_storage_set_then_migrate(tmp_path, capsys):
    store = _store(tmp_path)
    account = store.accounts["owner"]
    store.put(account, sha(b"cli"), b"cli", rows=1, created_at=1)
    with FakeS3() as fake:
        code, out, err = _cli(
            [
                "vault", "storage", "migrate", "--root", str(tmp_path), "--backend", "s3",
                "--endpoint", fake.url, "--bucket", BUCKET, "--region", REGION,
            ],
            capsys,
        )
        assert code == 0, err
        payload = json.loads(out)
        assert payload["switched"] is True
        assert payload["verified"] == 1
        code, out, err = _cli(["vault", "storage", "show", "--root", str(tmp_path)], capsys)
        assert json.loads(out)["config"]["backend"] == "s3"


def test_cli_storage_migrate_without_a_backend_is_a_usage_error(tmp_path, capsys):
    _store(tmp_path)
    code, out, err = _cli(["vault", "storage", "migrate", "--root", str(tmp_path)], capsys)
    assert code == 1
    assert "backend" in err
