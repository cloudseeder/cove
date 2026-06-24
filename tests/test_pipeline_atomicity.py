"""Pipeline atomicity under mid-step-8 faults.

§7.1 step 8 has four ordered writes — assign seq, persist, extend translog,
materialize STH — followed by step 9's overview/ledger update. If any of those
raises after an earlier one succeeded, we're in an inconsistent state.

The invariants we pin here are what CLAUDE.md calls 'no silent failures' applied
to internal state, not just external responses:

  1. A failed store.append must NOT burn a per-thread seq (no holes in the line).
  2. The entry store is source of truth (§9): if the translog or overview diverge,
     they can be rebuilt — TamperEvidentLog.rebuild and Overview.rebuild back
     this claim.
  3. accept() either commits *both* store and translog (and then materializes
     STH + overview), or leaves both untouched. Anything in between is
     recoverable from the store via rebuild.

We use REAL EventStore + TamperEvidentLog + Overview here so the rebuild paths
are exercised end-to-end; the directory and throttler are stubbed only because
they don't participate in the fault scenarios.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from cove import crypto
from cove.entry import Entry, sign_entry
from cove.index import Overview, Ledger
from cove.pipeline import AcceptanceError, Pipeline
from cove.store import EventStore
from cove.translog import TamperEvidentLog, verify_inclusion


# ---- minimal stubs for non-fault deps -----------------------------------

@dataclass
class _Att:
    role: str = "member"


class _Dir:
    def __init__(self, pub: str) -> None:
        self._pub = pub
    def resolve(self, pubkey: str):
        return _Att() if pubkey == self._pub else None
    def is_revoked(self, pubkey: str, as_of: Optional[str] = None) -> bool:
        return False


class _Throttler:
    def check_and_consume(self, *_a, **_kw) -> None: pass


@pytest.fixture
def no_structural(monkeypatch):
    monkeypatch.setattr("cove.pipeline.check_structural", lambda ev, cfg=None: None)


@pytest.fixture
def rig(tmp_path, no_structural, hub_keypair, keypair):
    """Real store + translog + overview; stubbed directory and throttler."""
    hub_priv, hub_pub = hub_keypair
    apriv, apub = keypair
    store = EventStore(str(tmp_path / "hub.db"))
    translog = TamperEvidentLog(hub_priv, hub_pub)
    overview = Overview()
    ledger = Ledger()
    pl = Pipeline(store=store, directory=_Dir(apub), translog=translog,
                  overview=overview, ledger=ledger, throttler=_Throttler())
    return pl, apriv, apub


def _post(apriv: str, apub: str, body: str = "hi") -> Entry:
    return sign_entry(Entry(thread="t1", author=apub, kind="post",
                            created_at="2026-01-01T00:00:00Z", body=body),
                      apriv)


# ---- 1. store.append fault: no burned seq, translog untouched -----------

def test_store_failure_does_not_burn_seq(rig, monkeypatch):
    """append_atomic is the contract: if INSERT raises, _next_seq stays put.
    Otherwise a transient DB error would create a permanent hole in the
    seq line that downstream verifiers would never explain."""
    pl, apriv, apub = rig
    ev = _post(apriv, apub, body="will-fail")

    # Patch the store's INSERT to blow up exactly once.
    original_append = pl.store.append
    raised = {"count": 0}

    def fail_once(entry, seq):
        if raised["count"] == 0:
            raised["count"] += 1
            raise RuntimeError("disk full (simulated)")
        return original_append(entry, seq)
    monkeypatch.setattr(pl.store, "append", fail_once)

    with pytest.raises(RuntimeError):
        pl.accept(ev)

    # Translog stayed empty — the failure was before step 8c.
    assert pl.translog.current_sth().tree_size == 0
    # And the seq was NOT burned: a fresh accept gets seq 0, not 1.
    good = _post(apriv, apub, body="recovery")
    seq = pl.accept(good)
    assert seq == 0
    assert pl.store.seq_of(good.id) == 0


# ---- 2. translog.append fault: store committed, recoverable via rebuild --

def test_translog_failure_after_store_commit_is_recoverable_via_rebuild(rig, monkeypatch):
    """The 'store is source of truth' claim is only real if we can actually
    re-derive translog state from the store. This is the test that backs it."""
    pl, apriv, apub = rig
    # First entry succeeds — establishes some known-good state.
    a = _post(apriv, apub, body="a")
    pl.accept(a)
    assert pl.translog.current_sth().tree_size == 1

    # Second entry: store.append succeeds, translog.append blows up.
    b = _post(apriv, apub, body="b")
    monkeypatch.setattr(pl.translog, "append",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("OOM")))
    with pytest.raises(RuntimeError):
        pl.accept(b)

    # Divergence: store has b, translog does not — exactly the inconsistency
    # we're claiming we can repair.
    assert pl.store.exists(b.id)
    assert pl.translog.current_sth().tree_size == 1

    # Lift the patch and rebuild translog from the source of truth.
    monkeypatch.undo()
    pl.translog.rebuild(pl.store.iter_global())

    sth = pl.translog.current_sth()
    assert sth.tree_size == 2
    # Both entries are now provable under the rebuilt head.
    seq_a = pl.store.seq_of(a.id)
    seq_b = pl.store.seq_of(b.id)
    proof_a = pl.translog.inclusion_proof(a.id)
    proof_b = pl.translog.inclusion_proof(b.id)
    assert verify_inclusion(a.id, seq_a, proof_a, sth)
    assert verify_inclusion(b.id, seq_b, proof_b, sth)


# ---- 3. translog.current_sth fault: state consistent, next call recovers ---

def test_current_sth_failure_leaves_log_consistent(rig, monkeypatch):
    """If current_sth() raises after append, the translog leaves are still
    valid (the leaf was appended). Calling current_sth() again — the next
    /sth request — returns a valid head."""
    pl, apriv, apub = rig
    ev = _post(apriv, apub)
    monkeypatch.setattr(pl.translog, "current_sth",
                        lambda: (_ for _ in ()).throw(RuntimeError("sign failed")))
    with pytest.raises(RuntimeError):
        pl.accept(ev)

    # Store and translog are both at size 1 — the divergence is in the
    # *materialized* STH, not the underlying leaf list.
    assert pl.store.exists(ev.id)
    monkeypatch.undo()
    sth = pl.translog.current_sth()       # next request works
    assert sth.tree_size == 1
    proof = pl.translog.inclusion_proof(ev.id)
    assert verify_inclusion(ev.id, pl.store.seq_of(ev.id), proof, sth)


# ---- 4. overview.add fault: store+translog consistent, overview rebuildable ---

def test_overview_failure_is_recoverable_via_rebuild(rig, monkeypatch):
    """Step 9's overview.add isn't atomic with step 8. If it raises after the
    log has been extended, the overview is missing the entry — but it can be
    rebuilt from the store (§6 integrity rule)."""
    pl, apriv, apub = rig
    a = _post(apriv, apub, body="a"); pl.accept(a)        # warm-up: good entry

    b = _post(apriv, apub, body="b")
    monkeypatch.setattr(pl.overview, "add",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("oom")))
    with pytest.raises(RuntimeError):
        pl.accept(b)

    # store + translog committed b; overview did not.
    assert pl.store.exists(b.id)
    assert pl.translog.current_sth().tree_size == 2
    # b is absent from the overview.
    assert [eid for eid, _, _ in pl.overview.thread_entries("t1")] == [a.id]

    # Rebuild overview from store. The store's iter_global gives (id, per-thread
    # seq) tuples globally; for the overview we need (thread, id, parents, seq)
    # which we get by joining iter_global with store.get for parents+thread.
    monkeypatch.undo()
    triples = []
    for entry_id, seq in pl.store.iter_global():
        ev_full = pl.store.get(entry_id)
        triples.append((ev_full.thread, entry_id, ev_full.parents, seq))
    pl.overview.rebuild(triples)

    got = [eid for eid, _, _ in pl.overview.thread_entries("t1")]
    assert got == [a.id, b.id]


# ---- 5. successful accept after a transient store failure ---------------

def test_recovery_after_transient_store_failure(rig, monkeypatch):
    """End-to-end: a transient store failure followed by a clean retry must
    leave the system fully consistent — same seq, same translog, all proofs
    verify. Combines invariants 1 + 'translog matches store after success'."""
    pl, apriv, apub = rig
    ev = _post(apriv, apub)

    calls = {"n": 0}
    original_append = pl.store.append
    def flaky(entry, seq):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return original_append(entry, seq)
    monkeypatch.setattr(pl.store, "append", flaky)

    with pytest.raises(RuntimeError):
        pl.accept(ev)
    seq = pl.accept(ev)                    # client retries; succeeds this time

    assert seq == 0                        # not 1 — first attempt didn't burn seq
    sth = pl.translog.current_sth()
    assert sth.tree_size == 1
    proof = pl.translog.inclusion_proof(ev.id)
    assert verify_inclusion(ev.id, seq, proof, sth)
