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
