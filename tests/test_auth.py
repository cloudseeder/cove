"""Auth contract. Spec §5: no passwords, keypair is the credential,
challenge-response yields a short-lived session token bound to a non-revoked
attested key.

The pinned guarantees:
  - issued nonces are single-use; consumed on a successful verify
  - nonces expire by TTL — a captured-but-unused nonce becomes useless
  - signature is verified against the claimed pubkey over the issued nonce
  - directory check: unknown or revoked pubkeys are rejected at verify time,
    not at challenge time (so /auth/challenge doesn't leak directory
    membership to scanners)
  - session tokens carry a TTL; expired tokens resolve to None
  - revocation of a session token is immediate
"""
from __future__ import annotations

import pytest

from cove import crypto
from cove.auth import AuthError, AuthService
from cove.identity import Directory, Revocation, issue_attestation


# ---- fixtures ----------------------------------------------------------

class _Clock:
    def __init__(self, t: float = 1_000_000.0): self.t = t
    def now(self) -> float: return self.t
    def advance(self, dt: float) -> None: self.t += dt


@pytest.fixture
def clock(): return _Clock()


def _directory_with(member_pub: str, root_priv: str, root_pub: str,
                    *, revoked: bool = False) -> Directory:
    att = issue_attestation(
        root_priv, member_pubkey=member_pub, display_name="Alice",
        unit="U-1", role="member", issuer_pubkey=root_pub,
        issued_at="2026-01-01T00:00:00+00:00",
    )
    revs = [Revocation(pubkey=member_pub,
                       revoked_at="2026-02-01T00:00:00+00:00", reason="left")] if revoked else []
    return Directory(attestations=[att], revocations=revs)


@pytest.fixture
def auth(clock, root_keypair, keypair):
    _, root_pub = root_keypair
    root_priv, _ = root_keypair
    _, member_pub = keypair
    directory = _directory_with(member_pub, root_priv, root_pub)
    return AuthService(directory=directory, time_fn=clock.now,
                       nonce_ttl_s=120, session_ttl_s=3600)


def _sign_nonce(member_priv: str, nonce: str) -> str:
    return crypto.sign(member_priv, nonce.encode())


# ---- nonce issuance ----------------------------------------------------

def test_issue_challenge_returns_nonce_and_expiry(auth, clock):
    ch = auth.issue_challenge()
    assert len(ch.nonce) == 64        # 32 random bytes hex
    assert ch.expires_at == clock.now() + 120


def test_issued_nonces_are_distinct(auth):
    seen = {auth.issue_challenge().nonce for _ in range(20)}
    assert len(seen) == 20            # 32 random bytes — collisions are vanishingly rare


def test_challenge_does_not_require_pubkey(auth):
    """§5 design choice: /auth/challenge doesn't take a pubkey. A scanner
    can't probe directory membership through the challenge endpoint —
    membership is checked at verify time."""
    auth.issue_challenge()             # no kwargs, no leak


# ---- verify happy path --------------------------------------------------

def test_verify_returns_bound_session_token(auth, keypair):
    member_priv, member_pub = keypair
    ch = auth.issue_challenge()
    sess = auth.verify_and_issue_session(
        pubkey=member_pub, nonce=ch.nonce,
        sig=_sign_nonce(member_priv, ch.nonce),
    )
    assert sess.pubkey == member_pub
    assert len(sess.token) == 64
    assert auth.resolve_session(sess.token) == member_pub


# ---- verify error cases (each one is an AuthError) ---------------------

def test_verify_unknown_nonce_raises(auth, keypair):
    member_priv, member_pub = keypair
    with pytest.raises(AuthError):
        auth.verify_and_issue_session(
            pubkey=member_pub, nonce="00" * 32,    # never issued
            sig=_sign_nonce(member_priv, "00" * 32),
        )


def test_verify_expired_nonce_raises(auth, clock, keypair):
    member_priv, member_pub = keypair
    ch = auth.issue_challenge()
    clock.advance(121)                # past nonce_ttl_s=120
    with pytest.raises(AuthError):
        auth.verify_and_issue_session(
            pubkey=member_pub, nonce=ch.nonce,
            sig=_sign_nonce(member_priv, ch.nonce),
        )


def test_verify_bad_signature_raises(auth, keypair):
    member_priv, member_pub = keypair
    other_priv, _ = crypto.generate_keypair()
    ch = auth.issue_challenge()
    with pytest.raises(AuthError):
        auth.verify_and_issue_session(
            pubkey=member_pub, nonce=ch.nonce,
            sig=_sign_nonce(other_priv, ch.nonce),    # signed by wrong key
        )


def test_verify_unknown_pubkey_raises(auth):
    """Pubkey isn't in the directory — checked HERE, not at challenge."""
    rogue_priv, rogue_pub = crypto.generate_keypair()
    ch = auth.issue_challenge()
    with pytest.raises(AuthError):
        auth.verify_and_issue_session(
            pubkey=rogue_pub, nonce=ch.nonce,
            sig=_sign_nonce(rogue_priv, ch.nonce),
        )


def test_verify_revoked_pubkey_raises(clock, root_keypair, keypair):
    root_priv, root_pub = root_keypair
    member_priv, member_pub = keypair
    directory = _directory_with(member_pub, root_priv, root_pub, revoked=True)
    a = AuthService(directory=directory, time_fn=clock.now)
    ch = a.issue_challenge()
    with pytest.raises(AuthError):
        a.verify_and_issue_session(pubkey=member_pub, nonce=ch.nonce,
                                   sig=_sign_nonce(member_priv, ch.nonce))


# ---- nonce single-use ---------------------------------------------------

def test_nonce_cannot_be_reused_after_successful_verify(auth, keypair):
    """Replay defense: once consumed, a nonce is gone. A captured request
    cannot be replayed to get a second session."""
    member_priv, member_pub = keypair
    ch = auth.issue_challenge()
    sig = _sign_nonce(member_priv, ch.nonce)
    auth.verify_and_issue_session(pubkey=member_pub, nonce=ch.nonce, sig=sig)
    with pytest.raises(AuthError):
        auth.verify_and_issue_session(pubkey=member_pub, nonce=ch.nonce, sig=sig)


def test_nonce_survives_failed_verify_so_client_can_retry(auth, keypair):
    """A nonce isn't consumed on signature failure — otherwise a single
    typo would force a fresh round trip. (Brute-forcing Ed25519 isn't a
    real threat; TTL still bounds the window.)"""
    member_priv, member_pub = keypair
    other_priv, _ = crypto.generate_keypair()
    ch = auth.issue_challenge()
    with pytest.raises(AuthError):
        auth.verify_and_issue_session(pubkey=member_pub, nonce=ch.nonce,
                                      sig=_sign_nonce(other_priv, ch.nonce))
    # Retry with the right key — should still work.
    sess = auth.verify_and_issue_session(pubkey=member_pub, nonce=ch.nonce,
                                         sig=_sign_nonce(member_priv, ch.nonce))
    assert sess.pubkey == member_pub


# ---- session lifecycle --------------------------------------------------

def test_resolve_session_unknown_token_is_none(auth):
    assert auth.resolve_session("ff" * 32) is None


def test_session_expires_at_ttl(auth, clock, keypair):
    member_priv, member_pub = keypair
    ch = auth.issue_challenge()
    sess = auth.verify_and_issue_session(pubkey=member_pub, nonce=ch.nonce,
                                         sig=_sign_nonce(member_priv, ch.nonce))
    assert auth.resolve_session(sess.token) == member_pub
    clock.advance(3601)               # past session_ttl_s=3600
    assert auth.resolve_session(sess.token) is None


def test_revoke_session_invalidates_immediately(auth, keypair):
    member_priv, member_pub = keypair
    ch = auth.issue_challenge()
    sess = auth.verify_and_issue_session(pubkey=member_pub, nonce=ch.nonce,
                                         sig=_sign_nonce(member_priv, ch.nonce))
    auth.revoke_session(sess.token)
    assert auth.resolve_session(sess.token) is None


def test_revoking_one_session_does_not_affect_others(auth, keypair):
    member_priv, member_pub = keypair
    # Two sessions.
    ch1 = auth.issue_challenge()
    s1 = auth.verify_and_issue_session(pubkey=member_pub, nonce=ch1.nonce,
                                       sig=_sign_nonce(member_priv, ch1.nonce))
    ch2 = auth.issue_challenge()
    s2 = auth.verify_and_issue_session(pubkey=member_pub, nonce=ch2.nonce,
                                       sig=_sign_nonce(member_priv, ch2.nonce))
    auth.revoke_session(s1.token)
    assert auth.resolve_session(s1.token) is None
    assert auth.resolve_session(s2.token) == member_pub
