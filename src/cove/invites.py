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

Process-local state, like throttle/pending — invites evaporate on
hub restart, which is acceptable because:
  - the keymaster mints fresh codes for outstanding invites that
    haven't been used yet,
  - codes don't grant durable trust (they gate ONE pending entry,
    nothing more),
  - persistence would require thinking about cleanup (expired-but-
    never-used rows), which we don't need yet.

If pilot scale ever demands durable invites (the keymaster doesn't
want to re-issue after a hub restart), this module is the right place
to add disk-backed storage; the surface stays the same.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional


# Codes are random hex strings. 32 hex chars = 128 bits of entropy —
# more than enough for a single-use admission gate. Out-of-band delivery
# (text/Signal/paper) is the channel; the code isn't a long-term secret.
_CODE_BYTES = 16


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
    created_at: float          # monotonic seconds since hub start
    expires_at: float          # same clock; expired when now > this
    name_hint: Optional[str]
    consumed_at: Optional[float]
    revoked_at: Optional[float]

    @property
    def is_active(self) -> bool:
        return self.consumed_at is None and self.revoked_at is None


class InviteRegistry:
    """In-memory invite store. Thread-safe; the atomic mint→consume
    primitive is what prevents two parallel /pending POSTs from
    double-spending a single code."""

    def __init__(self,
                 time_fn=time.monotonic,
                 code_factory=lambda: secrets.token_hex(_CODE_BYTES)) -> None:
        self._invites: dict[str, Invite] = {}
        self._lock = threading.Lock()
        self._now = time_fn
        self._mk_code = code_factory

    def mint(self, ttl_seconds: int,
             name_hint: Optional[str] = None) -> Invite:
        """Create a fresh code. `ttl_seconds` is the validity window;
        the keymaster picks (1h / 24h / 7d) per code. Returns the full
        Invite — the caller surfaces the code + expires_at to the UI."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        now = self._now()
        with self._lock:
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
