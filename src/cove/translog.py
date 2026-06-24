"""Tamper-evident log. Spec: server-hub-spec.md §6.4.  ** CRITICAL CORRECTNESS **

CT-style append-only Merkle log over accepted entries, in global acceptance
order. Each entry commits to the prior log head (hash chain); the leaf is the
entry id + assigned seq. The hub signs tree heads with its OPERATIONAL key
(crypto.generate_keypair) so its claims about history are non-repudiable.

Build this test-first against tests/test_translog.py. Do NOT weaken the tests
to make them pass, and do not run unsupervised here (CLAUDE.md): everything
downstream trusts these proofs.

What this defends (must be detectable): rewrite, reorder, delete of accepted
entries; false denial of inclusion after ack; equivocation (via receipt-carried
STH, §6.4.3). What it does NOT defend: withholding a never-delivered entry.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from datetime import datetime, timezone

from cove import crypto


# ---- domain-separated hashing (RFC 6962 §2.1) -----------------------------
LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"
_ZERO_PREV = "sha256:" + "0" * 64


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def hash_leaf(entry_id: str, seq: int) -> str:
    """Leaf commits to BOTH entry id and assigned seq (its position claim)."""
    data = struct.pack(">Q", seq) + entry_id.encode("ascii")
    return _sha(LEAF_PREFIX + data)


def hash_node(left_hex: str, right_hex: str) -> str:
    return _sha(NODE_PREFIX + bytes.fromhex(left_hex) + bytes.fromhex(right_hex))


def _lp2(n: int) -> int:
    """Largest power of two strictly less than n. Requires n >= 2."""
    return 1 << ((n - 1).bit_length() - 1)


def _mth(leaves: list[str]) -> str:
    """Merkle Tree Hash over an ordered list of *leaf hashes* (RFC 6962)."""
    n = len(leaves)
    if n == 0:
        return _sha(b"")
    if n == 1:
        return leaves[0]
    k = _lp2(n)
    return hash_node(_mth(leaves[:k]), _mth(leaves[k:]))


def _audit_path(m: int, leaves: list[str]) -> list[str]:
    """Sibling hashes from leaf m up to the root, deepest-first (RFC 6962 §2.1.1)."""
    n = len(leaves)
    if n <= 1:
        return []
    k = _lp2(n)
    if m < k:
        return _audit_path(m, leaves[:k]) + [_mth(leaves[k:])]
    return _audit_path(m - k, leaves[k:]) + [_mth(leaves[:k])]


def _recompute_root(leaf_hash: str, m: int, n: int, path: list[str]) -> str:
    """Mirror of _audit_path; consumes path end-first (top sibling)."""
    if n <= 1:
        return leaf_hash
    k = _lp2(n)
    sib, rest = path[-1], path[:-1]
    if m < k:
        return hash_node(_recompute_root(leaf_hash, m, k, rest), sib)
    return hash_node(sib, _recompute_root(leaf_hash, m - k, n - k, rest))


def _subproof(m: int, leaves: list[str], b: bool) -> list[str]:
    """RFC 6962 §2.1.2 SUBPROOF helper.

    `b` is True iff the verifier already holds the root of `leaves` as their
    old root (so it can be omitted from the proof).
    """
    n = len(leaves)
    if m == n:
        return [] if b else [_mth(leaves)]
    k = _lp2(n)
    if m <= k:
        return _subproof(m, leaves[:k], b) + [_mth(leaves[k:])]
    return _subproof(m - k, leaves[k:], False) + [_mth(leaves[:k])]


def _consistency_path(m: int, leaves: list[str]) -> list[str]:
    """Consistency proof path between an old tree of size m and `leaves`."""
    if m == len(leaves):
        return []
    return _subproof(m, leaves, True)


# ---- data models ----------------------------------------------------------
@dataclass
class STH:
    """Signed Tree Head (§6.4.1)."""
    tree_size: int
    root_hash: str
    prev_sth_hash: str
    timestamp: str
    hub_key: str          # hub operational public key (hex)
    sig: str              # hub signature over canonical(STH minus sig)


@dataclass
class InclusionProof:
    leaf_index: int
    tree_size: int
    audit_path: list[str]


@dataclass
class ConsistencyProof:
    first_size: int
    second_size: int
    path: list[str]


def _sth_content(sth: STH) -> dict:
    """Canonical signing payload — every STH field except `sig`."""
    return {
        "tree_size": sth.tree_size,
        "root_hash": sth.root_hash,
        "prev_sth_hash": sth.prev_sth_hash,
        "timestamp": sth.timestamp,
        "hub_key": sth.hub_key,
    }


# ---- log ------------------------------------------------------------------
class TamperEvidentLog:
    """Append-only Merkle log. The entry store is source of truth; this commits order."""

    def __init__(self, hub_private_hex: str, hub_public_hex: str) -> None:
        self._hub_priv = hub_private_hex
        self._hub_pub = hub_public_hex
        self._leaves: list[str] = []                  # leaf hashes in seq order
        self._index: dict[str, int] = {}              # entry_id -> position
        self._last_sth: STH | None = None

    def append(self, entry_id: str, seq: int) -> None:
        """Add a leaf for an accepted entry. Spec §7.1 step 8.

        Pre-condition: the pipeline has already validated that `seq` is the
        next monotonic value and that `entry_id` is unique (§7.1 step 6/7).
        translog trusts that and just records the leaf.
        """
        self._leaves.append(hash_leaf(entry_id, seq))
        self._index[entry_id] = len(self._leaves) - 1

    def current_sth(self) -> STH:
        """Compute root, chain prev_sth_hash, sign with hub key. Spec §6.4.1."""
        size = len(self._leaves)
        root = _mth(self._leaves)
        if self._last_sth is not None and self._last_sth.tree_size == size:
            return self._last_sth
        prev = (
            "sha256:" + _sha(crypto.canonicalize(
                {**_sth_content(self._last_sth), "sig": self._last_sth.sig}
            ))
            if self._last_sth is not None
            else _ZERO_PREV
        )
        sth = STH(
            tree_size=size,
            root_hash=root,
            prev_sth_hash=prev,
            timestamp=datetime.now(timezone.utc).isoformat(),
            hub_key=self._hub_pub,
            sig="",
        )
        sth.sig = crypto.sign(self._hub_priv, crypto.canonicalize(_sth_content(sth)))
        self._last_sth = sth
        return sth

    def inclusion_proof(self, entry_id: str) -> InclusionProof:
        """Spec §6.4.2. Raises KeyError if entry is absent."""
        if entry_id not in self._index:
            raise KeyError(f"entry {entry_id} not in log")
        m = self._index[entry_id]
        return InclusionProof(
            leaf_index=m,
            tree_size=len(self._leaves),
            audit_path=_audit_path(m, self._leaves),
        )

    def consistency_proof(self, first_size: int, second_size: int) -> ConsistencyProof:
        """Spec §6.4.2."""
        if not (0 < first_size <= second_size <= len(self._leaves)):
            raise ValueError(
                f"bad consistency sizes: 0 < {first_size} <= {second_size} <= {len(self._leaves)}"
            )
        return ConsistencyProof(
            first_size=first_size,
            second_size=second_size,
            path=_consistency_path(first_size, self._leaves[:second_size]),
        )


# ---- client-side verification (also used by the client; mirror in client repo) ----
def verify_inclusion(entry_id: str, seq: int, proof: InclusionProof, sth: STH) -> bool:
    """Recompute root from leaf + audit_path; compare to sth.root_hash. §6.4.2.

    `seq` is the per-thread seq the leaf commits to (hash_leaf input).
    `proof.leaf_index` is the GLOBAL log position — they are not the same in
    general (two entries on different threads can both have seq 0). The two
    are bound together by being baked into the same Merkle leaf + position.
    """
    if not (0 <= proof.leaf_index < proof.tree_size):
        return False
    if proof.tree_size != sth.tree_size:
        return False
    leaf = hash_leaf(entry_id, seq)
    root = _recompute_root(leaf, proof.leaf_index, proof.tree_size, proof.audit_path)
    return root == sth.root_hash


def verify_consistency(proof: ConsistencyProof, old: STH, new: STH) -> bool:
    """Verify the log only grew (append-only) between two STHs. §6.4.2.

    Algorithm: RFC 9162 §2.1.4.2. Climbs from the old tree's right edge upward,
    folding sibling hashes from the proof; at the end, the recomputed old root
    must equal `old.root_hash` (history wasn't rewritten) and the recomputed
    new root must equal `new.root_hash`.
    """
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


def verify_sth(sth: STH) -> bool:
    """Verify hub signature over canonical(STH minus sig). §6.4.1."""
    return crypto.verify(sth.hub_key, sth.sig, crypto.canonicalize(_sth_content(sth)))
