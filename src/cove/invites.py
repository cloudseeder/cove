"""Invite-code registry. v0.4.33 — admission gate for /pending.

Without an invite gate, POST /pending is wide open: any attacker who
knows the hub URL can flood the keymaster's queue with garbage
attestation requests. The keymaster can ignore them, but the cost is
real (audit fatigue, missed legit requests).

Invite codes are a binary structural gate: have a valid unused code,
get queued; don't, get rejected. This isn't spam SCORING (CLAUDE.md
non-negotiable #4 forbids that — origin is binary and proven, or
rejected); it's a key-shaped lock on the admission step.

How the loop works:
  1. Keymaster mints an invite via /admin/invites (root-signed).
     Code is a 16-byte random hex string, single-use, time-limited.
  2. Code is delivered out-of-band (same channel as the fingerprint
     verification — text, in-person, encrypted Signal, etc.).
  3. Member's app posts /pending with the code attached.
  4. Hub checks code valid + unused + unexpired; if so, registers
     pending and atomically marks the code consumed.
  5. Keymaster attests in AdminPanel as usual.

v0.5.1: durable across hub restart. Prior to v0.5.1 invites were
process-local — codes evaporated on restart, and every deploy silently
invalidated whatever the keymaster had already texted to prospective
members. A new pilot member seeing "the code doesn't work" a day after
receiving it is the exact friction the pilot doesn't need. Invites now
persist to SQLite (same file as EventStore + VaultStore, distinct
table); the constructor takes an optional db_path — in-memory only when
omitted (test convenience).

Time domain: wall-clock (time.time), not monotonic. Persisted expires_at
values must be comparable to the new process's clock; monotonic resets
to zero on restart and would falsely appear as "everything expires
immediately." Wall-clock NTP jumps are bounded to seconds — invites are
hours-to-days scoped, so drift is harmless.
"""
from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional


# Codes are random hex strings. 32 hex chars = 128 bits of entropy —
# more than enough for a single-use admission gate. Out-of-band delivery
# (text/Signal/paper) is the channel; the code isn't a long-term secret.
_CODE_BYTES = 16

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invites (
  code        TEXT PRIMARY KEY,
  created_at  REAL NOT NULL,
  expires_at  REAL NOT NULL,
  name_hint   TEXT,
  consumed_at REAL,
  revoked_at  REAL
);
"""


@dataclass(frozen=True)
class Invite:
    """A single-use invite. `name_hint` is optional metadata the keymaster
    can attach when minting ("for Carol's daughter") to help match the
    code to the eventual /pending submission. Status transitions:

        pending  → consumed  (someone POSTed /pending with it)
        pending  → revoked   (keymaster invalidated it before use)
        pending  → expired   (time passed; checked lazily, not stored)
    """
    code: str
    created_at: float          # wall-clock seconds since epoch (v0.5.1)
    expires_at: float          # same clock; expired when now > this
    name_hint: Optional[str]
    consumed_at: Optional[float]
    revoked_at: Optional[float]

    @property
    def is_active(self) -> bool:
        return self.consumed_at is None and self.revoked_at is None


class InviteRegistry:
    """Invite store, in-memory + optionally SQLite-backed. Thread-safe;
    the atomic mint→consume primitive is what prevents two parallel
    /pending POSTs from double-spending a single code.

    v0.5.1: pass `db_path` for persistence. When omitted the registry is
    pure-memory (fine for unit tests that don't need durability, but not
    fine for production — a hub restart silently invalidates every
    outstanding code). Row cleanup for long-expired / long-consumed
    rows happens lazily inside mint() so a busy hub doesn't need a
    background sweeper."""

    # Rows this old are dropped on next mint. Two weeks is comfortably
    # longer than any TTL the AdminPanel offers (max 7d) plus a grace
    # window for auditability.
    _CLEANUP_AGE_SECONDS = 14 * 24 * 3600

    def __init__(self,
                 db_path: Optional[str] = None,
                 time_fn=time.time,
                 code_factory=lambda: secrets.token_hex(_CODE_BYTES)) -> None:
        self._invites: dict[str, Invite] = {}
        self._lock = threading.Lock()
        self._now = time_fn
        self._mk_code = code_factory
        self._conn: Optional[sqlite3.Connection] = None
        if db_path is not None:
            self._conn = sqlite3.connect(
                db_path, check_same_thread=False, isolation_level=None,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._load_from_disk()

    def _load_from_disk(self) -> None:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT code, created_at, expires_at, name_hint,"
            " consumed_at, revoked_at FROM invites"
        ).fetchall()
        for code, created, expires, hint, consumed, revoked in rows:
            self._invites[code] = Invite(
                code=code,
                created_at=float(created),
                expires_at=float(expires),
                name_hint=hint,
                consumed_at=(float(consumed) if consumed is not None else None),
                revoked_at=(float(revoked) if revoked is not None else None),
            )

    def _persist(self, inv: Invite) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO invites"
            " (code, created_at, expires_at, name_hint, consumed_at, revoked_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (inv.code, inv.created_at, inv.expires_at, inv.name_hint,
             inv.consumed_at, inv.revoked_at),
        )

    def _cleanup_old_rows(self, now: float) -> None:
        """Drop rows that are long-consumed / long-revoked / long-expired.
        Called under _lock inside mint()."""
        cutoff = now - self._CLEANUP_AGE_SECONDS
        stale = [
            code for code, inv in self._invites.items()
            if (inv.consumed_at is not None and inv.consumed_at < cutoff)
            or (inv.revoked_at is not None and inv.revoked_at < cutoff)
            or (inv.expires_at < cutoff)
        ]
        for code in stale:
            del self._invites[code]
        if self._conn is not None and stale:
            self._conn.executemany(
                "DELETE FROM invites WHERE code = ?",
                [(c,) for c in stale],
            )

    def mint(self, ttl_seconds: int,
             name_hint: Optional[str] = None) -> Invite:
        """Create a fresh code. `ttl_seconds` is the validity window;
        the keymaster picks (1h / 24h / 7d) per code. Returns the full
        Invite — the caller surfaces the code + expires_at to the UI."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        now = self._now()
        with self._lock:
            self._cleanup_old_rows(now)
            # Re-roll on the (vanishingly unlikely) collision; never
            # return a code that already exists in the registry.
            for _ in range(8):
                code = self._mk_code()
                if code not in self._invites:
                    break
            else:
                raise RuntimeError("could not mint a unique invite code")
            inv = Invite(
                code=code,
                created_at=now,
                expires_at=now + ttl_seconds,
                name_hint=name_hint,
                consumed_at=None,
                revoked_at=None,
            )
            self._invites[code] = inv
            self._persist(inv)
            return inv

    def get(self, code: str) -> Optional[Invite]:
        """Returns the Invite or None. Active-but-expired invites still
        come back — caller checks the timestamp + is_active to decide
        whether to honor."""
        with self._lock:
            return self._invites.get(code)

    def consume(self, code: str) -> Invite:
        """Mark an invite consumed if it's currently honorable. Atomic
        check-and-set so two parallel /pending POSTs with the same code
        can't both win — exactly one returns the Invite, the other gets
        InviteUnusable.

        Raises InviteUnusable with a `reason` field describing why."""
        now = self._now()
        with self._lock:
            inv = self._invites.get(code)
            if inv is None:
                raise InviteUnusable("unknown")
            if inv.revoked_at is not None:
                raise InviteUnusable("revoked")
            if inv.consumed_at is not None:
                raise InviteUnusable("already_used")
            if now > inv.expires_at:
                raise InviteUnusable("expired")
            consumed = Invite(
                code=inv.code,
                created_at=inv.created_at,
                expires_at=inv.expires_at,
                name_hint=inv.name_hint,
                consumed_at=now,
                revoked_at=None,
            )
            self._invites[code] = consumed
            self._persist(consumed)
            return consumed

    def revoke(self, code: str) -> Invite:
        """Invalidate an unused invite. Idempotent on already-revoked.
        Raises InviteUnusable if the code was already consumed (you
        can't un-do an attestation by revoking the code that admitted
        it; the manifest is the source of truth)."""
        now = self._now()
        with self._lock:
            inv = self._invites.get(code)
            if inv is None:
                raise InviteUnusable("unknown")
            if inv.consumed_at is not None:
                raise InviteUnusable("already_used")
            if inv.revoked_at is not None:
                return inv  # idempotent
            revoked = Invite(
                code=inv.code,
                created_at=inv.created_at,
                expires_at=inv.expires_at,
                name_hint=inv.name_hint,
                consumed_at=None,
                revoked_at=now,
            )
            self._invites[code] = revoked
            self._persist(revoked)
            return revoked

    def list_active(self) -> list[Invite]:
        """All invites that haven't been consumed AND haven't been
        revoked AND haven't expired. Sorted by created_at (oldest first)
        so the UI shows them in mint-order."""
        now = self._now()
        with self._lock:
            out = [
                inv for inv in self._invites.values()
                if inv.is_active and now <= inv.expires_at
            ]
        out.sort(key=lambda inv: inv.created_at)
        return out


class InviteUnusable(Exception):
    """Raised by consume()/revoke() when the code can't be honored.
    `args[0]` is a short reason code ('unknown', 'expired',
    'already_used', 'revoked') — the API surfaces it as the 401 detail
    so the client UI can render a useful message without parsing."""
