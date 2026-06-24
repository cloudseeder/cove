"""Identity & directory. Spec: server-hub-spec.md §2.

The organization root key (offline, NOT on the hub) signs attestations binding a
member public key to a directory identity. The directory manifest (attestations
+ revocations) is root-signed; the hub serves it but cannot forge it.

Seam discipline (§11): attestations carry `issuer`. Never hardcode a single root
in a way that blocks multi-root resolution later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Attestation:
    member_pubkey: str
    enc_pubkey: Optional[str]      # null in v1 (sign-only)
    display_name: str
    unit: str
    role: str                      # "member" | "board" | "officer"
    issued_at: str
    expires_at: Optional[str]
    issuer: str                    # root pubkey — keep this; it is the cross-org seam
    sig: str                       # root signature over canonical(attestation minus sig)


@dataclass
class Revocation:
    pubkey: str
    revoked_at: str
    reason: str


def issue_attestation(root_private_hex: str, *, member_pubkey: str, display_name: str,
                      unit: str, role: str, issuer_pubkey: str,
                      expires_at: Optional[str] = None) -> Attestation:
    """Sign an attestation with the ROOT key. Runs in the admin tool, not on the hub. §2.3."""
    raise NotImplementedError


def verify_attestation(att: Attestation) -> bool:
    """Verify the root signature over the attestation. §2.2."""
    raise NotImplementedError


class Directory:
    """In-memory view of the signed directory manifest. Source: root-signed manifest. §2.3."""

    def resolve(self, pubkey: str):  # -> Attestation | None
        """Return the current attestation for a key, or None. Caller checks revocation/expiry."""
        raise NotImplementedError

    def is_revoked(self, pubkey: str, as_of: Optional[str] = None) -> bool:
        """Spec §2.3: entries signed before revocation remain verifiable."""
        raise NotImplementedError
