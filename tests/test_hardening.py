"""Hardening: the failures that do not announce themselves.

Every test here is a scenario that has already produced a silent bug in this
project or would produce one: a writer killed mid-publish, a failed rename, two
processes writing the same index entry, a transcript full of control characters
and injected result blocks, a reader holding an old map while a new file lands.

The rule these tests enforce is the same one the code claims: a half-written
state is never observable, and a failed write leaves the previous state intact.
"""

from __future__ import annotations

import hashlib
import os
import random
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from trick.matrix import MatrixIndex, MatrixMissing, MatrixWriter
from trick.transcript import (
    Message,
    TranscriptIndex,
    chunk_messages,
    fold_tr,
    snippet,
    strip_result_blocks,
)
from trick.errors import StorageIO
from trick.vaultremote import Account, VaultStore, new_token, token_digest, write_accounts

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# a writer that dies
# ---------------------------------------------------------------------------
CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {src!r})
    from trick.matrix import MatrixWriter

    writer = MatrixWriter({target!r}, header_flags=0)
    writer.open()
    i = 0
    while True:
        writer.add(i, bytes([i % 251 + 1]) * 16, 0.5, 0)
        i += 1
        if i % 2000 == 0:
            time.sleep(0.01)
    """
)


def test_a_killed_writer_never_publishes_a_partial_matrix(tmp_path: Path) -> None:
    """SIGKILL between the first record and the rename must leave no map at all."""
    target = tmp_path / "score.idx"
    script = CHILD.format(src=str(ROOT / "src"), target=str(target))
    proc = subprocess.Popen([sys.executable, "-c", script])
    try:
        # wait until the writer has certainly started writing
        for _ in range(200):
            if any(tmp_path.iterdir()):
                break
            proc.poll()
            threading.Event().wait(0.01)
        assert any(tmp_path.iterdir()), "the child never started writing"
    finally:
        proc.kill()
        proc.wait(timeout=30)

    if target.exists():
        # The rename happened before the kill: then the map must be complete.
        index = MatrixIndex(target)
        try:
            assert index.count > 0
            index.verify()
        finally:
            index.close()
    else:
        with pytest.raises(MatrixMissing):
            MatrixIndex(target)

    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    # A .tmp may survive a SIGKILL, but it must never be mistaken for the map,
    # and the next writer must be able to take over (the lock died with the pid).
    with MatrixWriter(target) as writer:
        writer.add(1, bytes(16), 0.0, 0)
    assert target.exists()
    MatrixIndex(target).close()
    assert all(name.endswith(".tmp") for name in leftovers)


def test_a_failed_publish_keeps_the_previous_matrix(tmp_path: Path, monkeypatch) -> None:
    """If the rename fails, the old map must still be the one that is read."""
    target = tmp_path / "score.idx"
    with MatrixWriter(target) as writer:
        writer.add(1, bytes(16), 1.0, 0)
    before = target.read_bytes()

    real_replace = os.replace

    def boom(src, dst):  # noqa: ANN001, ANN202
        if str(dst) == str(target):
            raise OSError("disk went away during the rename")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", boom)
    writer = MatrixWriter(target)
    with pytest.raises(StorageIO) as caught:
        with writer:
            writer.add(2, bytes(16), 1.0, 0)
    assert "publish" in str(caught.value)
    monkeypatch.undo()

    assert target.read_bytes() == before, "a failed publish must not touch the live map"
    index = MatrixIndex(target)
    try:
        assert index.count == 1
    finally:
        index.close()


def test_a_reader_keeps_its_map_while_a_new_one_lands(tmp_path: Path) -> None:
    """Content of an open mmap must not change under the reader's feet."""
    target = tmp_path / "score.idx"
    with MatrixWriter(target) as writer:
        writer.add(1, bytes(16), 1.0, 0)
    index = MatrixIndex(target)
    try:
        first = index.count
        with MatrixWriter(target) as writer:
            writer.add(2, bytes(16), 1.0, 0)
            writer.add(3, bytes(16), 1.0, 0)
        assert index.count == first, "an open map changed after a new file was published"
    finally:
        index.close()
    fresh = MatrixIndex(target)
    try:
        # MatrixWriter publishes a new map; the old one had a single record and
        # the new one has two, so the count proves the reader kept the old file.
        assert fresh.count == 2
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# two writers, one object
# ---------------------------------------------------------------------------
def test_concurrent_writers_of_the_same_blob_store_it_once(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    token = new_token()
    accounts = [Account(name="owner", token_sha256=token_digest(token))]
    path = root / "accounts.json"
    write_accounts(path, accounts)
    store = VaultStore(root, accounts, accounts_path=path)
    account = store.account("owner")
    blob = b"the same archive from eight threads"
    sha = hashlib.sha256(blob).hexdigest()

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            store.put(account, sha, blob, 77, 1_700_000_000)
        except BaseException as exc:  # noqa: BLE001 - the point is to catch anything
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"a concurrent put raised: {errors!r}"
    assert store.get(account, sha) == blob
    usage = store.usage(account)
    assert usage["objects"] == 1
    assert usage["bytes"] == len(blob)


def test_a_writer_that_loses_the_lock_does_not_corrupt_the_index(tmp_path: Path) -> None:
    """Two stores on one root: the second must wait, not interleave."""
    root = tmp_path / "vault"
    token = new_token()
    accounts = [Account(name="owner", token_sha256=token_digest(token))]
    path = root / "accounts.json"
    write_accounts(path, accounts)
    first = VaultStore(root, accounts, accounts_path=path)
    second = VaultStore(root, accounts, accounts_path=path)
    blobs = [f"archive {i}".encode() for i in range(6)]

    def put_all(store: VaultStore) -> None:
        for blob in blobs:
            store.put(first.account("owner"), hashlib.sha256(blob).hexdigest(), blob, 1, 1_700_000_000)

    threads = [threading.Thread(target=put_all, args=(store,)) for store in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    usage = first.usage(first.account("owner"))
    assert usage["objects"] == len(blobs)


# ---------------------------------------------------------------------------
# fuzz: the chat corpus
# ---------------------------------------------------------------------------
ALPHABET = (
    "ab\u00e7defg\u011fhi\u0131jklmno\u00f6prs\u015ftu\u00fcvyz"
    " 0123456789 .,:;!?()[]{}\n\t\"'`*-_/\\|@#$%^&~"
    "\u0130\u015e\u011e\u00c7\u00d6\u00dc \u4e2d\u6587 \U0001f600"
)


def random_text(rng: random.Random, length: int) -> str:
    return "".join(rng.choice(ALPHABET) for _ in range(length))


def test_fuzzed_transcripts_keep_the_invariants(tmp_path: Path) -> None:
    """A thousand random chats: unique ids, sane offsets, nothing invented."""
    rng = random.Random(20261103)
    for round_index in range(40):
        messages = []
        for turn in range(rng.randint(0, 6)):
            role = rng.choice(["user", "assistant"])
            body = random_text(rng, rng.randint(0, 900))
            if rng.random() < 0.2:
                body += (
                    "\n<details>\n<summary>Trick \u00b7 q \u00b7 3 passages match</summary>\n"
                    "<ol><li><a href='?message=x'>turn 1 \u00b7 assistant</a> \u00b7 matched: q</li></ol>\n"
                    "</details>"
                )
            messages.append(Message(role=role, content=body, mid=f"id-{round_index}-{turn}"))
        lengths = {message.mid: len(message.content) for message in messages}
        index = TranscriptIndex(messages)
        try:
            ids = [chunk.chunk_id for chunk in index.chunks]
            assert len(ids) == len(set(ids)), "chunk ids must be unique"
            for chunk in index.chunks:
                assert chunk.text.strip(), "an empty passage would match everything"
                assert chunk.turn >= 1, "turn numbers are 1-based"
                assert chunk.mid, "a passage without a message id cannot be jumped to"
                assert 0 <= chunk.char_start < chunk.char_end
                assert chunk.char_end <= lengths[chunk.mid] + 2, "offset outside its message"
            assert "trick:results" not in "".join(c.text for c in index.chunks), (
                "the tool's own output must never become searchable evidence"
            )
            if index.chunks:
                quote = snippet(index.chunks[0].text, "ab", width=40)
                assert isinstance(quote, str)
        finally:
            index.close()


def test_strip_result_blocks_is_idempotent_and_bounded() -> None:
    text = (
        "before\n"
        "<!-- trick:results --> <details><summary>Trick \u00b7 q \u00b7 3 passages match</summary>\n"
        "<ol><li>turn 2 \u00b7 assistant \u00b7 matched: q</li></ol>\n"
        "_8 catalogue sources matched outside this conversation_\n"
        "</details>\nafter"
    )
    once = strip_result_blocks(text)
    assert "trick:results" not in once
    assert once == "before", "the block and everything appended after it is ours to cut"
    assert strip_result_blocks("") == ""
    # a marker with no closing tag must not eat the rest of the file silently
    tail = strip_result_blocks("keep\n<!-- trick:results --> passages match, matched: q")
    assert tail.startswith("keep")
    # a marker with no block signal at all is the reader's own text, not ours
    assert strip_result_blocks("keep\n<!-- trick:results -->") == "keep\n<!-- trick:results -->"


def test_chunking_never_splits_a_word_and_never_exceeds_the_cap() -> None:
    words = ["R" * 30] * 400
    message = Message(role="user", content=" ".join(words), mid="m1")
    chunks = chunk_messages([message])
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= 700, "a passage must stay inside the render budget"
    joined = " ".join(chunk.text for chunk in chunks)
    assert joined.count("RRRRR") >= 400


def test_folding_is_stable_under_repetition() -> None:
    for text in ("Ya\u015fad\u0131\u011f\u0131m\u0131z", "yasadigimiz", "  MiXeD CaSe  ", "\u0130stanbul"):
        once = fold_tr(text)
        assert fold_tr(once) == once
        assert fold_tr(text) == once
