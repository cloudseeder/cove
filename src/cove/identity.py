"""Identity & directory. Spec: server-hub-spec.md §2.

The organization root key (offline, NOT on the hub) signs attestations binding a
member public key to a directory identity. The directory manifest (attestations
+ revocations) is root-signed; the hub serves it but cannot forge it.

Seam discipline (§11): attestations carry `issuer`. Never hardcode a single root
in a way that blocks multi-root resolution later — verify_attestation must key
off `att.issuer`, not a global constant.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import crypto

_ZERO_PREV_MANIFEST = "sha256:" + "0" * 64


@dataclass
class Attestation:
    member_pubkey: str
    enc_pubkey: Optional[str]      # null in v1 (sign-only)
    display_name: str              # real name shown in the UI
    affiliation: str               # freeform org sub-grouping: lot, dept, team,
                                   # chapter, class, etc. Empty string is fine.
                                   # v0.3 rename from the HOA-shaped 'unit'.
    role: str                      # tier: "member" | "officer" | "board"
                                   # Drives throttle/quota (config.py TIERS),
                                   # NOT a job title — see `title` for that.
    title: Optional[str]           # human-readable role title — "President",
                                   # "VP Engineering", "Treasurer", null when
                                   # the person doesn't carry one.
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
    """The signed directory wire format (§2.3).

    Every manifest commits to its predecessor via `prev_manifest_hash` —
    the genesis manifest uses the zero sentinel ('sha256:000...0'). The
    chain serves two jobs at once: optimistic-concurrency control (a
    stale update is detectable as 'this prev_hash isn't the current
    head') and an append-only audit trail of admin actions (you can
    walk the chain back through every directory change).
    """
    org: str                        # root pubkey
    attestations: list[Attestation] = field(default_factory=list)
    revocations: list[Revocation] = field(default_factory=list)
    updated_at: str = ""
    prev_manifest_hash: str = _ZERO_PREV_MANIFEST
    sig: str = ""                   # root sig over canonical(manifest minus sig)


# ---- exceptions -------------------------------------------------------
class InvalidManifestSignatureError(ValueError):
    """Root signature doesn't verify against m.org."""


class StaleManifestError(ValueError):
    """Manifest's prev_manifest_hash doesn't match the current head — the
    admin tool built it on a stale base and a newer update has landed in
    between. Re-pull the current manifest and rebuild."""


class RevocationDroppedError(ValueError):
    """New manifest is missing a revocation from the prior one — a key
    that was tombstoned cannot be un-tombstoned by submitting a manifest
    'cleansed' of it. The admin tool must carry forward prior revocations."""


# ---- signing payloads (everything except `sig`) -------------------------
def _att_content(att: Attestation) -> dict:
    return {k: v for k, v in asdict(att).items() if k != "sig"}


def _manifest_content(m: DirectoryManifest) -> dict:
    return {
        "org": m.org,
        "attestations": [asdict(a) for a in m.attestations],
        "revocations": [asdict(r) for r in m.revocations],
        "updated_at": m.updated_at,
        "prev_manifest_hash": m.prev_manifest_hash,
    }


def hash_manifest(m: DirectoryManifest) -> str:
    """Content-and-sig hash. The next manifest's `prev_manifest_hash`
    points here, chaining the audit history and giving concurrency
    control in one mechanism."""
    body = {**_manifest_content(m), "sig": m.sig}
    return "sha256:" + crypto.sha256_hex(crypto.canonicalize(body))


def manifest_to_dict(m: DirectoryManifest) -> dict:
    """Wire/disk JSON form. Every signed field included so a round trip
    preserves what the sig covers."""
    return {
        "org": m.org,
        "attestations": [asdict(a) for a in m.attestations],
        "revocations": [asdict(r) for r in m.revocations],
        "updated_at": m.updated_at,
        "prev_manifest_hash": m.prev_manifest_hash,
        "sig": m.sig,
    }


def manifest_from_dict(d: dict) -> DirectoryManifest:
    """Inverse of manifest_to_dict. Strict on required org field; lenient
    defaults for optional ones, matching the dataclass."""
    atts = [Attestation(**a) for a in d.get("attestations", []) or []]
    revs = [Revocation(**r) for r in d.get("revocations", []) or []]
    return DirectoryManifest(
        org=d["org"], attestations=atts, revocations=revs,
        updated_at=d.get("updated_at", ""),
        prev_manifest_hash=d.get("prev_manifest_hash", _ZERO_PREV_MANIFEST),
        sig=d.get("sig", ""),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- attestation -------------------------------------------------------
def issue_attestation(root_private_hex: str, *, member_pubkey: str, display_name: str,
                      affiliation: str, role: str, issuer_pubkey: str,
                      title: Optional[str] = None,
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
        affiliation=affiliation,
        role=role,
        title=title,
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
                    updated_at: Optional[str] = None,
                    prev_manifest_hash: str = _ZERO_PREV_MANIFEST,
                    ) -> DirectoryManifest:
    """Build and sign a directory manifest with the ROOT key. Admin-tool only.

    `prev_manifest_hash` defaults to the genesis sentinel — pass the
    current head's hash (from `hash_manifest`) for any non-genesis update.
    The hub will reject an update whose prev hash doesn't match its current
    head (StaleManifestError, §2.3 concurrency control).
    """
    m = DirectoryManifest(
        org=org,
        attestations=list(attestations),
        revocations=list(revocations),
        updated_at=updated_at or _now(),
        prev_manifest_hash=prev_manifest_hash,
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
        # The audit chain: every manifest ever applied here, in order. Each
        # successive entry's `prev_manifest_hash` points at the prior one.
        self._history: list[DirectoryManifest] = []
        # When attached, every update_from also appends the manifest to
        # this JSONL file so admin actions survive a process restart.
        self._persist_path: Optional[Path] = None
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
        """Apply a manifest update with full validation:

          1. Root signature verifies (InvalidManifestSignatureError).
          2. Chain check: `m.prev_manifest_hash` matches `hash_manifest`
             of the current head — rejects a stale-base submission
             (StaleManifestError). Skipped on the genesis load (no
             current head yet).
          3. Revocation-superset: every prior `(pubkey, revoked_at)`
             tombstone must appear in the new manifest's revocations
             (RevocationDroppedError). Prevents accidental un-revocation
             via a manifest 'cleansed' of prior revocations.

        On success, mutates in place — existing references to this
        Directory (AuthService._dir, /ledger's directory, etc.) stay
        valid across the update. The full manifest is appended to
        the audit chain (manifest_history()).

        Note on §2.3 as-of-time semantics: revocations carry their
        own `revoked_at`. Replacing state does NOT change the
        `revoked_at` of a preserved revocation, so an entry signed
        before that timestamp is still 'not revoked as of then',
        and historical inclusion-verified entries from a now-revoked
        member remain valid.
        """
        if not verify_directory_manifest(m):
            raise InvalidManifestSignatureError("manifest signature invalid")

        if self._manifest is not None:
            expected = hash_manifest(self._manifest)
            if m.prev_manifest_hash != expected:
                raise StaleManifestError(
                    f"prev_manifest_hash {m.prev_manifest_hash} does not chain "
                    f"to current head {expected}"
                )
            prior = {(r.pubkey, r.revoked_at) for r in self._manifest.revocations}
            new = {(r.pubkey, r.revoked_at) for r in m.revocations}
            dropped = prior - new
            if dropped:
                raise RevocationDroppedError(
                    f"new manifest is missing {len(dropped)} prior revocation(s)"
                )

        self._by_key.clear()
        self._revoked.clear()
        self._absorb(m.attestations, m.revocations)
        self._manifest = m
        self._history.append(m)
        if self._persist_path is not None:
            with self._persist_path.open("a") as f:
                f.write(json.dumps(manifest_to_dict(m)) + "\n")

    def attach_persistence(self, path: Path) -> None:
        """Persist every applied manifest as JSONL at `path`. Flushes the
        current chain immediately so the on-disk state matches the
        in-memory state at the moment of attachment. Subsequent update_from
        calls append.

        Use load_chain() to read a chain back in; this method is the
        write side, separated so the production wiring can attach
        persistence to a freshly-constructed Directory (genesis case)
        or to one that loaded a chain (restart case) uniformly.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate-and-rewrite — atomic-ish via temp + rename so a crashed
        # write doesn't corrupt the chain.
        import os
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w") as f:
            for m in self._history:
                f.write(json.dumps(manifest_to_dict(m)) + "\n")
        os.replace(tmp, path)
        self._persist_path = path

    @classmethod
    def load_chain(cls, path: Path) -> "Directory":
        """Build a Directory by reading the JSONL chain at `path` and
        applying each manifest in order via update_from — which means
        every loaded manifest passes the same sig + chain + revocation-
        superset checks the live admin path does.

        If `path` doesn't exist, returns a fresh empty Directory. The
        caller is responsible for calling attach_persistence to start
        writing subsequent updates. Tampering with the on-disk chain is
        detected: a bad sig or broken chain raises during load."""
        d = cls()
        if path.exists():
            with path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    d.update_from(manifest_from_dict(json.loads(line)))
        return d

    def manifest_history(self) -> list[DirectoryManifest]:
        """Append-only audit trail of every manifest applied here, in order.
        Each successive entry chains to its predecessor via
        `prev_manifest_hash` — a governance dispute can walk this chain
        to see who attested or revoked whom, when, and under whose root
        signature. Returns a copy so callers can iterate safely."""
        return list(self._history)

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
        """Seed a fresh Directory from a manifest. The chain check is
        skipped (this IS the genesis from the in-memory view's perspective)
        but the signature still must verify."""
        d = cls()
        d.update_from(m)
        return d
