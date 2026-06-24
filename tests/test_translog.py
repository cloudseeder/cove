"""Tamper-evident log contract. Spec §6.4.  ** CRITICAL — build to satisfy these. **

These tests define the guarantee. Implement hub/translog.py until they pass.
Do NOT weaken them. They are the difference between a hub you trust and a hub you
verify. (CLAUDE.md: do not run unsupervised here.)
"""
import pytest

from cove.translog import (
    TamperEvidentLog, verify_inclusion, verify_consistency, verify_sth,
)


@pytest.fixture
def log(hub_keypair):
    priv, pub = hub_keypair
    return TamperEvidentLog(priv, pub)


def _append_n(log, n):
    for i in range(n):
        log.append(entry_id=f"sha256:{i:064x}", seq=i)


def test_sth_is_signed_by_hub_key(log):
    _append_n(log, 4)
    sth = log.current_sth()
    assert sth.tree_size == 4
    assert verify_sth(sth) is True


def test_inclusion_proof_verifies_for_present_event(log):
    _append_n(log, 8)
    sth = log.current_sth()
    proof = log.inclusion_proof("sha256:" + f"{3:064x}")
    assert verify_inclusion("sha256:" + f"{3:064x}", 3, proof, sth) is True


def test_inclusion_proof_fails_for_absent_event(log):
    _append_n(log, 8)
    sth = log.current_sth()
    with pytest.raises(Exception):
        proof = log.inclusion_proof("sha256:" + f"{999:064x}")
        verify_inclusion("sha256:" + f"{999:064x}", 999, proof, sth)


def test_consistency_proof_holds_for_append_only_growth(log):
    _append_n(log, 4)
    old = log.current_sth()
    _append_n(log, 4)            # grow 4 -> 8, append-only
    new = log.current_sth()
    proof = log.consistency_proof(old.tree_size, new.tree_size)
    assert verify_consistency(proof, old, new) is True


def test_consistency_detects_rewrite(log, hub_keypair):
    """A new STH whose history is NOT a superset of the old must fail consistency.

    Simulate by constructing a divergent log and asking for a consistency proof
    between heads at the same size with different roots — must not verify.
    """
    _append_n(log, 4)
    old = log.current_sth()

    priv, pub = hub_keypair
    other = TamperEvidentLog(priv, pub)
    for i in range(4):
        other.append(entry_id=f"sha256:{(i+100):064x}", seq=i)  # different leaves
    divergent = other.current_sth()

    assert old.tree_size == divergent.tree_size
    assert old.root_hash != divergent.root_hash   # equivocation signal (§6.4.3)
    with pytest.raises(Exception):
        proof = log.consistency_proof(old.tree_size, divergent.tree_size)
        verify_consistency(proof, old, divergent)
