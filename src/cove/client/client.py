"""Cove client. Holds member keypair + local state, talks to a hub.

Verification semantics live HERE — the full client-spec §5 chain runs on
every entry that goes through sync(). A UI surface (the Tauri app, the
agent MCP server, anything else) consumes VerifiedEntry objects and never
re-implements the verification math.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from .. import crypto
from ..entry import BlobRef, Entry, Receipt, sign_entry, verify_entry
from ..identity import (
    Attestation, Directory, DirectoryManifest,
    manifest_from_dict, verify_directory_manifest,
)
from ..translog import (
    InclusionProof, STH, verify_inclusion, verify_sth,
)


# ---- errors -----------------------------------------------------------
class ClientError(Exception):
    """Base for client-side failures the caller should handle distinctly
    from network-level surprises."""


class AuthenticationError(ClientError):
    """Auth challenge-response failed or session is missing/expired."""


class VerificationError(ClientError):
    """Some part of the §5 verification chain failed for an entry —
    signature, id, directory resolution, or inclusion proof. The caller
    MUST treat the entry as rejected; the entry is NOT silently dropped."""


# ---- data ------------------------------------------------------------
@dataclass
class VerifiedEntry:
    """An entry that has passed the full §5 verification chain. This is
    what a UI surface consumes — the verification IS done by the time the
    object exists.

    The 'ceremony reveal' the UX builds against:
      - role + display_name + verified_against_root: who, attested by whom
      - inclusion_position + sth.tree_size: where in the tamper-evident log
      - sig_summary: one-line human-readable chain
    """
    entry: Entry
    seq: int                              # per-thread seq the leaf hash commits to
    sth: STH                              # head this entry was verified UNDER
    inclusion_proof: InclusionProof
    attestation: Attestation              # author's directory record at verify time

    @property
    def role(self) -> str:
        return self.attestation.role

    @property
    def display_name(self) -> str:
        return self.attestation.display_name

    @property
    def sig_summary(self) -> str:
        """One-line render-ready summary of the verification chain. The
        Tauri 'tap the seal to reveal' interaction expands on this."""
        return (f"Signed by {self.display_name} ({self.role}) "
                f"→ verified against root {self.attestation.issuer[:8]}… "
                f"→ inclusion proof position {self.inclusion_proof.leaf_index} "
                f"of {self.sth.tree_size}")


@dataclass
class _Session:
    token: str
    expires_at: float
    pubkey: str


# ---- the client -------------------------------------------------------
class Client:
    """Cove client. Sync (slice 1) — WebSocket subscription, throttle
    backoff, storage persistence land in follow-ups."""

    def __init__(self, *, hub_url: str = "http://localhost:8000",
                 private_key: str, public_key: str,
                 http: Optional[httpx.Client] = None,
                 now: callable = time.time) -> None:
        self.hub_url = hub_url
        self.priv = private_key
        self.pub = public_key
        self._http = http or httpx.Client(base_url=hub_url, timeout=10.0)
        self._now = now
        self._session: Optional[_Session] = None
        # Verified manifest (Directory holds the chain + the as-of-time
        # is_revoked semantics §5 needs).
        self._directory: Optional[Directory] = None
        # Per-thread high-water seq — the `since` we send on the next sync.
        self._high_water: dict[str, int] = {}
        # Last STH we verified the hub's sig on — anchors consistency.
        self._last_sth: Optional[STH] = None

    # ---- key loading -------------------------------------------------
    @classmethod
    def from_keyfile(cls, *, hub_url: str, key_basename: str,
                     http: Optional[httpx.Client] = None) -> "Client":
        """Load a paired keyfile pair matching scripts/gen_keys.py output:
        `<basename>.priv` (0o600) and `<basename>.pub` (0o644)."""
        priv = Path(key_basename + ".priv").read_text().strip()
        pub = Path(key_basename + ".pub").read_text().strip()
        return cls(hub_url=hub_url, private_key=priv, public_key=pub, http=http)

    # ---- introspection -----------------------------------------------
    @property
    def authenticated(self) -> bool:
        return self._session is not None and self._session.expires_at > self._now()

    @property
    def session_token(self) -> Optional[str]:
        return self._session.token if self._session is not None else None

    @property
    def directory_cache(self) -> Optional[Directory]:
        return self._directory

    def high_water(self, thread: str) -> int:
        return self._high_water.get(thread, -1)

    # ---- auth (§5) ---------------------------------------------------
    def authenticate(self) -> str:
        """Challenge-response. Persists session token on the underlying
        httpx client so subsequent calls carry the Authorization header
        automatically. Returns the token."""
        r = self._http.post("/auth/challenge")
        self._raise_for_status(r, "auth/challenge")
        ch = r.json()
        sig = crypto.sign(self.priv, ch["nonce"].encode())
        r = self._http.post("/auth/verify", json={
            "pubkey": self.pub, "nonce": ch["nonce"], "sig": sig,
        })
        if r.status_code != 200:
            body = r.json() if r.content else {}
            raise AuthenticationError(body.get("reason", f"auth_verify_failed {r.status_code}"))
        body = r.json()
        self._session = _Session(
            token=body["token"],
            expires_at=float(body["expires_at"]),
            pubkey=body["pubkey"],
        )
        self._http.headers["Authorization"] = f"Bearer {body['token']}"
        return body["token"]

    # ---- directory (§2) ---------------------------------------------
    def fetch_directory(self) -> Directory:
        """Pull the manifest, verify the root sig + every contained
        attestation, return a Directory (also cached internally)."""
        self._require_auth()
        r = self._http.get("/directory")
        self._raise_for_status(r, "directory")
        manifest = manifest_from_dict(r.json())
        if not verify_directory_manifest(manifest):
            raise VerificationError("directory manifest signature invalid")
        self._directory = Directory.from_manifest(manifest)
        return self._directory

    # ---- STH (§6.4) --------------------------------------------------
    def fetch_sth(self) -> STH:
        """Pull the latest STH and verify the hub's signature on it
        before returning. The verified STH is cached as the consistency
        anchor for subsequent verification."""
        r = self._http.get("/sth")
        self._raise_for_status(r, "sth")
        sth = STH(**r.json())
        if not verify_sth(sth):
            raise VerificationError("STH signature invalid — pinned hub key check failed")
        self._last_sth = sth
        return sth

    # ---- sync (§7 + client-spec §4.1 + §5) --------------------------
    def sync(self, thread: str) -> list[VerifiedEntry]:
        """Delta-sync the thread from our high-water seq. For every
        returned entry, run the full §5 verification chain:
            1. recompute id from canonical content; check it matches.
            2. verify sig over canonical content against `author`.
            3. resolve author in the directory; reject if unattested.
            4. is_revoked(author, as_of=entry.created_at) must be False
               — §2.3 historical revocation semantics.
            5. inclusion proof verifies under the current STH.

        Any failure raises VerificationError without populating the
        high-water — clients re-sync after fixing the underlying issue.
        Returns the verified entries in seq order on success.
        """
        self._require_auth()
        if self._directory is None:
            self.fetch_directory()
        sth = self.fetch_sth()

        since = self.high_water(thread)
        r = self._http.get("/sync", params={"thread": thread, "since": since})
        self._raise_for_status(r, "sync")
        items = r.json()["entries"]

        verified: list[VerifiedEntry] = []
        for item in items:
            ev = _entry_from_dict(item["entry"])
            seq = int(item["seq"])
            verified.append(self._verify(ev, seq, sth))

        # Advance high-water only after every entry passed — partial
        # advancement on failure would silently swallow the rejected
        # entry on the next sync.
        if verified:
            self._high_water[thread] = max(self.high_water(thread),
                                           max(v.seq for v in verified))
        return verified

    def verify(self, entry: Entry, seq: int) -> VerifiedEntry:
        """Standalone verification of a single entry against the current
        head — used by the push side (when WS lands) to verify pushed
        entries without going through sync."""
        self._require_auth()
        if self._directory is None:
            self.fetch_directory()
        sth = self._last_sth or self.fetch_sth()
        return self._verify(entry, seq, sth)

    def _verify(self, ev: Entry, seq: int, _sth: STH) -> VerifiedEntry:
        # 1+2. id + sig (recomputes id, verifies sig over canonical content).
        if not verify_entry(ev):
            raise VerificationError(f"entry {ev.id} id/sig invalid")

        # 3. directory resolution.
        att = self._directory.resolve(ev.author)
        if att is None:
            raise VerificationError(f"author {ev.author} not attested")

        # 4. revocation as-of entry time. §2.3: entries signed BEFORE
        # revocation remain valid; entries signed AFTER are rejected.
        if self._directory.is_revoked(ev.author, as_of=ev.created_at):
            raise VerificationError(
                f"author {ev.author} was revoked as-of {ev.created_at}")

        # 5. inclusion proof + STH bundled atomically (v0.4.31). Eliminates
        # the race where proof.tree_size could drift past a separately-
        # fetched STH's tree_size when another entry landed between calls.
        # The _sth caller-passed parameter is ignored — kept for the
        # internal callsite signature only.
        proof, sth = self._fetch_inclusion_proof_and_sth(ev.id)
        if not verify_sth(sth):
            raise VerificationError("STH signature invalid")
        if not verify_inclusion(ev.id, seq, proof, sth):
            raise VerificationError(
                f"inclusion proof failed for {ev.id} under sth size={sth.tree_size}")

        return VerifiedEntry(entry=ev, seq=seq, sth=sth,
                             inclusion_proof=proof, attestation=att)

    def _fetch_inclusion_proof_and_sth(self, entry_id: str) -> tuple[InclusionProof, STH]:
        """v0.4.31: read /proof/inclusion's bundled response. Returns
        (proof, sth) from a single hub-side atomic snapshot."""
        r = self._http.get("/proof/inclusion", params={"entry": entry_id})
        if r.status_code != 200:
            raise VerificationError(
                f"no inclusion proof for {entry_id} (status {r.status_code})")
        body = r.json()
        sth_dict = body.pop("sth", None)
        if sth_dict is None:
            # Older hub fallback — fetch STH separately. Race window
            # exists but is small in single-writer scenarios.
            sth = self.fetch_sth()
            return InclusionProof(**body), sth
        return InclusionProof(**body), STH(**sth_dict)

    # ---- post (§3) ---------------------------------------------------
    def post(self, entry: Entry) -> int:
        """Sign the entry locally if not already signed, then POST. The
        author's private key never leaves this process — only the
        canonical signed bytes go on the wire. Returns the per-thread seq
        the hub assigned."""
        self._require_auth()
        if entry.id is None or entry.sig is None:
            entry = sign_entry(entry, self.priv)
        r = self._http.post("/entries", json=_entry_to_dict(entry))
        if r.status_code == 429:
            # Throttle — client-spec §3.1. Surface the structured body
            # so callers can implement backoff per scope.
            body = r.json() if r.content else {}
            raise ClientError(f"throttled: {body}")
        self._raise_for_status(r, "entries")
        return int(r.json()["seq"])

    def post_receipt(self, *, thread: str, high_water_seq: int,
                     observed_sth: STH,
                     created_at: Optional[str] = None) -> int:
        """Build, sign, and post a kind='receipt' entry. The recipient
        attests to having caught up to `high_water_seq` AND having
        observed `observed_sth` at that moment — §6.4.3 equivocation
        evidence in addition to the cumulative ack."""
        ev = Entry(
            thread=thread, author=self.pub, kind="receipt",
            created_at=created_at or _now_iso(),
            body="",
            receipt=Receipt(
                high_water_seq=high_water_seq,
                observed_sth_size=observed_sth.tree_size,
                observed_sth_root=observed_sth.root_hash,
            ),
        )
        return self.post(ev)

    # ---- internals ---------------------------------------------------
    def _require_auth(self) -> None:
        if not self.authenticated:
            raise AuthenticationError("not authenticated; call authenticate() first")

    @staticmethod
    def _raise_for_status(r: httpx.Response, route: str) -> None:
        if r.status_code >= 400:
            try:
                body = r.json()
            except Exception:
                body = {"raw": r.text[:200]}
            raise ClientError(f"/{route} returned {r.status_code}: {body}")


# ---- helpers (entry (de)serialization mirrors api._entry_*) ----------
_CONTENT_FIELDS = {"thread", "author", "kind", "created_at", "parents",
                   "body", "blobs", "supersedes", "receipt"}


def _entry_from_dict(d: dict) -> Entry:
    blobs = [BlobRef(**b) for b in d.get("blobs", []) or []]
    fields = {k: d[k] for k in _CONTENT_FIELDS if k in d}
    fields["blobs"] = blobs
    if fields.get("receipt") is not None:
        fields["receipt"] = Receipt(**fields["receipt"])
    ev = Entry(**fields)
    ev.id = d.get("id")
    ev.sig = d.get("sig")
    return ev


def _entry_to_dict(ev: Entry) -> dict:
    from dataclasses import asdict
    return asdict(ev)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
