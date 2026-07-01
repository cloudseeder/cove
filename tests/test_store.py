"""Entry store contract. Spec §9.

The store is the SOURCE OF TRUTH (the overview index, translog, and ledger are
all derived from it). The contract these tests pin:

  - append is append-only: a given (thread, seq) and a given id may each be
    written exactly once.
  - per-thread seq is monotonic and independent across threads.
  - since(thread, seq) gives the delta-sync window: entries strictly after seq,
    in seq order. /sync depends on this (§7).
  - entries roundtrip with full fidelity (parents, blobs, supersedes, sig).
  - state survives reopen: the store is on disk, not in process memory.
"""
from __future__ import annotations

import pytest

from cove import crypto
from cove.entry import BlobRef, Entry, sign_entry, verify_entry
from cove.store import EventStore


@pytest.fixture
def store(tmp_path):
    return EventStore(str(tmp_path / "hub.db"))


def _post(author_pub: str, *, thread: str = "t1", body: str = "hi",
          parents=None, blobs=None, supersedes=None) -> Entry:
    return Entry(
        thread=thread, author=author_pub, kind="post",
        created_at="2026-01-01T00:00:00Z", body=body,
        parents=parents or [], blobs=blobs or [], supersedes=supersedes,
    )


def _signed(author_priv: str, author_pub: str, **kwargs) -> Entry:
    return sign_entry(_post(author_pub, **kwargs), author_priv)


# ---- next_seq / append --------------------------------------------------

def test_next_seq_starts_at_zero(store):
    assert store.next_seq("anything") == 0


def test_next_seq_monotonic_within_thread(store):
    assert [store.next_seq("t1") for _ in range(4)] == [0, 1, 2, 3]


def test_next_seq_independent_across_threads(store):
    assert store.next_seq("tA") == 0
    assert store.next_seq("tB") == 0
    assert store.next_seq("tA") == 1
    assert store.next_seq("tB") == 1


# ---- exists / get -------------------------------------------------------

def test_exists_false_before_append(store, keypair):
    priv, pub = keypair
    ev = _signed(priv, pub)
    assert store.exists(ev.id) is False


def test_append_then_exists_then_get(store, keypair):
    priv, pub = keypair
    ev = _signed(priv, pub)
    seq = store.next_seq(ev.thread)
    store.append(ev, seq)

    assert store.exists(ev.id) is True
    got = store.get(ev.id)
    assert got is not None
    # Full fidelity: id, sig, every field intact.
    assert got.id == ev.id
    assert got.sig == ev.sig
    assert got.thread == ev.thread
    assert got.author == ev.author
    assert got.kind == ev.kind
    assert got.created_at == ev.created_at
    assert got.body == ev.body
    assert got.parents == ev.parents
    assert got.blobs == ev.blobs
    assert got.supersedes == ev.supersedes
    # Roundtripped entry must still verify (id+sig preserved over canonical content).
    assert verify_entry(got) is True


def test_get_returns_none_for_unknown_id(store):
    assert store.get("sha256:" + "ff" * 32) is None


def test_roundtrip_preserves_blobs_parents_supersedes(store, keypair):
    priv, pub = keypair
    ev = _signed(priv, pub,
                 parents=["sha256:" + "11" * 32, "sha256:" + "22" * 32],
                 blobs=[BlobRef(hash="sha256:" + "aa" * 32, media_type="image/png",
                                size=4096, name="x.png")],
                 supersedes="sha256:" + "33" * 32,
                 body="with blobs and parents")
    store.append(ev, store.next_seq(ev.thread))
    got = store.get(ev.id)
    assert got.parents == ev.parents
    assert got.blobs == ev.blobs
    assert got.supersedes == ev.supersedes
    assert verify_entry(got) is True


# ---- append-only enforcement -------------------------------------------

def test_duplicate_id_rejected(store, keypair):
    priv, pub = keypair
    ev = _signed(priv, pub)
    store.append(ev, store.next_seq(ev.thread))
    with pytest.raises(Exception):
        store.append(ev, store.next_seq(ev.thread))


def test_duplicate_thread_seq_rejected(store, keypair):
    """Two DIFFERENT entries cannot share a (thread, seq) — append-only invariant."""
    priv, pub = keypair
    a = _signed(priv, pub, body="a")
    b = _signed(priv, pub, body="b")
    assert a.id != b.id
    store.append(a, 0)
    with pytest.raises(Exception):
        store.append(b, 0)


# ---- since (delta-sync) -------------------------------------------------

def test_since_returns_entries_strictly_after_seq(store, keypair):
    priv, pub = keypair
    evs = []
    for i in range(5):
        e = _signed(priv, pub, body=f"m{i}")
        s = store.next_seq(e.thread)
        store.append(e, s)
        evs.append((e, s))

    # since(t1, seq=1) should give seqs 2, 3, 4 — strictly after, in order.
    got = list(store.since("t1", 1))
    assert [e.id for e in got] == [evs[2][0].id, evs[3][0].id, evs[4][0].id]


def test_since_excludes_other_threads(store, keypair):
    priv, pub = keypair
    a = _signed(priv, pub, thread="tA", body="a")
    b = _signed(priv, pub, thread="tB", body="b")
    store.append(a, 0)
    store.append(b, 0)
    assert [e.id for e in store.since("tA", -1)] == [a.id]
    assert [e.id for e in store.since("tB", -1)] == [b.id]


def test_since_unknown_thread_is_empty(store):
    assert list(store.since("never-seen", -1)) == []


def test_since_at_high_water_is_empty(store, keypair):
    priv, pub = keypair
    ev = _signed(priv, pub)
    store.append(ev, 0)
    assert list(store.since("t1", 0)) == []


# ---- ephemeral registry (v0.4.37) --------------------------------------

def _mk_auth(thread: str, ttl: int = 30 * 24 * 3600) -> tuple[bytes, str]:
    """A minimal delete-authorization pair for tests. The pipeline verifies
    the sig against the creator's pubkey; the store just persists bytes."""
    return (b'{"thread":"' + thread.encode() + b'","ttl":' + str(ttl).encode() + b'}',
            "aa" * 32)


def test_is_ephemeral_false_before_open(store):
    assert store.is_ephemeral("beach") is False
    assert store.get_ephemeral("beach") is None


def test_open_ephemeral_registers_the_thread(store):
    content, sig = _mk_auth("beach")
    store.open_ephemeral(
        thread="beach", creator_pubkey="alice_pub",
        created_at="2026-07-01T00:00:00Z", ttl_seconds=30 * 86400,
        delete_auth_content=content, delete_auth_sig=sig,
    )
    assert store.is_ephemeral("beach") is True
    rec = store.get_ephemeral("beach")
    assert rec["creator_pubkey"] == "alice_pub"
    assert rec["ttl_seconds"] == 30 * 86400
    assert rec["tombstoned_at"] is None


def test_open_ephemeral_rejects_re_open(store):
    content, sig = _mk_auth("beach")
    store.open_ephemeral(
        thread="beach", creator_pubkey="a", created_at="2026-07-01T00:00:00Z",
        ttl_seconds=86400, delete_auth_content=content, delete_auth_sig=sig,
    )
    with pytest.raises(ValueError, match="already exists"):
        store.open_ephemeral(
            thread="beach", creator_pubkey="a",
            created_at="2026-07-01T00:00:00Z", ttl_seconds=86400,
            delete_auth_content=content, delete_auth_sig=sig,
        )


def test_open_ephemeral_rejects_existing_permanent_thread(store, keypair):
    """A thread name that already has permanent entries can't be re-typed
    as ephemeral — otherwise a hub could quietly move an accountable log
    into the deletable tier."""
    priv, pub = keypair
    ev = _signed(priv, pub, thread="permanent-t", body="written already")
    store.append(ev, store.next_seq(ev.thread))
    content, sig = _mk_auth("permanent-t")
    with pytest.raises(ValueError, match="permanent"):
        store.open_ephemeral(
            thread="permanent-t", creator_pubkey="a",
            created_at="2026-07-01T00:00:00Z", ttl_seconds=86400,
            delete_auth_content=content, delete_auth_sig=sig,
        )


def test_all_ephemeral_lists_every_registered_thread(store):
    for name in ("beach", "lake", "trail"):
        content, sig = _mk_auth(name)
        store.open_ephemeral(
            thread=name, creator_pubkey="a",
            created_at="2026-07-01T00:00:00Z", ttl_seconds=86400,
            delete_auth_content=content, delete_auth_sig=sig,
        )
    listed = {r["thread"] for r in store.all_ephemeral()}
    assert listed == {"beach", "lake", "trail"}


def test_iter_global_excludes_ephemeral_thread_entries(store, keypair):
    """Main translog rebuild must not slurp ephemeral leaves. That would
    put ephemeral entries in the main tree and break the cross-tree
    binding EphemeralSTH defends against."""
    priv, pub = keypair
    perm = _signed(priv, pub, thread="perm", body="in permanent thread")
    store.append(perm, store.next_seq(perm.thread))

    content, sig = _mk_auth("beach")
    store.open_ephemeral(
        thread="beach", creator_pubkey="a",
        created_at="2026-07-01T00:00:00Z", ttl_seconds=86400,
        delete_auth_content=content, delete_auth_sig=sig,
    )
    eph = _signed(priv, pub, thread="beach", body="in ephemeral thread")
    store.append(eph, store.next_seq(eph.thread))

    globals_ = list(store.iter_global())
    assert (perm.id, 0) in globals_
    assert (eph.id, 0) not in globals_


def test_iter_ephemeral_entries_returns_only_that_threads_entries(store, keypair):
    priv, pub = keypair
    for name in ("beach", "lake"):
        content, sig = _mk_auth(name)
        store.open_ephemeral(
            thread=name, creator_pubkey="a",
            created_at="2026-07-01T00:00:00Z", ttl_seconds=86400,
            delete_auth_content=content, delete_auth_sig=sig,
        )
    beach1 = _signed(priv, pub, thread="beach", body="b1")
    beach2 = _signed(priv, pub, thread="beach", body="b2")
    lake1  = _signed(priv, pub, thread="lake",  body="l1")
    for ev in (beach1, beach2, lake1):
        store.append(ev, store.next_seq(ev.thread))

    got = store.iter_ephemeral_entries("beach")
    assert got == [(beach1.id, 0), (beach2.id, 1)]
    assert store.iter_ephemeral_entries("lake") == [(lake1.id, 0)]


# ---- persistence --------------------------------------------------------

def test_state_survives_reopen(tmp_path, keypair):
    path = str(tmp_path / "hub.db")
    priv, pub = keypair
    a = _signed(priv, pub, body="a")
    b = _signed(priv, pub, body="b")

    s1 = EventStore(path)
    s1.append(a, s1.next_seq(a.thread))
    s1.append(b, s1.next_seq(b.thread))
    s1.close()

    s2 = EventStore(path)
    assert s2.exists(a.id) and s2.exists(b.id)
    # next_seq picks up where the first store left off — monotonic across reopens.
    assert s2.next_seq("t1") == 2
    # delta-sync still walks the on-disk entries.
    assert [e.id for e in s2.since("t1", -1)] == [a.id, b.id]
