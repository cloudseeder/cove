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

from dataclasses import dataclass


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


# ---- leaf / node hashing (RFC 6962-style domain separation recommended) ----
def hash_leaf(entry_id: str, seq: int) -> str:
    """TODO: domain-separated hash of (entry_id, seq). Spec §6.4.1."""
    raise NotImplementedError


def hash_node(left: str, right: str) -> str:
    """TODO: domain-separated internal node hash. Spec §6.4.1."""
    raise NotImplementedError


class TamperEvidentLog:
    """Append-only Merkle log. The entry store is source of truth; this commits order."""

    def __init__(self, hub_private_hex: str, hub_public_hex: str) -> None:
        self._hub_priv = hub_private_hex
        self._hub_pub = hub_public_hex
        # TODO: persist leaves + STH history (store.py). Spec §9.

    def append(self, entry_id: str, seq: int) -> None:
        """Add a leaf for an accepted entry. Spec §7.1 step 8."""
        raise NotImplementedError

    def current_sth(self) -> STH:
        """Compute root, chain prev_sth_hash, sign with hub key. Spec §6.4.1."""
        raise NotImplementedError

    def inclusion_proof(self, entry_id: str) -> InclusionProof:
        """Spec §6.4.2."""
        raise NotImplementedError

    def consistency_proof(self, first_size: int, second_size: int) -> ConsistencyProof:
        """Spec §6.4.2."""
        raise NotImplementedError


# ---- client-side verification (also used by the client; mirror in client repo) ----
def verify_inclusion(entry_id: str, seq: int, proof: InclusionProof, sth: STH) -> bool:
    """TODO: recompute root from leaf + audit_path; compare to sth.root_hash. §6.4.2."""
    raise NotImplementedError


def verify_consistency(proof: ConsistencyProof, old: STH, new: STH) -> bool:
    """TODO: verify the log only grew (append-only) between two STHs. §6.4.2."""
    raise NotImplementedError


def verify_sth(sth: STH) -> bool:
    """TODO: verify hub signature over canonical(STH minus sig). §6.4.1."""
    raise NotImplementedError
