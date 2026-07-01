"""Per-thread tamper-evident log contract. Spec §6.4 + ephemeral extension.

** CRITICAL — build to satisfy these. **

Mirrors test_translog.py's per-log contract, but every operation is keyed by
`thread` because each ephemeral thread carries its OWN chain + Merkle tree +
STHs. Two independent threads must be provably independent — cross-tree
substitution (serving thread A's STH as thread B's) must not verify.

Do NOT weaken these. They are the difference between an ephemeral hub you
trust and one you verify. (CLAUDE.md: do not run unsupervised on translog.)
"""
import pytest

from cove.translog_ephemeral import (
    EphemeralTransLog, verify_inclusion_ephemeral, verify_consistency_ephemeral,
    verify_sth_ephemeral,
)


@pytest.fixture
def elog(hub_keypair):
    priv, pub = hub_keypair
    return EphemeralTransLog(priv, pub)


def _open(elog, thread):
    elog.open_thread(thread)


def _append_n(elog, thread, n, offset=0):
    for i in range(n):
        elog.append(thread=thread, entry_id=f"sha256:{i+offset:064x}", seq=i)


# ---- lifecycle -----------------------------------------------------------

def test_open_thread_is_idempotent(elog):
    """Re-opening the same thread must not clobber state."""
    elog.open_thread("beach")
    elog.append(thread="beach", entry_id="sha256:" + "aa" * 32, seq=0)
    elog.open_thread("beach")   # no-op — must not reset
    sth = elog.current_sth("beach")
    assert sth.tree_size == 1


def test_append_before_open_is_rejected(elog):
    """No auto-create. The pipeline is the gatekeeper — the translog refuses
    to log entries for a thread that was never opened as ephemeral."""
    with pytest.raises(Exception):
        elog.append(thread="never-opened", entry_id="sha256:" + "aa" * 32, seq=0)


def test_current_sth_before_open_is_rejected(elog):
    with pytest.raises(Exception):
        elog.current_sth("never-opened")


# ---- STH signing (per-thread) -------------------------------------------

def test_sth_is_signed_by_hub_key_and_binds_thread(elog):
    _open(elog, "beach")
    _append_n(elog, "beach", 4)
    sth = elog.current_sth("beach")
    assert sth.tree_size == 4
    assert sth.thread == "beach"
    assert verify_sth_ephemeral(sth) is True


def test_sth_signature_covers_the_thread_field(elog, hub_keypair):
    """Cross-tree substitution attack: a signed STH for thread A must not
    verify when the thread field is swapped to B. Otherwise the hub could
    quietly serve one thread's history as another's."""
    _open(elog, "beach")
    _append_n(elog, "beach", 3)
    sth = elog.current_sth("beach")
    # Forge a same-signature STH with a different thread label.
    from dataclasses import replace
    forged = replace(sth, thread="lake")
    assert verify_sth_ephemeral(forged) is False


# ---- Merkle inclusion (per-thread) --------------------------------------

def test_inclusion_proof_verifies_for_present_event(elog):
    _open(elog, "beach")
    _append_n(elog, "beach", 8)
    sth = elog.current_sth("beach")
    target = f"sha256:{3:064x}"
    proof = elog.inclusion_proof("beach", target)
    assert verify_inclusion_ephemeral("beach", target, 3, proof, sth) is True


def test_inclusion_proof_fails_for_absent_event(elog):
    _open(elog, "beach")
    _append_n(elog, "beach", 8)
    sth = elog.current_sth("beach")
    with pytest.raises(Exception):
        elog.inclusion_proof("beach", "sha256:" + f"{999:064x}")


def test_inclusion_proof_is_thread_scoped(elog):
    """A proof issued from thread A must NOT verify against thread B's STH,
    even if the leaves happen to line up by coincidence."""
    _open(elog, "beach")
    _open(elog, "lake")
    _append_n(elog, "beach", 4, offset=0)
    _append_n(elog, "lake",  4, offset=100)
    beach_sth = elog.current_sth("beach")
    lake_sth  = elog.current_sth("lake")
    beach_target = f"sha256:{2:064x}"
    beach_proof = elog.inclusion_proof("beach", beach_target)
    # Correct: verifies against its own thread's STH.
    assert verify_inclusion_ephemeral("beach", beach_target, 2, beach_proof, beach_sth) is True
    # Cross-tree: proof is for beach, verifier is asked to check lake — must fail.
    assert verify_inclusion_ephemeral("lake", beach_target, 2, beach_proof, lake_sth) is False


def test_inclusion_proof_and_sth_atomic_snapshot(elog):
    """v0.4.31-style atomic bundle: proof + STH from a single locked read
    so a concurrent append can't slip between them."""
    _open(elog, "beach")
    _append_n(elog, "beach", 5)
    target = f"sha256:{2:064x}"
    proof, sth = elog.inclusion_proof_and_sth("beach", target)
    assert proof.tree_size == sth.tree_size
    assert verify_inclusion_ephemeral("beach", target, 2, proof, sth) is True


# ---- Consistency (per-thread) -------------------------------------------

def test_consistency_proof_holds_for_append_only_growth(elog):
    _open(elog, "beach")
    _append_n(elog, "beach", 4)
    old = elog.current_sth("beach")
    _append_n(elog, "beach", 4, offset=4)     # grow 4 -> 8
    new = elog.current_sth("beach")
    proof = elog.consistency_proof("beach", old.tree_size, new.tree_size)
    assert verify_consistency_ephemeral("beach", proof, old, new) is True


def test_consistency_detects_rewrite(elog, hub_keypair):
    _open(elog, "beach")
    _append_n(elog, "beach", 4)
    old = elog.current_sth("beach")

    # Construct a divergent log for the same thread with different leaves.
    priv, pub = hub_keypair
    other = EphemeralTransLog(priv, pub)
    other.open_thread("beach")
    for i in range(4):
        other.append(thread="beach", entry_id=f"sha256:{(i+100):064x}", seq=i)
    divergent = other.current_sth("beach")

    assert old.tree_size == divergent.tree_size
    assert old.root_hash != divergent.root_hash    # §6.4.3 equivocation signal
    with pytest.raises(Exception):
        proof = elog.consistency_proof("beach", old.tree_size, divergent.tree_size)
        verify_consistency_ephemeral("beach", proof, old, divergent)


def test_consistency_proof_rejects_cross_thread(elog):
    """A consistency proof issued from thread A must not verify against
    thread B's STHs. Same defense as the inclusion-proof case."""
    _open(elog, "beach")
    _open(elog, "lake")
    _append_n(elog, "beach", 4)
    _append_n(elog, "lake",  4, offset=100)
    beach_old = elog.current_sth("beach")
    _append_n(elog, "beach", 4, offset=4)
    beach_new = elog.current_sth("beach")

    proof = elog.consistency_proof("beach", beach_old.tree_size, beach_new.tree_size)
    lake_sth = elog.current_sth("lake")
    # Beach proof + one lake STH: verifier must reject on thread mismatch.
    assert verify_consistency_ephemeral(
        "lake", proof, beach_old, lake_sth,
    ) is False


# ---- Multi-thread isolation ---------------------------------------------

def test_threads_are_independent(elog):
    """Appending to thread A must not change thread B's STH root or size."""
    _open(elog, "beach")
    _open(elog, "lake")
    _append_n(elog, "beach", 3)
    lake_before = elog.current_sth("lake")
    _append_n(elog, "beach", 5, offset=3)
    lake_after = elog.current_sth("lake")
    assert lake_before.tree_size == lake_after.tree_size == 0
    assert lake_before.root_hash == lake_after.root_hash


def test_sths_chain_within_a_thread(elog):
    """Each thread has its OWN STH chain — issuing STH N+1 must reference
    STH N's hash in prev_sth_hash. Cross-thread chains must not tangle."""
    _open(elog, "beach")
    _append_n(elog, "beach", 2)
    first = elog.current_sth("beach")
    _append_n(elog, "beach", 3, offset=2)
    second = elog.current_sth("beach")
    assert second.prev_sth_hash != "sha256:" + "0" * 64
    assert first.prev_sth_hash == "sha256:" + "0" * 64


# ---- Rebuild (per-thread) -----------------------------------------------

def test_rebuild_replaces_state_from_source_of_truth(elog):
    _open(elog, "beach")
    _append_n(elog, "beach", 5)
    canonical = elog.current_sth("beach")
    canonical_root = canonical.root_hash

    # Corrupt with extra leaves.
    elog.append(thread="beach", entry_id="sha256:" + "cc" * 32, seq=99)
    assert elog.current_sth("beach").root_hash != canonical_root

    elog.rebuild("beach", [(f"sha256:{i:064x}", i) for i in range(5)])
    rebuilt = elog.current_sth("beach")
    assert rebuilt.tree_size == 5
    assert rebuilt.root_hash == canonical_root
    assert rebuilt.prev_sth_hash == "sha256:" + "0" * 64  # chain resets to genesis


def test_rebuild_of_one_thread_leaves_others_untouched(elog):
    _open(elog, "beach")
    _open(elog, "lake")
    _append_n(elog, "beach", 3)
    _append_n(elog, "lake",  4, offset=100)
    lake_root_before = elog.current_sth("lake").root_hash

    elog.rebuild("beach", [(f"sha256:{i:064x}", i) for i in range(2)])
    lake_root_after = elog.current_sth("lake").root_hash
    assert lake_root_before == lake_root_after


# ---- close_thread (v0.4.38 tombstone substrate) ------------------------

def test_close_thread_returns_final_sth_matching_current_state(elog):
    """Sealing must return the same STH the live log would have produced
    at that instant — otherwise the tombstone commits to a different
    history than what members saw."""
    _open(elog, "beach")
    _append_n(elog, "beach", 5)
    live = elog.current_sth("beach")
    final = elog.close_thread("beach")
    assert final.tree_size == live.tree_size
    assert final.root_hash == live.root_hash
    assert final.thread == "beach"
    assert verify_sth_ephemeral(final) is True


def test_append_after_close_is_rejected(elog):
    """Post-seal, the thread is frozen. Any further append must raise —
    a message landing after the tombstone would silently escape the
    accountability window."""
    _open(elog, "beach")
    _append_n(elog, "beach", 2)
    elog.close_thread("beach")
    with pytest.raises(Exception):
        elog.append(thread="beach", entry_id="sha256:" + "ff" * 32, seq=99)


def test_close_thread_is_idempotent(elog):
    """Re-closing an already-closed thread returns the same final STH.
    Enables the seal ceremony to be safely retried after a mid-flight
    error without producing two conflicting tombstones."""
    _open(elog, "beach")
    _append_n(elog, "beach", 3)
    first = elog.close_thread("beach")
    second = elog.close_thread("beach")
    assert first.root_hash == second.root_hash
    assert first.tree_size == second.tree_size


def test_close_thread_on_unopened_raises(elog):
    with pytest.raises(Exception):
        elog.close_thread("never-opened")


def test_close_one_thread_leaves_others_writable(elog):
    _open(elog, "beach")
    _open(elog, "lake")
    _append_n(elog, "beach", 2)
    _append_n(elog, "lake", 2, offset=100)
    elog.close_thread("beach")
    # Lake is still writable and its STH still advances.
    elog.append(thread="lake", entry_id=f"sha256:{999:064x}", seq=2)
    assert elog.current_sth("lake").tree_size == 3
