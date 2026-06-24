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
        """Add a leaf for an accepted entry. Spec §7.1 step 8."""
        if seq != len(self._leaves):
            raise ValueError(f"seq {seq} does not match next position {len(self._leaves)}")
        if entry_id in self._index:
            raise ValueError(f"entry {entry_id} already in log at seq {self._index[entry_id]}")
        self._leaves.append(hash_leaf(entry_id, seq))
        self._index[entry_id] = seq

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
        """Spec §6.4.2."""
        raise NotImplementedError

    def consistency_proof(self, first_size: int, second_size: int) -> ConsistencyProof:
        """Spec §6.4.2."""
        raise NotImplementedError


# ---- client-side verification (also used by the client; mirror in client repo) ----
def verify_inclusion(entry_id: str, seq: int, proof: InclusionProof, sth: STH) -> bool:
    """Recompute root from leaf + audit_path; compare to sth.root_hash. §6.4.2."""
    raise NotImplementedError


def verify_consistency(proof: ConsistencyProof, old: STH, new: STH) -> bool:
    """Verify the log only grew (append-only) between two STHs. §6.4.2."""
    raise NotImplementedError


def verify_sth(sth: STH) -> bool:
    """Verify hub signature over canonical(STH minus sig). §6.4.1."""
    return crypto.verify(sth.hub_key, sth.sig, crypto.canonicalize(_sth_content(sth)))
