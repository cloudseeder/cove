"""Per-thread tamper-evident log for ephemeral threads.
Spec: server-hub-spec.md §6.4 + ephemeral extension. ** CRITICAL CORRECTNESS **

Each ephemeral thread carries its OWN hash chain + Merkle tree + STHs.
Two threads must be provably independent. The STH signature covers the
`thread` field so a signed STH for thread A cannot be relayed as thread
B's — cross-tree substitution must not verify.

Build test-first against tests/test_translog_ephemeral.py. Do NOT weaken
those tests to make them pass, and do not run unsupervised here (CLAUDE.md):
everything downstream trusts these proofs.

The Merkle machinery itself is reused from cove.translog (leaf hashing,
audit path, subproof, consistency path) — this module is the per-thread
STATE manager and thread-scoped SIGN/VERIFY layer. Not a duplicate of
the tree math.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from cove import crypto
from cove.translog import (
    _ZERO_PREV, _audit_path, _consistency_path, _mth, _recompute_root, _sha,
    hash_leaf, hash_node,
)


# ---- data models ---------------------------------------------------------
@dataclass
class EphemeralSTH:
    """Per-thread Signed Tree Head. Same shape as STH plus a `thread` binding
    that is INCLUDED in the signing payload so cross-tree substitution
    (relabeling one thread's STH as another's) fails verification."""
    thread: str
    tree_size: int
    root_hash: str
    prev_sth_hash: str
    timestamp: str
    hub_key: str
    sig: str


@dataclass
class EphemeralInclusionProof:
    thread: str
    leaf_index: int
    tree_size: int
    audit_path: list[str]


@dataclass
class EphemeralConsistencyProof:
    thread: str
    first_size: int
    second_size: int
    path: list[str]


def _sth_content(sth: EphemeralSTH) -> dict:
    """Canonical signing payload — every field except `sig`. `thread` is
    included so the signature binds the tree to its thread identity."""
    return {
        "thread": sth.thread,
        "tree_size": sth.tree_size,
        "root_hash": sth.root_hash,
        "prev_sth_hash": sth.prev_sth_hash,
        "timestamp": sth.timestamp,
        "hub_key": sth.hub_key,
    }


# ---- per-thread state ----------------------------------------------------
class _ThreadState:
    """Isolated Merkle+chain state for a single ephemeral thread. Not exported
    — the EphemeralTransLog owns and multiplexes these."""
    __slots__ = ("leaves", "index", "last_sth", "sealed_sth")

    def __init__(self) -> None:
        self.leaves: list[str] = []
        self.index: dict[str, int] = {}
        self.last_sth: EphemeralSTH | None = None
        # v0.4.38: once close_thread runs, this pins the final STH and
        # further appends raise. Re-closing an already-sealed thread
        # returns this same STH so retrying a seal after a mid-flight
        # error doesn't produce two conflicting tombstones.
        self.sealed_sth: EphemeralSTH | None = None


# ---- log -----------------------------------------------------------------
class EphemeralTransLog:
    """Multiplexes per-thread tamper-evident logs. The entry store is the
    source of truth; this module derives per-thread order + proofs."""

    def __init__(self, hub_private_hex: str, hub_public_hex: str) -> None:
        self._hub_priv = hub_private_hex
        self._hub_pub = hub_public_hex
        self._threads: dict[str, _ThreadState] = {}
        # Serialize mutations + atomic snapshots. RLock so a method that
        # builds a proof can also call current_sth without deadlocking.
        self._lock = threading.RLock()

    # ---- lifecycle -------------------------------------------------------
    def open_thread(self, thread: str) -> None:
        """Register a new ephemeral thread. Idempotent: re-opening an existing
        thread is a no-op (does NOT reset state). The pipeline calls this at
        POST /threads/ephemeral time."""
        with self._lock:
            if thread not in self._threads:
                self._threads[thread] = _ThreadState()

    def _require(self, thread: str) -> _ThreadState:
        state = self._threads.get(thread)
        if state is None:
            raise KeyError(
                f"ephemeral thread {thread!r} not open — call open_thread first"
            )
        return state

    # ---- append ----------------------------------------------------------
    def append(self, *, thread: str, entry_id: str, seq: int) -> None:
        """Add a leaf for an accepted ephemeral entry. Spec §7.1 step 8
        (per-thread variant). Pre-condition: the pipeline has already
        validated the entry belongs to `thread` and that `seq` is the
        next monotonic value within it."""
        with self._lock:
            state = self._require(thread)
            if state.sealed_sth is not None:
                raise ValueError(
                    f"ephemeral thread {thread!r} is sealed — no further appends",
                )
            state.leaves.append(hash_leaf(entry_id, seq))
            state.index[entry_id] = len(state.leaves) - 1

    def rebuild(self, thread: str, entries: Iterable[tuple[str, int]]) -> None:
        """Replace one thread's state from the entry stream. Resets that
        thread's STH chain to genesis so the operator repairing history
        isn't pretending an earlier STH covered the fresh head. Other
        threads' state is left untouched."""
        with self._lock:
            state = self._threads.setdefault(thread, _ThreadState())
            state.leaves.clear()
            state.index.clear()
            state.last_sth = None
            for entry_id, seq in entries:
                state.leaves.append(hash_leaf(entry_id, seq))
                state.index[entry_id] = len(state.leaves) - 1

    # ---- STH -------------------------------------------------------------
    def current_sth(self, thread: str) -> EphemeralSTH:
        with self._lock:
            return self._current_sth_unlocked(thread)

    def _current_sth_unlocked(self, thread: str) -> EphemeralSTH:
        state = self._require(thread)
        size = len(state.leaves)
        root = _mth(state.leaves)
        if state.last_sth is not None and state.last_sth.tree_size == size:
            return state.last_sth
        prev = (
            "sha256:" + _sha(crypto.canonicalize(
                {**_sth_content(state.last_sth), "sig": state.last_sth.sig}
            ))
            if state.last_sth is not None
            else _ZERO_PREV
        )
        sth = EphemeralSTH(
            thread=thread,
            tree_size=size,
            root_hash=root,
            prev_sth_hash=prev,
            timestamp=datetime.now(timezone.utc).isoformat(),
            hub_key=self._hub_pub,
            sig="",
        )
        sth.sig = crypto.sign(self._hub_priv, crypto.canonicalize(_sth_content(sth)))
        state.last_sth = sth
        return sth

    # ---- proofs ----------------------------------------------------------
    def inclusion_proof(self, thread: str, entry_id: str) -> EphemeralInclusionProof:
        with self._lock:
            state = self._require(thread)
            if entry_id not in state.index:
                raise KeyError(f"entry {entry_id} not in thread {thread!r}")
            m = state.index[entry_id]
            return EphemeralInclusionProof(
                thread=thread,
                leaf_index=m,
                tree_size=len(state.leaves),
                audit_path=_audit_path(m, state.leaves),
            )

    def inclusion_proof_and_sth(
        self, thread: str, entry_id: str,
    ) -> tuple[EphemeralInclusionProof, EphemeralSTH]:
        """Atomic snapshot of (proof, STH) under one lock. Same fix as
        translog.py's v0.4.31 bundle: eliminates the race where a
        concurrent append slips between the client's separate /sth and
        /proof/inclusion fetches."""
        with self._lock:
            state = self._require(thread)
            if entry_id not in state.index:
                raise KeyError(f"entry {entry_id} not in thread {thread!r}")
            m = state.index[entry_id]
            proof = EphemeralInclusionProof(
                thread=thread,
                leaf_index=m,
                tree_size=len(state.leaves),
                audit_path=_audit_path(m, state.leaves),
            )
            sth = self._current_sth_unlocked(thread)
        return proof, sth

    # ---- seal (v0.4.38 tombstone substrate) ------------------------------
    def close_thread(self, thread: str) -> EphemeralSTH:
        """Seal the thread: compute + pin the final STH, refuse further
        appends. Returns the sealed STH. Idempotent — a second call
        returns the same STH so a retried seal ceremony (e.g. after a
        mid-flight error between STH computation and tombstone
        publication) doesn't produce two conflicting tombstones."""
        with self._lock:
            state = self._require(thread)
            if state.sealed_sth is not None:
                return state.sealed_sth
            sth = self._current_sth_unlocked(thread)
            state.sealed_sth = sth
            return sth

    def consistency_proof(
        self, thread: str, first_size: int, second_size: int,
    ) -> EphemeralConsistencyProof:
        with self._lock:
            state = self._require(thread)
            n = len(state.leaves)
            if not (0 < first_size <= second_size <= n):
                raise ValueError(
                    f"bad consistency sizes for thread {thread!r}: "
                    f"0 < {first_size} <= {second_size} <= {n}"
                )
            return EphemeralConsistencyProof(
                thread=thread,
                first_size=first_size,
                second_size=second_size,
                path=_consistency_path(first_size, state.leaves[:second_size]),
            )


# ---- client-side verification -------------------------------------------

def verify_sth_ephemeral(sth: EphemeralSTH) -> bool:
    """Verify hub signature over canonical(STH minus sig). The signing
    payload INCLUDES `thread`, so a same-signature STH with a different
    thread label fails verification (cross-tree substitution defense)."""
    return crypto.verify(
        sth.hub_key, sth.sig, crypto.canonicalize(_sth_content(sth)),
    )


def verify_inclusion_ephemeral(
    thread: str, entry_id: str, seq: int,
    proof: EphemeralInclusionProof, sth: EphemeralSTH,
) -> bool:
    """Recompute root from leaf + audit_path; compare to sth.root_hash.
    Rejects if the proof, STH, or verifier's target thread disagree —
    a proof issued for one thread must not verify against another's STH.
    """
    if proof.thread != thread or sth.thread != thread:
        return False
    if not (0 <= proof.leaf_index < proof.tree_size):
        return False
    if proof.tree_size != sth.tree_size:
        return False
    leaf = hash_leaf(entry_id, seq)
    root = _recompute_root(leaf, proof.leaf_index, proof.tree_size, proof.audit_path)
    return root == sth.root_hash


def verify_consistency_ephemeral(
    thread: str, proof: EphemeralConsistencyProof,
    old: EphemeralSTH, new: EphemeralSTH,
) -> bool:
    """Verify the thread's log only grew (append-only) between two STHs.
    All three of the proof and the two STHs must agree on the thread —
    otherwise we return False rather than raising, matching the surface
    of verify_inclusion_ephemeral. Structural / cryptographic failures
    inside the RFC 9162 climb still raise, same as translog.verify_consistency.
    """
    if proof.thread != thread or old.thread != thread or new.thread != thread:
        return False

    m, n = old.tree_size, new.tree_size
    if not (0 < m <= n):
        raise ValueError("bad sizes")
    if m == n:
        if old.root_hash != new.root_hash:
            raise ValueError("equal size, divergent root -> equivocation")
        return list(proof.path) == []

    p = list(proof.path)
    if (m & (m - 1)) == 0:
        p = [old.root_hash] + p
    if not p:
        raise ValueError("empty proof")

    fn, sn = m - 1, n - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1

    old_r = new_r = p[0]
    for c in p[1:]:
        if sn == 0:
            raise ValueError("proof too long")
        if (fn & 1) or (fn == sn):
            old_r = hash_node(c, old_r)
            new_r = hash_node(c, new_r)
            while (not (fn & 1)) and fn != 0:
                fn >>= 1
                sn >>= 1
        else:
            new_r = hash_node(new_r, c)
        fn >>= 1
        sn >>= 1

    if sn != 0:
        raise ValueError("proof too short")
    if old_r != old.root_hash:
        raise ValueError("old root mismatch -> history rewritten")
    if new_r != new.root_hash:
        raise ValueError("new root mismatch")
    return True
