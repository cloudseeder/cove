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
                 revoked: set[str] | None = None,
                 caps_by_pubkey: dict[str, set[str]] | None = None) -> None:
        self._attested = attested or {}
        self._revoked = revoked or set()
        # v0.5.0: audience gate reads this via caller_capabilities. Default
        # empty (regular members have no caps) so pre-existing tests are
        # unchanged.
        self._caps_by_pubkey = caps_by_pubkey or {}

    def resolve(self, pubkey: str):
        return self._attested.get(pubkey)

    def is_revoked(self, pubkey: str, as_of: Optional[str] = None) -> bool:
        return pubkey in self._revoked

    def caller_capabilities(self, pubkey: str) -> set[str]:
        return set(self._caps_by_pubkey.get(pubkey, set()))


class StubStore:
    def __init__(self, ephemeral: set[str] | None = None,
                 tombstoned: set[str] | None = None,
                 audiences: dict[str, list[str]] | None = None) -> None:
        self._by_id: dict[str, Entry] = {}
        self._next_seq: dict[str, int] = {}
        self.appended: list[tuple[str, int]] = []   # for assertions
        # v0.4.37: names in this set report is_ephemeral(t) == True.
        # Empty set = pure permanent-thread behavior (default for prior tests).
        self._ephemeral = ephemeral or set()
        # v0.4.49: names in this set report is_tombstoned(t) == True —
        # the pipeline uses this to refuse further writes to a sealed
        # thread. Empty set = no tombstones (default).
        self._tombstoned = tombstoned or set()
        # v0.5.0: pre-seeded thread audiences for the audience-gate tests.
        # Missing key = bootstrap (no prior audience) — the case the pipeline
        # gate accepts unconditionally.
        self._audiences: dict[str, list[str]] = dict(audiences or {})

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

    def is_ephemeral(self, thread: str) -> bool:
        return thread in self._ephemeral

    def is_tombstoned(self, thread: str) -> bool:
        return thread in self._tombstoned

    def thread_audience(self, thread: str):
        """v0.5.0: mimic the shape of cove.store.thread_audience — returns an
        object with `.pubkeys` list, or None for a public/bootstrap thread."""
        pubkeys = self._audiences.get(thread)
        if pubkeys is None:
            return None
        from cove.entry import Audience
        return Audience(pubkeys=list(pubkeys))


class StubThrottler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def check_and_consume(self, author: str, role: str, entry_bytes: int,
                          new_blob_bytes: int = 0) -> None:
        self.calls.append((author, role, entry_bytes))


class StubOverview:
    def __init__(self) -> None:
        self.added: list[tuple[str, str, list[str], int]] = []

    def add(self, thread: str, entry_id: str, parents: list[str], seq: int,
            branch_thread=None) -> None:
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


# ---- v0.4.37: ephemeral thread routing ---------------------------------

from cove.entry import Receipt
from cove.translog_ephemeral import (
    EphemeralTransLog, verify_inclusion_ephemeral,
)


@pytest.fixture
def eph_pipeline(no_structural, hub_keypair, keypair):
    """Pipeline wired with an ephemeral translog and one already-open
    ephemeral thread named 'beach'. Everything else mirrors the base
    `pipeline` fixture."""
    priv, pub = hub_keypair
    apriv, apub = keypair
    directory = StubDirectory(attested={apub: _Att(role="member")})
    eph = EphemeralTransLog(priv, pub)
    eph.open_thread("beach")
    pl = Pipeline(
        store=StubStore(ephemeral={"beach"}),
        directory=directory,
        translog=TamperEvidentLog(priv, pub),
        overview=StubOverview(),
        ledger=StubLedger(),
        throttler=StubThrottler(),
        ephemeral_translog=eph,
    )
    return pl, apriv, apub


def test_ephemeral_post_goes_to_ephemeral_log_not_main(eph_pipeline):
    pl, apriv, apub = eph_pipeline
    ev = sign_entry(_post("beach", apub, "hey"), apriv)

    seq = pl.accept(ev)

    # Main log stays empty; ephemeral log has the leaf.
    assert pl.translog.current_sth().tree_size == 0
    esth = pl.ephemeral_translog.current_sth("beach")
    assert esth.tree_size == 1
    assert esth.thread == "beach"
    # Inclusion proof from the ephemeral log verifies with the ephemeral STH.
    proof = pl.ephemeral_translog.inclusion_proof("beach", ev.id)
    assert verify_inclusion_ephemeral("beach", ev.id, seq, proof, esth) is True


def test_permanent_and_ephemeral_logs_stay_isolated(eph_pipeline):
    """A permanent post next to an ephemeral post: each ends up in its own
    tree only. Cross-contamination is exactly the failure the per-thread
    STH binding is defending against."""
    pl, apriv, apub = eph_pipeline
    perm = sign_entry(_post("permanent-thread", apub, "perm"), apriv)
    eph  = sign_entry(_post("beach", apub, "eph"), apriv)

    pl.accept(perm)
    pl.accept(eph)

    assert pl.translog.current_sth().tree_size == 1
    assert pl.ephemeral_translog.current_sth("beach").tree_size == 1


@pytest.mark.parametrize("bad_kind", [
    "notice", "membership", "supersede", "revoke",
    "branch", "archive", "reopen",
    # v0.4.48: `audience` was previously rejected but is now permitted
    # (routing, not governance — dies with the thread). See
    # test_audience_kind_accepted_in_ephemeral below.
])
def test_governance_kinds_rejected_structurally_in_ephemeral(eph_pipeline, bad_kind):
    """post/reply/receipt/audience allowed inside an ephemeral thread;
    the governance-shape kinds listed above must be rejected BEFORE seq
    allocation so a caller can't burn seq numbers with rejected attempts."""
    pl, apriv, apub = eph_pipeline
    ev = Entry(
        thread="beach", author=apub, kind=bad_kind,
        created_at="2026-01-01T00:00:00Z",
        # branch needs a target thread; provide one so the *governance*
        # rejection is what we're testing, not a structural miss.
        branch_thread="somewhere" if bad_kind == "branch" else None,
    )
    ev = sign_entry(ev, apriv)

    with pytest.raises(AcceptanceError, match="not permitted in ephemeral"):
        pl.accept(ev)

    # Neither log advanced.
    assert pl.translog.current_sth().tree_size == 0
    assert pl.ephemeral_translog.current_sth("beach").tree_size == 0


def test_audience_kind_accepted_in_ephemeral(eph_pipeline):
    """v0.4.48: audience is per-thread routing, not governance, so it's
    allowed in ephemeral threads. Without this, "group ephemeral thread"
    (a private conversation with just a few people that expires) was
    structurally impossible — the pipeline rejected the very entry that
    would scope the audience."""
    from cove.entry import Audience
    pl, apriv, apub = eph_pipeline
    ev = sign_entry(Entry(
        thread="beach", author=apub, kind="audience",
        created_at="2026-01-01T00:00:00Z", body="",
        audience=Audience(pubkeys=[apub]),
    ), apriv)

    # Should NOT raise — pipeline accepts it into the ephemeral log.
    pl.accept(ev)

    assert pl.ephemeral_translog.current_sth("beach").tree_size == 1
    assert pl.translog.current_sth().tree_size == 0    # not the main log


def test_ephemeral_receipt_feeds_ledger(eph_pipeline):
    """Receipts are one of the three allowed kinds in ephemeral threads.
    A receipt in an ephemeral thread must still feed the ledger — that's
    the accountability substrate that makes ephemeral threads verifiable
    while they're alive."""
    pl, apriv, apub = eph_pipeline
    # First, a post to establish something to ack.
    p = sign_entry(_post("beach", apub, "hi"), apriv)
    pl.accept(p)
    # Now a receipt against tree_size=1.
    r_ev = Entry(
        thread="beach", author=apub, kind="receipt",
        created_at="2026-01-01T00:00:00Z", body="",
        receipt=Receipt(
            high_water_seq=0, observed_sth_size=1,
            observed_sth_root="a" * 64,
        ),
    )
    r_ev = sign_entry(r_ev, apriv)

    pl.accept(r_ev)

    assert pl.ledger.receipts == [(apub, "beach", 0, (1, "a" * 64))]


def test_writes_to_tombstoned_thread_are_rejected(no_structural, hub_keypair, keypair):
    """v0.4.49: after a thread is sealed (tombstoned_at set), the pipeline
    must refuse further writes to that thread name. Otherwise a post
    would silently land in the main log next to the tombstone entry —
    "the thread is deleted" would be a lie."""
    priv, pub = hub_keypair
    apriv, apub = keypair
    directory = StubDirectory(attested={apub: _Att(role="member")})
    pl = Pipeline(
        store=StubStore(tombstoned={"beach"}),
        directory=directory,
        translog=TamperEvidentLog(priv, pub),
        overview=StubOverview(),
        ledger=StubLedger(),
        throttler=StubThrottler(),
    )
    ev = sign_entry(_post("beach", apub, "sneaky post-tombstone message"), apriv)
    with pytest.raises(AcceptanceError, match="tombstoned"):
        pl.accept(ev)
    # Neither log advanced.
    assert pl.translog.current_sth().tree_size == 0


def test_ephemeral_thread_without_translog_wired_is_loud(no_structural, hub_keypair, keypair):
    """If the store reports a thread as ephemeral but the pipeline was
    constructed without the ephemeral translog, we refuse. Silent
    single-tree accumulation would be worst-case correctness loss."""
    priv, pub = hub_keypair
    apriv, apub = keypair
    directory = StubDirectory(attested={apub: _Att(role="member")})
    pl = Pipeline(
        store=StubStore(ephemeral={"beach"}),
        directory=directory,
        translog=TamperEvidentLog(priv, pub),
        overview=StubOverview(),
        ledger=StubLedger(),
        throttler=StubThrottler(),
        # ephemeral_translog INTENTIONALLY omitted
    )
    ev = sign_entry(_post("beach", apub, "hi"), apriv)
    with pytest.raises(AcceptanceError, match="no ephemeral translog wired"):
        pl.accept(ev)


# ---- v0.5.0: audience-change write-side gate (spec §3.x) -----------------

def _audience(thread: str, author_pub: str, pubkeys: list[str]) -> Entry:
    from cove.entry import Audience
    return Entry(thread=thread, author=author_pub, kind="audience",
                 created_at="2026-01-01T00:00:00Z", body="",
                 audience=Audience(pubkeys=list(pubkeys)))


def _audience_pipeline(hub_keypair, apriv, apub, *,
                       audiences: dict[str, list[str]] | None = None,
                       caps_by_pubkey: dict[str, set[str]] | None = None,
                       extra_attested: dict[str, _Att] | None = None):
    """Helper: pipeline with pre-seeded per-thread audiences + capabilities.

    Callers pass the raw (apriv, apub) so `apub` can appear inside `audiences`
    without a forward-ref chicken-and-egg.
    """
    priv, pub = hub_keypair
    attested = {apub: _Att(role="member")}
    if extra_attested:
        attested.update(extra_attested)
    directory = StubDirectory(
        attested=attested,
        caps_by_pubkey=caps_by_pubkey or {},
    )
    pl = Pipeline(
        store=StubStore(audiences=audiences),
        directory=directory,
        translog=TamperEvidentLog(priv, pub),
        overview=StubOverview(),
        ledger=StubLedger(),
        throttler=StubThrottler(),
    )
    return pl


def test_audience_bootstrap_by_any_member_accepted(no_structural, hub_keypair, keypair):
    """Rule 1: a thread with no prior audience — any attested member can
    establish the scope. Preserves pre-v0.5.0 bootstrap behavior."""
    apriv, apub = keypair
    pl = _audience_pipeline(hub_keypair, apriv, apub)
    ev = sign_entry(_audience("t1", apub, [apub]), apriv)
    pl.accept(ev)   # must not raise
    assert pl.store.appended == [(ev.id, 0)]


def test_audience_reject_when_author_not_in_current_audience(
        no_structural, hub_keypair, keypair):
    """Rule 2: existing audience present, author not in it → reject with
    structured reason (silent-failure elimination)."""
    apriv, apub = keypair
    other_priv, other_pub = crypto.generate_keypair()
    pl = _audience_pipeline(
        hub_keypair, apriv, apub,
        audiences={"t1": [other_pub]},
        extra_attested={other_pub: _Att(role="member")},
    )
    ev = sign_entry(_audience("t1", apub, [apub, other_pub]), apriv)
    with pytest.raises(AcceptanceError, match="not_in_audience"):
        pl.accept(ev)
    assert pl.store.appended == []


def test_audience_self_leave_accepted_by_any_member(
        no_structural, hub_keypair, keypair):
    """Rule 5: removed = {author} — accepted even without manage_audience."""
    apriv, apub = keypair
    other_priv, other_pub = crypto.generate_keypair()
    pl = _audience_pipeline(
        hub_keypair, apriv, apub,
        audiences={"t1": [apub, other_pub]},
        extra_attested={other_pub: _Att(role="member")},
    )
    # apub leaves — new list is just [other_pub]
    ev = sign_entry(_audience("t1", apub, [other_pub]), apriv)
    pl.accept(ev)   # must not raise
    assert pl.store.appended == [(ev.id, 0)]


def test_audience_add_only_accepted_by_any_member(
        no_structural, hub_keypair, keypair):
    """Rule 4: additive — accepted for any in-audience author."""
    apriv, apub = keypair
    other_priv, other_pub = crypto.generate_keypair()
    pl = _audience_pipeline(
        hub_keypair, apriv, apub,
        audiences={"t1": [apub]},
        extra_attested={other_pub: _Att(role="member")},
    )
    ev = sign_entry(_audience("t1", apub, [apub, other_pub]), apriv)
    pl.accept(ev)   # must not raise
    assert pl.store.appended == [(ev.id, 0)]


def test_audience_other_remove_by_member_rejected(
        no_structural, hub_keypair, keypair):
    """Rule 3: removing someone else without manage_audience → rejected."""
    apriv, apub = keypair
    other_priv, other_pub = crypto.generate_keypair()
    pl = _audience_pipeline(
        hub_keypair, apriv, apub,
        audiences={"t1": [apub, other_pub]},
        extra_attested={other_pub: _Att(role="member")},
    )
    ev = sign_entry(_audience("t1", apub, [apub]), apriv)   # drops other_pub
    with pytest.raises(AcceptanceError, match="removal_requires_manage_audience"):
        pl.accept(ev)
    assert pl.store.appended == []


def test_audience_other_remove_by_board_accepted(
        no_structural, hub_keypair, keypair):
    """Rule 3: board author (has manage_audience by default map) — accepted."""
    from cove.audience import MANAGE_AUDIENCE_CAP
    apriv, apub = keypair
    other_priv, other_pub = crypto.generate_keypair()
    pl = _audience_pipeline(
        hub_keypair, apriv, apub,
        audiences={"t1": [apub, other_pub]},
        extra_attested={other_pub: _Att(role="member")},
        caps_by_pubkey={apub: {MANAGE_AUDIENCE_CAP, "admin", "archive"}},
    )
    ev = sign_entry(_audience("t1", apub, [apub]), apriv)
    pl.accept(ev)
    assert pl.store.appended == [(ev.id, 0)]


def test_audience_other_remove_by_officer_accepted(
        no_structural, hub_keypair, keypair):
    """Rule 3: officer author (manage_audience by default map) — accepted."""
    from cove.audience import MANAGE_AUDIENCE_CAP
    apriv, apub = keypair
    other_priv, other_pub = crypto.generate_keypair()
    pl = _audience_pipeline(
        hub_keypair, apriv, apub,
        audiences={"t1": [apub, other_pub]},
        extra_attested={other_pub: _Att(role="member")},
        caps_by_pubkey={apub: {MANAGE_AUDIENCE_CAP}},   # officer's default cap
    )
    ev = sign_entry(_audience("t1", apub, [apub]), apriv)
    pl.accept(ev)
    assert pl.store.appended == [(ev.id, 0)]


def test_audience_mixed_add_plus_other_remove_by_member_rejected(
        no_structural, hub_keypair, keypair):
    """A mixed change that adds a new pubkey AND removes an existing one —
    the removal-of-other still fails the diff-gate for a plain member."""
    apriv, apub = keypair
    other_priv, other_pub = crypto.generate_keypair()
    third_priv, third_pub = crypto.generate_keypair()
    pl = _audience_pipeline(
        hub_keypair, apriv, apub,
        audiences={"t1": [apub, other_pub]},
        extra_attested={other_pub: _Att(role="member"),
                        third_pub: _Att(role="member")},
    )
    # apub adds third_pub AND drops other_pub
    ev = sign_entry(_audience("t1", apub, [apub, third_pub]), apriv)
    with pytest.raises(AcceptanceError, match="removal_requires_manage_audience"):
        pl.accept(ev)


def test_audience_mixed_self_leave_plus_add_accepted_by_member(
        no_structural, hub_keypair, keypair):
    """Self-leave combined with an add — no other member is being removed,
    so the diff-gate passes for a plain member."""
    apriv, apub = keypair
    other_priv, other_pub = crypto.generate_keypair()
    third_priv, third_pub = crypto.generate_keypair()
    pl = _audience_pipeline(
        hub_keypair, apriv, apub,
        audiences={"t1": [apub, other_pub]},
        extra_attested={other_pub: _Att(role="member"),
                        third_pub: _Att(role="member")},
    )
    # apub adds third_pub AND leaves themselves — removed = {apub}
    ev = sign_entry(_audience("t1", apub, [other_pub, third_pub]), apriv)
    pl.accept(ev)   # must not raise
    assert pl.store.appended == [(ev.id, 0)]


def test_audience_manage_audience_actor_not_in_audience_rejected(
        no_structural, hub_keypair, keypair):
    """Rule 2 short-circuits Rule 3: a board/officer member NOT in the
    current audience cannot post an audience change — governance authority
    is exercised from within the thread. Any current member can add them
    first, then they can act."""
    from cove.audience import MANAGE_AUDIENCE_CAP
    apriv, apub = keypair
    other_priv, other_pub = crypto.generate_keypair()
    pl = _audience_pipeline(
        hub_keypair, apriv, apub,
        audiences={"t1": [other_pub]},   # apub NOT in audience
        extra_attested={other_pub: _Att(role="member")},
        caps_by_pubkey={apub: {MANAGE_AUDIENCE_CAP}},
    )
    ev = sign_entry(_audience("t1", apub, [apub, other_pub]), apriv)
    with pytest.raises(AcceptanceError, match="not_in_audience"):
        pl.accept(ev)


def test_audience_noop_accepted(no_structural, hub_keypair, keypair):
    """Idempotent no-op: new == old. Accepted (subset of the additive case)."""
    apriv, apub = keypair
    other_priv, other_pub = crypto.generate_keypair()
    pl = _audience_pipeline(
        hub_keypair, apriv, apub,
        audiences={"t1": [apub, other_pub]},
        extra_attested={other_pub: _Att(role="member")},
    )
    ev = sign_entry(_audience("t1", apub, [apub, other_pub]), apriv)
    pl.accept(ev)   # must not raise
    assert pl.store.appended == [(ev.id, 0)]
