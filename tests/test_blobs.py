"""Blob store contract. Spec §4.

  - Content-addressed by sha256; the address is `'sha256:' + hex`.
  - Dedup is automatic: identical bytes always map to the same address,
    re-put is a no-op (and quota accounting at higher layers can skip
    charging because the bytes were already paid for).
  - Bytes survive process restart (filesystem-backed).
  - Integrity: a client re-hashing on download must get back the address.
  - Malformed addresses are rejected before they touch the filesystem
    (so 'sha256:../etc/passwd' can never escape the blob root).
"""
from __future__ import annotations

import hashlib

import pytest

from cove.blobs import BlobStore


@pytest.fixture
def store(tmp_path):
    return BlobStore(str(tmp_path / "blobs"))


def _h(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


# ---- put / get round trip --------------------------------------------

def test_put_returns_content_address(store):
    content = b"hello world"
    assert store.put(content) == _h(content)


def test_get_returns_stored_bytes_byte_identical(store):
    content = b"some\x00binary\xff\x01stuff"
    addr = store.put(content)
    assert store.get(addr) == content


def test_get_unknown_returns_none(store):
    assert store.get(_h(b"never put")) is None


def test_has_tracks_presence(store):
    content = b"x"
    addr = _h(content)
    assert store.has(addr) is False
    store.put(content)
    assert store.has(addr) is True


# ---- dedup -----------------------------------------------------------

def test_put_is_idempotent_for_same_bytes(store):
    """Two puts of the same content yield the same address and don't
    duplicate the file — quota accounting at higher layers depends on
    this being a no-op."""
    content = b"shared"
    a1 = store.put(content)
    a2 = store.put(content)
    assert a1 == a2


def test_put_different_bytes_different_address(store):
    assert store.put(b"a") != store.put(b"b")


# ---- persistence -----------------------------------------------------

def test_state_survives_reopen(tmp_path):
    content = b"persisted"
    s1 = BlobStore(str(tmp_path / "blobs"))
    addr = s1.put(content)
    s2 = BlobStore(str(tmp_path / "blobs"))
    assert s2.get(addr) == content


# ---- integrity & address validation ----------------------------------

def test_client_can_reverify_content_address(store):
    """The point of content addressing: any consumer re-hashes the bytes
    and confirms the address. This is what makes 'the hub cannot
    substitute blob content undetected' (§4) actually true."""
    content = b"some-document.pdf bytes here"
    addr = store.put(content)
    got = store.get(addr)
    assert "sha256:" + hashlib.sha256(got).hexdigest() == addr


# ---- metadata + references (data layer for future tiering/GC) -------

def test_put_records_metadata_row_with_size_and_first_seen(store):
    """A future cold-tier pass moves bytes off-disk but keeps this row —
    the hash + size are permanent proof-of-existence regardless of where
    the bytes themselves live."""
    content = b"meta-target"
    addr = store.put(content)
    meta = store.metadata(addr)
    assert meta is not None
    assert meta["hash"] == addr
    assert meta["size"] == len(content)
    assert meta["first_seen_at"] > 0


def test_metadata_returns_none_for_unknown(store):
    assert store.metadata("sha256:" + "ff" * 32) is None


def test_record_references_tracks_referencing_entries(store):
    """The recording layer a future refcount-driven GC will read from.
    Two entries referencing the same blob -> ref_count == 2; an entry
    referencing two blobs -> each blob's references_for has that entry."""
    a = store.put(b"shared bytes")
    b = store.put(b"other bytes")

    # entry e1 references both blobs; entry e2 references only `a`.
    store.record_references("sha256:e1", [
        {"hash": a, "media_type": "image/png", "size": 11, "name": "a.png"},
        {"hash": b, "media_type": "image/png", "size": 11, "name": "b.png"},
    ])
    store.record_references("sha256:e2", [
        {"hash": a, "media_type": "application/octet-stream",
         "size": 11, "name": "a-renamed"},
    ])

    assert sorted(store.references_for(a)) == ["sha256:e1", "sha256:e2"]
    assert store.references_for(b) == ["sha256:e1"]
    assert store.ref_count(a) == 2
    assert store.ref_count(b) == 1
    assert store.ref_count("sha256:" + "ff" * 32) == 0


def test_record_references_is_idempotent_on_replay(store):
    """A pipeline retry that re-records the same (blob, entry) pair must
    not double-count — otherwise a future refcount-based GC would
    incorrectly believe the blob has more references than it does."""
    a = store.put(b"x")
    store.record_references("sha256:e", [{"hash": a, "media_type": "x/y",
                                          "size": 1, "name": "n"}])
    store.record_references("sha256:e", [{"hash": a, "media_type": "x/y",
                                          "size": 1, "name": "n"}])
    assert store.ref_count(a) == 1


@pytest.mark.parametrize("bad", [
    "",
    "deadbeef",                                    # missing prefix
    "sha256:short",                                # wrong length
    "sha256:" + "z" * 64,                          # non-hex char
    "sha256:../etc/passwd",                        # path traversal attempt
    "md5:" + "0" * 32,                             # wrong algorithm
])
def test_malformed_blob_id_returns_none_not_filesystem_error(store, bad):
    assert store.get(bad) is None
    assert store.has(bad) is False
