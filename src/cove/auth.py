"""Authentication. Spec §5.

No passwords. The keypair IS the credential. Challenge-response with a
short-lived nonce yields a bearer session token bound to a non-revoked
attested key.

Design choices worth noting:

  - /auth/challenge takes NO pubkey. A scanner cannot probe directory
    membership through the challenge endpoint — membership is checked at
    verify time only. The cost is the verifier returns one of several
    AuthError reasons (unknown nonce / bad sig / unknown identity /
    revoked); production may want to flatten those into a single
    'auth_failed' on the wire to avoid the same leak.

  - Nonces are single-use AND time-bound. Consumed on successful verify;
    left alive on signature failure so a single typo doesn't force a fresh
    round trip. TTL is the actual bound (Ed25519 brute force isn't a
    threat).

  - Session token is a 32-byte opaque random bearer. Sessions live in
    process memory — §9 throttle-state philosophy ('operational; transient').
    Restart wipes them; clients re-authenticate.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import crypto
from .identity import Directory


_NONCE_BYTES = 32
_TOKEN_BYTES = 32


class AuthError(Exception):
    """Any failed challenge-response. Production /auth/verify SHOULD flatten
    the reason to avoid leaking which check failed; tests pin behavior with
    distinct messages."""


@dataclass
class Challenge:
    nonce: str            # hex of 32 random bytes
    expires_at: float     # in the same epoch as time_fn()


@dataclass
class Session:
    token: str            # opaque bearer (hex of 32 random bytes)
    pubkey: str           # attested identity this session is bound to
    expires_at: float


class AuthService:
    def __init__(self, directory: Directory, *,
                 nonce_ttl_s: int = 120,
                 session_ttl_s: int = 3600,
                 time_fn: Callable[[], float] = time.time) -> None:
        self._dir = directory
        self._nonce_ttl = nonce_ttl_s
        self._session_ttl = session_ttl_s
        self._now = time_fn
        self._nonces: dict[str, float] = {}        # nonce -> expires_at
        self._sessions: dict[str, Session] = {}    # token -> Session

    def issue_challenge(self) -> Challenge:
        """Issue a fresh nonce. Pubkey-less: the client doesn't tell us who
        they are at this step. See module docstring for the rationale."""
        now = self._now()
        nonce = secrets.token_hex(_NONCE_BYTES)
        expires_at = now + self._nonce_ttl
        self._nonces[nonce] = expires_at
        return Challenge(nonce=nonce, expires_at=expires_at)

    def verify_and_issue_session(self, *, pubkey: str, nonce: str, sig: str) -> Session:
        """Verify the signed nonce and return a session bound to pubkey.

        Raises AuthError on any of: unknown/expired nonce, bad signature,
        unattested pubkey, revoked pubkey. The nonce is consumed only on a
        FULL success — a sig-failure leaves it alive so the legitimate
        client can retry.
        """
        now = self._now()

        # 1. nonce known and not expired
        expires_at = self._nonces.get(nonce)
        if expires_at is None:
            raise AuthError("unknown or consumed nonce")
        if expires_at < now:
            del self._nonces[nonce]
            raise AuthError("nonce expired")

        # 2. signature verifies against the claimed pubkey over the nonce
        if not crypto.verify(pubkey, sig, nonce.encode()):
            raise AuthError("signature invalid")

        # 3. directory check — attested AND not currently revoked
        if self._dir.resolve(pubkey) is None:
            raise AuthError("unknown identity")
        if self._dir.is_revoked(pubkey):
            raise AuthError("revoked identity")

        # 4. consume nonce, mint session
        del self._nonces[nonce]
        token = secrets.token_hex(_TOKEN_BYTES)
        sess = Session(token=token, pubkey=pubkey,
                       expires_at=now + self._session_ttl)
        self._sessions[token] = sess
        return sess

    def resolve_session(self, token: str) -> Optional[str]:
        """Return the pubkey bound to this token, or None if unknown/expired.
        Lazily evicts an expired session on lookup — cheaper than a sweeper
        at pilot scale."""
        sess = self._sessions.get(token)
        if sess is None:
            return None
        if sess.expires_at < self._now():
            del self._sessions[token]
            return None
        return sess.pubkey

    def revoke_session(self, token: str) -> None:
        """Immediate session invalidation. Logout, or admin-driven cutoff."""
        self._sessions.pop(token, None)
