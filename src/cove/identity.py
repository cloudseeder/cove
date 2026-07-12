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


@dataclass(frozen=True)
class KeypairGroup:
    """Admin-defined logical grouping of member pubkeys under a display name.
    v0.4.64. Purely an ergonomic layer: audience selection surfaces let an
    admin pick a group to fan out to all its constituent pubkeys with one
    click, so "Kevin + Kevin's Phone" becomes a single choice. The audience
    on the wire stays a flat list of pubkeys — group membership is not a
    delivery-time concept.

    Signed inside the DirectoryManifest (root sig covers the group list),
    so all admins across all devices see the same groups after the manifest
    fetch. Editing a group is a manifest update, same flow as attestations.
    """
    name: str
    member_pubkeys: list[str]


@dataclass
class DirectoryManifest:
    """The signed directory wire format (§2.3).

    Every manifest commits to its predecessor via `prev_manifest_hash` —
    the genesis manifest uses the zero sentinel ('sha256:000...0'). The
    chain serves two jobs at once: optimistic-concurrency control (a
    stale update is detectable as 'this prev_hash isn't the current
    head') and an append-only audit trail of admin actions (you can
    walk the chain back through every directory change).

    `default_thread` (v0.4.13+) is an OPTIONAL soft hint to clients:
    "this is the thread a new member should land on after attestation."
    Backward-compat: when None it is OMITTED from the canonical payload
    so old manifests still verify with their existing signature.
    Present-and-set manifests canonicalize differently from absent ones,
    so a pre-v0.4.13 client will fail to verify a manifest where the
    field is set — orchestrate client-update before hub-reissue.

    `capabilities_by_role` (v0.4.25+) is an OPTIONAL org-defined map
    from role name → list of protocol capabilities. Drives the
    require_capability gate server-side and the AdminPanel + archive-
    button gates client-side. Same byte-identical-when-absent rule as
    default_thread — when None it is omitted from the canonical
    payload, and clients fall back to a hardcoded default
    (DEFAULT_CAPABILITIES_BY_ROLE: board has admin + archive, no other
    role has anything). The hardcoded default is intentionally
    LWCCOA-shaped — orgs with different role names set this explicitly.
    """
    org: str                        # root pubkey
    attestations: list[Attestation] = field(default_factory=list)
    revocations: list[Revocation] = field(default_factory=list)
    updated_at: str = ""
    prev_manifest_hash: str = _ZERO_PREV_MANIFEST
    default_thread: Optional[str] = None
    capabilities_by_role: Optional[dict[str, list[str]]] = None
    # v0.4.64: OPTIONAL admin-defined keypair groups (ergonomic shortcuts
    # for the audience picker — see KeypairGroup). Byte-identical-when-
    # absent canonicalization: pre-v0.4.64 manifests never had this field
    # and still verify with their existing signature.
    groups: Optional[list[KeypairGroup]] = None
    sig: str = ""                   # root sig over canonical(manifest minus sig)


# ---- capability constants (v0.4.25) ----------------------------------
# Closed set of protocol-defined capability strings. Manifests that
# reference unrecognized capability names are tolerated (forward-compat:
# a future client may know about "foo"; today's client just never
# matches on it). The default mapping below is what kicks in when a
# manifest has no capabilities_by_role field — it preserves the
# pre-v0.4.25 behavior where only board could see admin surfaces.
# v0.5.0: `manage_audience` is the gate for removing OTHER members from a
# thread's audience (self-leave and additive changes remain open to any
# current-audience member — see cove/audience.py). Officer gains a default
# capability for the first time; pre-v0.5.0 manifests with an override map
# that omits it will not grant it (per capabilities_for_role's precedence).
CAPABILITIES = frozenset({"admin", "archive", "manage_audience"})
DEFAULT_CAPABILITIES_BY_ROLE: dict[str, list[str]] = {
    "board": ["admin", "archive", "manage_audience"],
    "officer": ["manage_audience"],
}


def capabilities_for_role(role: Optional[str],
                          manifest: Optional[DirectoryManifest]) -> set[str]:
    """Resolve the capability set for a given role under a given manifest.

    `role` is the attestation's role string for the caller (None when
    the caller has no attestation — they get the empty set).
    `manifest` is the directory manifest in effect; pass None for the
    no-directory case (server returns empty set, falls through to a 403).

    Rule: if the manifest sets `capabilities_by_role`, that map is
    authoritative — a role missing from it has no capabilities. If the
    field is absent (typical for pre-v0.4.25 manifests, including the
    current LWCCOA pilot), DEFAULT_CAPABILITIES_BY_ROLE applies.
    """
    if role is None:
        return set()
    mapping = (manifest.capabilities_by_role if manifest else None) \
              or DEFAULT_CAPABILITIES_BY_ROLE
    return set(mapping.get(role, []))


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
    out = {
        "org": m.org,
        "attestations": [asdict(a) for a in m.attestations],
        "revocations": [asdict(r) for r in m.revocations],
        "updated_at": m.updated_at,
        "prev_manifest_hash": m.prev_manifest_hash,
    }
    # Conditional inclusion preserves byte-identical canonicalization for
    # pre-v0.4.13 manifests that never had this field. See DirectoryManifest
    # docstring for the orchestration note.
    if m.default_thread is not None:
        out["default_thread"] = m.default_thread
    if m.capabilities_by_role is not None:
        # JCS sorts object keys but not array values — normalize each
        # role's cap list (sorted + deduped) so the canonical bytes are
        # determined by the SET of (role, cap) pairs, not by ordering
        # noise from whatever produced the dict.
        out["capabilities_by_role"] = {
            role: sorted(set(caps))
            for role, caps in m.capabilities_by_role.items()
        }
    if m.groups is not None:
        # v0.4.64: same normalization rule as capabilities_by_role.
        # Per-group: dedupe + sort pubkeys so canonical bytes are the SET
        # of pubkeys, not the order the admin panel happened to submit.
        # Cross-group: sort by name so the array's order is deterministic
        # (JCS sorts object keys but preserves array order).
        out["groups"] = [
            {"name": g.name, "member_pubkeys": sorted(set(g.member_pubkeys))}
            for g in sorted(m.groups, key=lambda g: g.name)
        ]
    return out


def hash_manifest(m: DirectoryManifest) -> str:
    """Content-and-sig hash. The next manifest's `prev_manifest_hash`
    points here, chaining the audit history and giving concurrency
    control in one mechanism."""
    body = {**_manifest_content(m), "sig": m.sig}
    return "sha256:" + crypto.sha256_hex(crypto.canonicalize(body))


def manifest_to_dict(m: DirectoryManifest) -> dict:
    """Wire/disk JSON form. Every signed field included so a round trip
    preserves what the sig covers. `default_thread` and
    `capabilities_by_role` are omitted when None to keep the byte-
    identical round-trip property for older manifests."""
    out = {
        "org": m.org,
        "attestations": [asdict(a) for a in m.attestations],
        "revocations": [asdict(r) for r in m.revocations],
        "updated_at": m.updated_at,
        "prev_manifest_hash": m.prev_manifest_hash,
        "sig": m.sig,
    }
    if m.default_thread is not None:
        out["default_thread"] = m.default_thread
    if m.capabilities_by_role is not None:
        out["capabilities_by_role"] = {
            role: sorted(set(caps))
            for role, caps in m.capabilities_by_role.items()
        }
    if m.groups is not None:
        out["groups"] = [
            {"name": g.name, "member_pubkeys": sorted(set(g.member_pubkeys))}
            for g in sorted(m.groups, key=lambda g: g.name)
        ]
    return out


def manifest_from_dict(d: dict) -> DirectoryManifest:
    """Inverse of manifest_to_dict. Strict on required org field; lenient
    defaults for optional ones, matching the dataclass."""
    atts = [Attestation(**a) for a in d.get("attestations", []) or []]
    revs = [Revocation(**r) for r in d.get("revocations", []) or []]
    caps_raw = d.get("capabilities_by_role")
    caps = None
    if isinstance(caps_raw, dict):
        # Normalize so a hand-edited / tool-produced manifest round-trips
        # through verify(); the canonical form is sorted-deduped per role.
        caps = {role: sorted(set(v)) for role, v in caps_raw.items()
                if isinstance(role, str) and isinstance(v, list)}
    groups_raw = d.get("groups")
    groups: Optional[list[KeypairGroup]] = None
    if isinstance(groups_raw, list):
        # v0.4.64. Same normalization discipline as caps: dedupe/sort
        # pubkeys per group; the canonical form the sig covers is set-of-
        # pubkeys, not entry order.
        groups = [
            KeypairGroup(
                name=g["name"],
                member_pubkeys=sorted(set(g.get("member_pubkeys", []) or [])),
            )
            for g in groups_raw
            if isinstance(g, dict) and isinstance(g.get("name"), str)
        ]
    return DirectoryManifest(
        org=d["org"], attestations=atts, revocations=revs,
        updated_at=d.get("updated_at", ""),
        prev_manifest_hash=d.get("prev_manifest_hash", _ZERO_PREV_MANIFEST),
        default_thread=d.get("default_thread"),
        capabilities_by_role=caps,
        groups=groups,
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
                    default_thread: Optional[str] = None,
                    capabilities_by_role: Optional[dict[str, list[str]]] = None,
                    groups: Optional[list[KeypairGroup]] = None,
                    ) -> DirectoryManifest:
    """Build and sign a directory manifest with the ROOT key. Admin-tool only.

    `prev_manifest_hash` defaults to the genesis sentinel — pass the
    current head's hash (from `hash_manifest`) for any non-genesis update.
    The hub will reject an update whose prev hash doesn't match its current
    head (StaleManifestError, §2.3 concurrency control).

    `default_thread` (v0.4.13+) is the soft hint clients should land a
    new member on after attestation. Omit (None) to keep canonicalization
    byte-identical to pre-v0.4.13 manifests.

    `capabilities_by_role` (v0.4.25+) is the org-defined role → caps map.
    Omit (None) to keep canonicalization byte-identical to pre-v0.4.25
    manifests; the hub + clients fall back to DEFAULT_CAPABILITIES_BY_ROLE
    in that case.
    """
    m = DirectoryManifest(
        org=org,
        attestations=list(attestations),
        revocations=list(revocations),
        updated_at=updated_at or _now(),
        prev_manifest_hash=prev_manifest_hash,
        default_thread=default_thread,
        capabilities_by_role=capabilities_by_role,
        groups=list(groups) if groups is not None else None,
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

    def caller_capabilities(self, pubkey: str) -> set[str]:
        """v0.4.25: capability set for the holder of `pubkey` under the
        current manifest. Returns the empty set for an unattested or
        revoked-as-of-now caller. Backed by capabilities_for_role +
        the manifest's optional capabilities_by_role map; falls back
        to DEFAULT_CAPABILITIES_BY_ROLE when the manifest omits it."""
        att = self._by_key.get(pubkey)
        if att is None:
            return set()
        return capabilities_for_role(att.role, self._manifest)

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
