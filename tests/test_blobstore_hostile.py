"""What the S3 backend does when the server is not polite.

The happy path was tested against a server that verifies signatures. That says
nothing about a server that answers with a redirect to another host, streams
gigabytes, returns a 500 whose body is binary garbage, or carries a newline in
its error message. Each of those is a real failure mode of a self-hosted
minio/R2/B2 endpoint and of a misconfigured proxy in front of one, and each one
is cheap to get wrong in a way that never shows up in normal use.

Four rules are pinned here:

1. a body larger than ``max_bytes`` is refused instead of buffered;
2. a redirect is never followed -- an S3 client that follows a 302 will hand a
   signed request to whatever host the answer names;
3. error text is flattened to one line and capped, so a server cannot forge log
   lines or fill a terminal;
4. the secret never appears in anything a user can see: not in ``repr``, not in
   ``public()``, not in an exception raised by a rejected request.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from s3fake import ACCESS_KEY, BUCKET, REGION, SECRET_KEY, FakeS3  # noqa: E402

from trick.blobstore import S3Backend, S3Settings, object_key  # noqa: E402
from trick.errors import ConfigError, StorageIO  # noqa: E402

KEY = object_key("acct1", "a" * 64)


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch):
    monkeypatch.setenv("TRICK_S3_ACCESS_KEY", ACCESS_KEY)
    monkeypatch.setenv("TRICK_S3_SECRET_KEY", SECRET_KEY)


def settings(url: str, **overrides) -> S3Settings:
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
# a body that never ends
# ---------------------------------------------------------------------------
def test_an_oversized_body_is_refused_not_buffered():
    """8 MiB announced against a 1 MiB ceiling: the read stops at the ceiling."""
    with FakeS3(oversize=8 * 1024 * 1024) as fake:
        backend = S3Backend(settings(fake.url, max_bytes=1024 * 1024))
        with pytest.raises(StorageIO) as caught:
            backend.get(KEY)
    assert "max_bytes" in str(caught.value)
    # The point is that it failed at the ceiling rather than after 8 MiB.
    assert "exceeded" in str(caught.value)


def test_a_body_at_the_ceiling_is_still_accepted():
    """The cap must not reject the archive it was sized for."""
    with FakeS3() as fake:
        backend = S3Backend(settings(fake.url, max_bytes=4096))
        backend.put(KEY, b"x" * 4096)
        assert backend.get(KEY) == b"x" * 4096


# ---------------------------------------------------------------------------
# a redirect
# ---------------------------------------------------------------------------
def test_a_redirect_is_an_error_not_a_detour():
    """A 302 is surfaced, and the named host is never contacted."""
    with FakeS3() as elsewhere, FakeS3(redirect=elsewhere.url + "/elsewhere") as fake:
        backend = S3Backend(settings(fake.url))
        with pytest.raises(StorageIO) as caught:
            backend.get(KEY)
        # The detour would have landed here; it must stay untouched.
        assert elsewhere.requests == []
    assert "302" in str(caught.value)


# ---------------------------------------------------------------------------
# an error body that is not what it claims
# ---------------------------------------------------------------------------
def test_a_binary_error_body_does_not_crash_the_parser():
    garbage = (500, b"\x00\x01\x02 not xml at all \xff\xfe")
    with FakeS3(garbage=garbage) as fake:
        backend = S3Backend(settings(fake.url, retries=1))
        with pytest.raises(StorageIO) as caught:
            backend.put(KEY, b"data")
    text = str(caught.value)
    assert "http 500" in text
    assert "\x00" not in text


def test_an_error_message_cannot_forge_a_log_line():
    """A newline inside <Message> would look like a second log entry."""
    body = (
        b"<?xml version='1.0'?><Error><Code>AccessDenied</Code>"
        b"<Message>denied\nERROR 2026-01-01 admin login succeeded\r\n</Message></Error>"
    )
    with FakeS3(garbage=(403, body)) as fake:
        backend = S3Backend(settings(fake.url, retries=1))
        with pytest.raises(StorageIO) as caught:
            backend.put(KEY, b"data")
    text = str(caught.value)
    assert "\n" not in text and "\r" not in text
    assert text.count("ERROR") == 1  # still the server's word, on one line
    assert "AccessDenied" in text


def test_a_very_long_error_message_is_capped():
    body = b"<Error><Code>SlowDown</Code><Message>" + b"A" * 50_000 + b"</Message></Error>"
    with FakeS3(garbage=(503, body)) as fake:
        backend = S3Backend(settings(fake.url, retries=1))
        with pytest.raises(StorageIO) as caught:
            backend.put(KEY, b"data")
    assert len(str(caught.value)) < 500


# ---------------------------------------------------------------------------
# the secret
# ---------------------------------------------------------------------------
def test_the_secret_is_absent_from_repr_and_public_view():
    configured = settings("https://s3.example.com")
    assert SECRET_KEY not in repr(configured)
    assert SECRET_KEY not in str(configured.public())
    assert configured.public()["credentials"] == "present"


def test_a_rejected_request_does_not_echo_the_secret():
    with FakeS3(reject="InvalidAccessKeyId") as fake:
        backend = S3Backend(settings(fake.url))
        with pytest.raises(StorageIO) as caught:
            backend.put(KEY, b"data")
    assert SECRET_KEY not in str(caught.value)
    assert ACCESS_KEY not in str(caught.value)


# ---------------------------------------------------------------------------
# configuration that would escape the bucket
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bucket", ["../other", "a/b", "trick bucket", ".."])
def test_a_bucket_name_cannot_carry_a_path(bucket):
    with pytest.raises(ConfigError):
        S3Backend(settings("https://s3.example.com", bucket=bucket))


@pytest.mark.parametrize("prefix", ["a/../b", "./a", "a//b", "a/\x01b"])
def test_a_prefix_must_be_plain_segments(prefix):
    with pytest.raises(ConfigError):
        S3Backend(settings("https://s3.example.com", prefix=prefix))


def test_a_non_positive_ceiling_is_refused():
    with pytest.raises(ConfigError):
        S3Backend(settings("https://s3.example.com", max_bytes=0))


def test_plain_http_is_still_only_for_loopback():
    with pytest.raises(ConfigError):
        S3Backend(settings("http://minio.example.com"))
    S3Backend(settings("http://127.0.0.1:9000"))
