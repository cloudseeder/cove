"""Identity & directory. Spec: server-hub-spec.md §2.

The organization root key (offline, NOT on the hub) signs attestations binding a
member public key to a directory identity. The directory manifest (attestations
+ revocations) is root-signed; the hub serves it but cannot forge it.

Seam discipline (§11): attestations carry `issuer`. Never hardcode a single root
in a way that blocks multi-root resolution later — verify_attestation must key
off `att.issuer`, not a global constant.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import crypto


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


@dataclass
class DirectoryManifest:
    """The signed directory wire format (§2.3)."""
    org: str                        # root pubkey
    attestations: list[Attestation] = field(default_factory=list)
    revocations: list[Revocation] = field(default_factory=list)
    updated_at: str = ""
    sig: str = ""                   # root sig over canonical(manifest minus sig)


# ---- signing payloads (everything except `sig`) -------------------------
def _att_content(att: Attestation) -> dict:
    return {k: v for k, v in asdict(att).items() if k != "sig"}


def _manifest_content(m: DirectoryManifest) -> dict:
    return {
        "org": m.org,
        "attestations": [asdict(a) for a in m.attestations],
        "revocations": [asdict(r) for r in m.revocations],
        "updated_at": m.updated_at,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- attestation -------------------------------------------------------
def issue_attestation(root_private_hex: str, *, member_pubkey: str, display_name: str,
                      unit: str, role: str, issuer_pubkey: str,
                      issued_at: Optional[str] = None,
                      expires_at: Optional[str] = None,
                      enc_pubkey: Optional[str] = None) -> Attestation:
    """Sign an attestation with the ROOT key. Runs in the admin tool, not on the hub. §2.3.

    enc_pubkey defaults to None (sign-only in v1, per §10.2). The hub MUST NOT
    hold the root private key (CLAUDE.md non-negotiable #1), so this function
    runs in the admin tooling that does.
    """
    att = Attestation(
        member_pubkey=member_pubkey,
        enc_pubkey=enc_pubkey,
        display_name=display_name,
        unit=unit,
        role=role,
        issued_at=issued_at or _now(),
        expires_at=expires_at,
        issuer=issuer_pubkey,
        sig="",
    )
    att.sig = crypto.sign(root_private_hex, crypto.canonicalize(_att_content(att)))
    return att


def verify_attestation(att: Attestation) -> bool:
    """Verify the issuer's signature over the attestation. §2.2.

    Keys off `att.issuer` — this is the cross-org seam: the verifier does not
    pin a single root, so the same machinery generalizes to multi-root later
    (§11) without churn.
    """
    return crypto.verify(att.issuer, att.sig, crypto.canonicalize(_att_content(att)))


# ---- directory manifest ------------------------------------------------
def issue_directory(root_private_hex: str, *, org: str,
                    attestations: list[Attestation],
                    revocations: list[Revocation],
                    updated_at: Optional[str] = None) -> DirectoryManifest:
    """Build and sign a directory manifest with the ROOT key. Admin-tool only."""
    m = DirectoryManifest(
        org=org,
        attestations=list(attestations),
        revocations=list(revocations),
        updated_at=updated_at or _now(),
        sig="",
    )
    m.sig = crypto.sign(root_private_hex, crypto.canonicalize(_manifest_content(m)))
    return m


def verify_directory_manifest(m: DirectoryManifest) -> bool:
    """Verify the root signature over the manifest AND every contained
    attestation's own signature. A valid outer sig is necessary but not
    sufficient — a forged attestation slipped into a freshly-signed manifest
    by a careless admin tool would still fail at the inner check."""
    if not crypto.verify(m.org, m.sig, crypto.canonicalize(_manifest_content(m))):
        return False
    return all(verify_attestation(a) for a in m.attestations)


# ---- in-memory view ----------------------------------------------------
class Directory:
    """In-memory view of the signed directory manifest. Source: root-signed manifest. §2.3.

    Constructed either directly (admin tooling, tests) or via
    `Directory.from_manifest` after the manifest has been verified.
    """

    def __init__(self, attestations: Optional[list[Attestation]] = None,
                 revocations: Optional[list[Revocation]] = None) -> None:
        # Keep latest attestation per pubkey (by issued_at) — handles re-attestation
        # after key rotation. The DAG of historical attestations is in the manifest
        # history; this in-memory view is the "current" lookup.
        self._by_key: dict[str, Attestation] = {}
        # Keep EARLIEST revocation per pubkey — a key can't be un-revoked, so the
        # first revocation is the binding one for any historical lookup.
        self._revoked: dict[str, Revocation] = {}
        self._manifest: Optional[DirectoryManifest] = None
        self._absorb(attestations or [], revocations or [])

    def _absorb(self, attestations: list[Attestation],
                revocations: list[Revocation]) -> None:
        for a in attestations:
            existing = self._by_key.get(a.member_pubkey)
            if existing is None or a.issued_at > existing.issued_at:
                self._by_key[a.member_pubkey] = a
        for r in revocations:
            existing = self._revoked.get(r.pubkey)
            if existing is None or r.revoked_at < existing.revoked_at:
                self._revoked[r.pubkey] = r

    @property
    def manifest(self) -> Optional[DirectoryManifest]:
        return self._manifest

    def update_from(self, m: DirectoryManifest) -> None:
        """Replace internal state from a verified manifest. Caller MUST have
        already validated the manifest (verify_directory_manifest); this
        method trusts it. Mutates in place so existing references — most
        importantly AuthService's `self._dir` — stay valid across an
        admin-driven /admin/attest or /admin/revoke."""
        self._by_key.clear()
        self._revoked.clear()
        self._absorb(m.attestations, m.revocations)
        self._manifest = m

    def resolve(self, pubkey: str) -> Optional[Attestation]:
        """Return the current attestation for a key, or None. Caller checks revocation/expiry."""
        return self._by_key.get(pubkey)

    def attested_keys(self) -> list[str]:
        """All pubkeys that have been attested at any point — regardless of
        current revocation status. Used by /ledger to enumerate historical
        recipients (a notice sent before a revocation is still 'owed' to
        them, per §2.3)."""
        return list(self._by_key.keys())

    def is_revoked(self, pubkey: str, as_of: Optional[str] = None) -> bool:
        """Spec §2.3: entries signed before revocation remain verifiable.

        With `as_of` omitted, answers 'is this key revoked NOW?' — used by the
        acceptance pipeline to bar new entries from a revoked author. With
        `as_of` set (e.g. the historical entry's created_at), answers whether
        the key was revoked at that moment.
        """
        r = self._revoked.get(pubkey)
        if r is None:
            return False
        if as_of is None:
            return True
        return as_of >= r.revoked_at

    @classmethod
    def from_manifest(cls, m: DirectoryManifest) -> "Directory":
        """Verify the manifest end-to-end then construct the in-memory view."""
        if not verify_directory_manifest(m):
            raise ValueError("directory manifest signature invalid")
        d = cls()
        d.update_from(m)
        return d
