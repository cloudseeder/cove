"""Identity-vault registry. v0.4.76 — cross-platform key custody.

Each vault is an opaque, member-signed blob that pins one canonical
Ed25519 identity priv against N unlock methods (passphrase, Passkey PRF,
YubiKey/recovery-codes later). The blob is encrypted client-side; the hub
stores ciphertext and enforces envelope integrity — never plaintext.

Threat model this closes:
  Cross-ecosystem Passkey sync (iCloud ≠ Google Password Manager) forced
  a "one keypair per person per ecosystem" story. A user with a Mac + a
  Pixel ended up with two identities-per-hub. The vault lets the client
  synchronize ONE identity priv across every device that can unlock any
  of the vault's methods, with the hub storing only ciphertext + sig.

Invariants preserved (CLAUDE.md non-negotiables):
  #1: hub holds no member/root priv. Vault ciphertext is opaque; the
      wire schema forbids the hub from ever seeing plaintext.
  #5: no silent failures. Every rejection is a structured JSON body
      with a discriminated `reason` code.

Chain semantics mirror DirectoryManifest (identity.py):
  - Every vault carries `prev_vault_hash`; the chain-hash covers the
    full record INCLUDING sig, so tampering the wrap ciphertext without
    re-signing is detectable.
  - Genesis sentinel `sha256:000...0` for the first record per pubkey.
  - CAS on PUT: if `prev_vault_hash` doesn't match current head → 409.
    The response carries `head_hash` so the client can pull-merge-retry
    without a second GET.

Ownership rationale for a separate VaultStore (vs. bolting onto EventStore):
  Mirrors how blobs.py owns its own storage. Fewer entangled methods on
  EventStore, cleaner schema ownership, isolable durability story.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import Optional

from . import crypto


GENESIS_PREV = "sha256:" + "0" * 64

# A vault with more than a handful of unlock methods is a UX smell (nine
# passphrases means someone should be using a recovery-code system, not
# stuffing the slot list). The cap also bounds a runaway loop that would
# otherwise let a hostile client inflate the vault to megabytes of slots.
MAX_METHOD_SLOTS = 8
MAX_LABEL_LEN = 64

# Slot type discriminant. Adding a type here is the ONLY place shape
# validation gates it — the schema itself is polymorphic on `type`.
KNOWN_SLOT_TYPES = frozenset({"passphrase", "passkey"})

# Required fields per record. Field VALIDATION (b64 vs hex, length caps,
# etc.) is intentionally light — the hub doesn't decrypt, so most fields
# are opaque strings from its POV. What we DO enforce is presence + type.
_TOP_REQUIRED = frozenset({
    "pubkey", "version", "prev_vault_hash",
    "content_algo", "content_iv", "content_ciphertext",
    "method_slots", "updated_at", "sig",
})
_SLOT_COMMON_REQUIRED = frozenset({
    "id", "type", "algo",
    "wrap_iv", "wrap_ciphertext",
    "label", "created_at",
})
_SLOT_PASSPHRASE_REQUIRED = _SLOT_COMMON_REQUIRED | {"kdf_salt", "kdf_iterations"}
_SLOT_PASSKEY_REQUIRED = _SLOT_COMMON_REQUIRED | {
    "credential_id", "rp_id", "prf_salt_tag", "hkdf_info",
}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS vaults (
    pubkey     TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    hash       TEXT NOT NULL,           -- sha256:<hex>
    body       BLOB NOT NULL,           -- JCS bytes verbatim from the client
    size_bytes INTEGER NOT NULL,        -- mirrored so quota math doesn't reparse
    updated_at TEXT NOT NULL
);
"""


# ---- exceptions --------------------------------------------------------

class StaleVaultError(ValueError):
    """CAS check failed: `prev_vault_hash` doesn't match the current head.
    Carries `head_hash` so the API layer can echo it back to the client
    for a pull-merge-retry without a second GET round-trip."""

    def __init__(self, msg: str, head_hash: str) -> None:
        super().__init__(msg)
        self.head_hash = head_hash


class MalformedVaultError(ValueError):
    """Envelope shape or slot shape violates the schema. Raised by
    validate_shape; mapped to 400 at the API layer."""


# ---- data model --------------------------------------------------------

@dataclass(frozen=True)
class VaultHead:
    """Metadata about the current vault record for a pubkey. Cheap to
    fetch; doesn't carry the body (body is served fresh on GET)."""
    pubkey: str
    version: int
    hash: str
    updated_at: str
    size_bytes: int


# ---- pure helpers ------------------------------------------------------

def hash_vault(body: dict) -> str:
    """Content-and-sig hash of a vault record. `body` is the full record
    the client posted, INCLUDING `sig`. The next record's
    `prev_vault_hash` points at this value — chain covers sig so tampering
    the wrap without re-signing is detectable.

    Mirrors identity.hash_manifest at identity.py:204.
    """
    return "sha256:" + crypto.sha256_hex(crypto.canonicalize(body))


def _validate_slot(slot: dict) -> None:
    """Raise MalformedVaultError on a bad slot. Type-discriminated so
    each unlock method's required-field set is enforced independently."""
    if not isinstance(slot, dict):
        raise MalformedVaultError("method_slots entry must be a JSON object")
    kind = slot.get("type")
    if kind not in KNOWN_SLOT_TYPES:
        raise MalformedVaultError(f"unknown slot type: {kind!r}")
    required = (
        _SLOT_PASSPHRASE_REQUIRED if kind == "passphrase"
        else _SLOT_PASSKEY_REQUIRED
    )
    missing = required - set(slot.keys())
    if missing:
        raise MalformedVaultError(
            f"slot type={kind!r} missing fields: {sorted(missing)}"
        )
    label = slot.get("label")
    if not isinstance(label, str) or len(label) > MAX_LABEL_LEN:
        raise MalformedVaultError(
            f"slot label must be a string ≤ {MAX_LABEL_LEN} chars"
        )
    if kind == "passphrase":
        iters = slot.get("kdf_iterations")
        # Anything below 100k is well below the PBKDF2 hardening the
        # client uses (600k). Reject rather than silently accept a
        # weak-KDF slot that other clients would refuse to use.
        if not isinstance(iters, int) or iters < 100_000:
            raise MalformedVaultError("kdf_iterations must be int ≥ 100_000")


def validate_shape(body: dict) -> None:
    """Raise MalformedVaultError on any envelope problem. Pure — safe to
    call before touching storage or throttle state so a malformed PUT
    short-circuits cheaply."""
    if not isinstance(body, dict):
        raise MalformedVaultError("body must be a JSON object")
    missing = _TOP_REQUIRED - set(body.keys())
    if missing:
        raise MalformedVaultError(f"missing fields: {sorted(missing)}")
    if body["version"] != 1:
        raise MalformedVaultError(f"unsupported version: {body['version']!r}")
    slots = body["method_slots"]
    if not isinstance(slots, list):
        raise MalformedVaultError("method_slots must be a list")
    if not (1 <= len(slots) <= MAX_METHOD_SLOTS):
        raise MalformedVaultError(
            f"method_slots must have 1..{MAX_METHOD_SLOTS} entries"
        )
    for slot in slots:
        _validate_slot(slot)


# ---- storage -----------------------------------------------------------

class VaultStore:
    """SQLite-backed vault registry. One row per pubkey (latest only).

    Concurrency: a threading.Lock guards the head-read + INSERT OR REPLACE
    pair inside put_atomic. Multiple hub workers can call get/head freely
    (SQLite's WAL handles readers), but the CAS path serializes writes to
    guarantee that two racing PUTs for the same pubkey resolve as 1×200 +
    1×409. Prior-art: EventStore.append_atomic at store.py:123.
    """

    def __init__(self, path: str = "data/hub.db") -> None:
        self._path = path
        if path != ":memory:":
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        # check_same_thread=False + a module lock — same pattern as
        # EventStore. isolation_level=None puts sqlite3 in autocommit
        # mode; explicit transactions aren't needed for a single-row
        # upsert under our own lock.
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    def head(self, pubkey: str) -> Optional[VaultHead]:
        """Cheap metadata read. Returns None if no vault exists yet for
        this pubkey. Callers use this for the CAS chain check and for
        quota-delta accounting without paying the cost of reading `body`."""
        row = self._conn.execute(
            "SELECT pubkey, version, hash, updated_at, size_bytes"
            " FROM vaults WHERE pubkey = ?",
            (pubkey,),
        ).fetchone()
        if row is None:
            return None
        return VaultHead(
            pubkey=row[0], version=row[1], hash=row[2],
            updated_at=row[3], size_bytes=row[4],
        )

    def get(self, pubkey: str) -> Optional[bytes]:
        """Raw JCS body bytes for the GET path. Verbatim as the client
        posted — the endpoint b64-envelopes it for JSON transport."""
        row = self._conn.execute(
            "SELECT body FROM vaults WHERE pubkey = ?", (pubkey,),
        ).fetchone()
        return row[0] if row else None

    def put_atomic(self, *, pubkey: str, prev_hash: str, new_hash: str,
                   body: bytes, size_bytes: int, updated_at: str,
                   new_version: Optional[int] = None) -> VaultHead:
        """CAS write. Under the module lock, verifies prev_hash matches
        the current head (or GENESIS_PREV when no head exists), then
        INSERT OR REPLACE. Raises StaleVaultError on mismatch.

        `new_version` defaults to (existing.version + 1) or 1 for the
        first-ever record. Callers pass an explicit override only when
        the client-authored version needs to be preserved verbatim.
        """
        with self._lock:
            existing = self._read_head_locked(pubkey)
            expected_prev = existing.hash if existing else GENESIS_PREV
            if prev_hash != expected_prev:
                raise StaleVaultError(
                    f"prev_vault_hash {prev_hash} does not chain to head "
                    f"{expected_prev}",
                    head_hash=expected_prev,
                )
            version = new_version if new_version is not None else (
                (existing.version + 1) if existing else 1
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO vaults"
                " (pubkey, version, hash, body, size_bytes, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (pubkey, version, new_hash, body, size_bytes, updated_at),
            )
            return VaultHead(
                pubkey=pubkey, version=version, hash=new_hash,
                updated_at=updated_at, size_bytes=size_bytes,
            )

    def _read_head_locked(self, pubkey: str) -> Optional[VaultHead]:
        """Same as head() but assumes the caller already holds self._lock.
        Avoids a lock re-entrance in put_atomic."""
        row = self._conn.execute(
            "SELECT pubkey, version, hash, updated_at, size_bytes"
            " FROM vaults WHERE pubkey = ?",
            (pubkey,),
        ).fetchone()
        if row is None:
            return None
        return VaultHead(
            pubkey=row[0], version=row[1], hash=row[2],
            updated_at=row[3], size_bytes=row[4],
        )
