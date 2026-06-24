"""Pipeline ↔ translog wiring. Spec §7.1 step 8.

These tests pin the *wiring* between the acceptance pipeline and the
tamper-evident log. Validation of the other steps (directory resolve, throttle,
ACL, store) is covered when those modules land; here we stub them so the focus
is on: accepted entry → translog leaf → STH covers it → inclusion proof verifies.

Per spec §6.4.1, the leaf commits to (entry_id, assigned per-thread seq); the
leaf POSITION in the Merkle tree is the global acceptance order. The pipeline
is responsible for keeping those two things consistent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from cove import crypto
from cove.entry import Entry, sign_entry
from cove.pipeline import AcceptanceError, Pipeline
from cove.translog import TamperEvidentLog, verify_inclusion


# ---- minimal stubs (real impls land in later slices) ----------------------

@dataclass
class _Att:
    role: str = "member"


class StubDirectory:
    def __init__(self, attested: dict[str, _Att] | None = None,
                 revoked: set[str] | None = None) -> None:
        self._attested = attested or {}
        self._revoked = revoked or set()

    def resolve(self, pubkey: str):
        return self._attested.get(pubkey)

    def is_revoked(self, pubkey: str, as_of: Optional[str] = None) -> bool:
        return pubkey in self._revoked


class StubStore:
    def __init__(self) -> None:
        self._by_id: dict[str, Entry] = {}
        self._next_seq: dict[str, int] = {}
        self.appended: list[tuple[str, int]] = []   # for assertions

    def next_seq(self, thread: str) -> int:
        s = self._next_seq.get(thread, 0)
        self._next_seq[thread] = s + 1
        return s

    def append(self, ev: Entry, seq: int) -> None:
        self._by_id[ev.id] = ev
        self.appended.append((ev.id, seq))

    def append_atomic(self, ev: Entry) -> int:
        seq = self._next_seq.get(ev.thread, 0)
        self.append(ev, seq)
        self._next_seq[ev.thread] = seq + 1
        return seq

    def exists(self, entry_id: str) -> bool:
        return entry_id in self._by_id


class StubThrottler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def check_and_consume(self, author: str, role: str, entry_bytes: int,
                          new_blob_bytes: int = 0) -> None:
        self.calls.append((author, role, entry_bytes))


class StubOverview:
    def __init__(self) -> None:
        self.added: list[tuple[str, str, list[str], int]] = []

    def add(self, thread: str, entry_id: str, parents: list[str], seq: int) -> None:
        self.added.append((thread, entry_id, list(parents), seq))


class StubLedger:
    def __init__(self) -> None:
        self.receipts: list[tuple[str, str, int, tuple[int, str]]] = []

    def apply_receipt(self, recipient: str, thread: str, high_water_seq: int,
                      observed_sth: tuple[int, str]) -> None:
        self.receipts.append((recipient, thread, high_water_seq, observed_sth))


@pytest.fixture
def no_structural(monkeypatch):
    """check_structural is unimplemented; make it a no-op for these tests."""
    monkeypatch.setattr("cove.pipeline.check_structural", lambda ev, cfg=None: None)


@pytest.fixture
def pipeline(no_structural, hub_keypair, keypair):
    priv, pub = hub_keypair
    apriv, apub = keypair
    directory = StubDirectory(attested={apub: _Att(role="member")})
    return Pipeline(
        store=StubStore(),
        directory=directory,
        translog=TamperEvidentLog(priv, pub),
        overview=StubOverview(),
        ledger=StubLedger(),
        throttler=StubThrottler(),
    ), apriv, apub


def _post(thread: str, author_pub: str, body: str = "hi") -> Entry:
    return Entry(thread=thread, author=author_pub, kind="post",
                 created_at="2026-01-01T00:00:00Z", body=body)


# ---- the tests ------------------------------------------------------------

def test_accepted_entry_extends_translog(pipeline):
    pl, apriv, apub = pipeline
    ev = sign_entry(_post("t1", apub), apriv)

    seq = pl.accept(ev)

    assert seq == 0
    sth = pl.translog.current_sth()
    assert sth.tree_size == 1
    # the entry must be in the log at log-position 0 with the assigned seq.
    proof = pl.translog.inclusion_proof(ev.id)
    assert proof.leaf_index == 0
    assert verify_inclusion(ev.id, seq, proof, sth) is True


def test_translog_position_is_global_acceptance_order(pipeline):
    """Two threads → two different per-thread seqs (both 0), but distinct log positions."""
    pl, apriv, apub = pipeline
    a = sign_entry(_post("tA", apub, "a"), apriv)
    b = sign_entry(_post("tB", apub, "b"), apriv)

    seq_a = pl.accept(a)
    seq_b = pl.accept(b)

    assert seq_a == 0 and seq_b == 0   # per-thread seq resets per thread
    sth = pl.translog.current_sth()
    assert sth.tree_size == 2

    proof_a = pl.translog.inclusion_proof(a.id)
    proof_b = pl.translog.inclusion_proof(b.id)
    assert proof_a.leaf_index == 0
    assert proof_b.leaf_index == 1   # global order, even though both have per-thread seq 0
    assert verify_inclusion(a.id, seq_a, proof_a, sth)
    assert verify_inclusion(b.id, seq_b, proof_b, sth)


def test_sth_advances_with_each_acceptance(pipeline):
    pl, apriv, apub = pipeline
    sizes = []
    for i in range(3):
        ev = sign_entry(_post("t1", apub, f"msg-{i}"), apriv)
        pl.accept(ev)
        sizes.append(pl.translog.current_sth().tree_size)
    assert sizes == [1, 2, 3]


def test_rejected_entry_does_not_touch_translog(pipeline):
    """A bad signature must fail at step 4 (verify_entry) — translog must stay empty."""
    pl, apriv, apub = pipeline
    ev = sign_entry(_post("t1", apub), apriv)
    ev.body = "tampered after signing"   # invalidates sig/id

    with pytest.raises(AcceptanceError):
        pl.accept(ev)

    assert pl.translog.current_sth().tree_size == 0


def test_unknown_author_rejected_before_translog(pipeline):
    """Directory miss (step 2) must reject before any translog mutation."""
    pl, apriv, apub = pipeline
    other_priv, other_pub = crypto.generate_keypair()   # not in directory
    ev = sign_entry(_post("t1", other_pub), other_priv)

    with pytest.raises(AcceptanceError):
        pl.accept(ev)

    assert pl.translog.current_sth().tree_size == 0


def test_revoked_author_rejected_before_translog(no_structural, hub_keypair, keypair):
    priv, pub = hub_keypair
    apriv, apub = keypair
    directory = StubDirectory(attested={apub: _Att(role="member")},
                              revoked={apub})
    pl = Pipeline(
        store=StubStore(),
        directory=directory,
        translog=TamperEvidentLog(priv, pub),
        overview=StubOverview(),
        ledger=StubLedger(),
        throttler=StubThrottler(),
    )
    ev = sign_entry(_post("t1", apub), apriv)

    with pytest.raises(AcceptanceError):
        pl.accept(ev)

    assert pl.translog.current_sth().tree_size == 0


def test_dangling_parent_rejected_before_translog(pipeline):
    pl, apriv, apub = pipeline
    ev = Entry(thread="t1", author=apub, kind="reply",
               created_at="2026-01-01T00:00:00Z",
               parents=["sha256:" + "ff" * 32], body="reply")
    ev = sign_entry(ev, apriv)

    with pytest.raises(AcceptanceError):
        pl.accept(ev)

    assert pl.translog.current_sth().tree_size == 0


def test_accepted_entry_is_persisted_then_logged(pipeline):
    """Order matters: store.append before translog.append (store is source of truth)."""
    pl, apriv, apub = pipeline
    ev = sign_entry(_post("t1", apub), apriv)

    pl.accept(ev)

    assert pl.store.appended == [(ev.id, 0)]
    # translog leaf at position 0 is hash_leaf(ev.id, 0); inclusion proof checks it.
    proof = pl.translog.inclusion_proof(ev.id)
    assert proof.leaf_index == 0


# ---- receipt acceptance (§8) -----------------------------------------

def test_receipt_entry_feeds_ledger_with_observed_sth(pipeline):
    """A kind='receipt' entry routes through the pipeline like any other —
    auth + sig + parent checks — and additionally calls
    Ledger.apply_receipt with the cumulative-ack payload AND the
    recipient's OBSERVED Signed Tree Head. The observed STH is what
    Ledger.equivocation_signals() cross-checks across recipients (§6.4.3)."""
    from cove.entry import Receipt
    pl, apriv, apub = pipeline
    receipt_payload = Receipt(high_water_seq=17,
                              observed_sth_size=42,
                              observed_sth_root="root_at_observe_time")
    ev = sign_entry(Entry(
        thread="t1", author=apub, kind="receipt",
        created_at="2026-01-01T00:00:00Z", body="",
        receipt=receipt_payload,
    ), apriv)

    pl.accept(ev)

    assert pl.ledger.receipts == [(
        apub, "t1", 17, (42, "root_at_observe_time"),
    )]


def test_receipt_entry_without_payload_is_rejected(pipeline):
    """Acceptance step refuses a kind='receipt' entry with receipt=None —
    the alternative would be silently accepting an opaque marker that
    contributes nothing to the ledger. Loud rejection forces the client
    to send a well-formed receipt."""
    pl, apriv, apub = pipeline
    ev = sign_entry(Entry(
        thread="t1", author=apub, kind="receipt",
        created_at="2026-01-01T00:00:00Z", body="",
    ), apriv)

    with pytest.raises(AcceptanceError, match="receipt"):
        pl.accept(ev)

    # And nothing landed in the ledger or the store.
    assert pl.ledger.receipts == []
    assert pl.store.appended == []


def test_non_receipt_entry_does_not_call_apply_receipt(pipeline):
    """A regular post never feeds the ledger — only kind='receipt' does.
    Regression guard against accidentally always-calling apply_receipt."""
    pl, apriv, apub = pipeline
    ev = sign_entry(_post("t1", apub, "just a post"), apriv)
    pl.accept(ev)
    assert pl.ledger.receipts == []
