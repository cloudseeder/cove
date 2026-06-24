"""Identity & directory contract. Spec §2.

Pins the trust root: an attestation is valid iff the ROOT signed it; a directory
manifest is valid iff the root signed the whole thing AND every contained
attestation is itself valid under its named issuer. Revocation tombstones a key
*as of* a moment in time (so historical entries signed before that moment
remain verifiable, per §2.3).

Seam discipline (§11): attestations carry `issuer`. Verification keys off
`att.issuer` — we deliberately do NOT pin a single root in this layer, so the
machinery is reusable for the future multi-root case without churn.
"""
from __future__ import annotations

import pytest

from cove import crypto
from cove.identity import (
    Attestation, Directory, DirectoryManifest, Revocation,
    issue_attestation, issue_directory,
    verify_attestation, verify_directory_manifest,
)


def _issue(root_priv, root_pub, member_pub, *, role="member",
           issued_at="2026-01-01T00:00:00+00:00", expires_at=None,
           display_name="Jane", unit="U-1") -> Attestation:
    return issue_attestation(
        root_priv,
        member_pubkey=member_pub,
        display_name=display_name,
        unit=unit,
        role=role,
        issuer_pubkey=root_pub,
        issued_at=issued_at,
        expires_at=expires_at,
    )


# ---- attestation issuance / verification --------------------------------

def test_issue_attestation_roundtrip(root_keypair, keypair):
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub, role="board")
    assert att.member_pubkey == member_pub
    assert att.issuer == root_pub
    assert att.role == "board"
    assert att.enc_pubkey is None       # §10.2: sign-only in v1
    assert att.sig                     # populated
    assert verify_attestation(att) is True


def test_tampered_attestation_fails_verify(root_keypair, keypair):
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub, role="member")
    att.role = "board"                  # privilege escalation attempt
    assert verify_attestation(att) is False


def test_attestation_signed_by_non_root_fails_verify(root_keypair, keypair):
    """`issuer` claims root_pub, but sig is from a different key."""
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    impostor_priv, _ = crypto.generate_keypair()
    att = issue_attestation(
        impostor_priv,                     # not the real root
        member_pubkey=member_pub,
        display_name="x", unit="x", role="board",
        issuer_pubkey=root_pub,            # claims to be from root anyway
    )
    assert verify_attestation(att) is False


def test_attestations_use_issuer_field_not_a_hardcoded_root(root_keypair, keypair):
    """Seam (§11): the verifier MUST key off att.issuer, not a global constant.

    We assert this behavioral: two distinct roots can each issue valid
    attestations, and both verify. Any code that hard-codes a single root
    would fail one of these.
    """
    root_a_priv, root_a_pub = root_keypair
    root_b_priv, root_b_pub = crypto.generate_keypair()
    _, member_pub = keypair
    a = _issue(root_a_priv, root_a_pub, member_pub)
    b = issue_attestation(root_b_priv,
                          member_pubkey=member_pub,
                          display_name="x", unit="x", role="member",
                          issuer_pubkey=root_b_pub)
    assert verify_attestation(a) is True
    assert verify_attestation(b) is True


# ---- Directory.resolve --------------------------------------------------

def test_resolve_returns_attestation_for_attested_key(root_keypair, keypair):
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub, role="officer")
    d = Directory(attestations=[att])
    got = d.resolve(member_pub)
    assert got is att
    assert got.role == "officer"


def test_resolve_returns_none_for_unknown_key(root_keypair):
    d = Directory()
    other_priv, other_pub = crypto.generate_keypair()
    assert d.resolve(other_pub) is None


def test_resolve_prefers_latest_attestation_after_reattest(root_keypair, keypair):
    """Re-attesting (e.g. after key rotation) replaces the previous attestation
    semantically; resolve returns the most recent by issued_at."""
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    old = _issue(root_priv, root_pub, member_pub,
                 role="member", issued_at="2026-01-01T00:00:00+00:00")
    new = _issue(root_priv, root_pub, member_pub,
                 role="board",  issued_at="2026-06-01T00:00:00+00:00")
    d = Directory(attestations=[old, new])
    assert d.resolve(member_pub).role == "board"
    # Order-independent.
    d2 = Directory(attestations=[new, old])
    assert d2.resolve(member_pub).role == "board"


# ---- Directory.is_revoked (the temporal one) ----------------------------

def test_is_revoked_false_for_never_revoked(root_keypair, keypair):
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub)
    d = Directory(attestations=[att])
    assert d.is_revoked(member_pub) is False


def test_is_revoked_true_now_when_revoked(root_keypair, keypair):
    """Without an as_of, is_revoked answers 'is this key currently revoked?' —
    used by the acceptance pipeline to bar NEW entries from a revoked key."""
    _, root_pub = root_keypair
    _, member_pub = keypair
    rev = Revocation(pubkey=member_pub, revoked_at="2026-03-01T00:00:00+00:00",
                     reason="sold unit")
    d = Directory(revocations=[rev])
    assert d.is_revoked(member_pub) is True


def test_is_revoked_respects_as_of_for_historical_entries(root_keypair, keypair):
    """§2.3: 'Events signed BEFORE a key's revocation remain verifiable.'

    The auditor passes the entry's created_at; is_revoked must answer
    against that moment, not 'now'.
    """
    _, member_pub = keypair
    rev = Revocation(pubkey=member_pub, revoked_at="2026-03-01T00:00:00+00:00",
                     reason="key compromise")
    d = Directory(revocations=[rev])
    # Entry written BEFORE revocation -> not revoked as of then.
    assert d.is_revoked(member_pub, as_of="2026-02-01T00:00:00+00:00") is False
    # Entry written AT revocation moment -> revoked.
    assert d.is_revoked(member_pub, as_of="2026-03-01T00:00:00+00:00") is True
    # Entry written AFTER -> revoked.
    assert d.is_revoked(member_pub, as_of="2026-04-01T00:00:00+00:00") is True


# ---- Directory manifest -------------------------------------------------

def test_manifest_roundtrip_verifies(root_keypair, keypair):
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub)
    m = issue_directory(root_priv, org=root_pub,
                        attestations=[att], revocations=[],
                        updated_at="2026-06-01T00:00:00+00:00")
    assert verify_directory_manifest(m) is True


def test_tampered_manifest_fails_verify(root_keypair, keypair):
    """Adding an entry to the manifest after signing must invalidate."""
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    forged_priv, forged_pub = crypto.generate_keypair()
    att = _issue(root_priv, root_pub, member_pub)
    m = issue_directory(root_priv, org=root_pub,
                        attestations=[att], revocations=[],
                        updated_at="2026-06-01T00:00:00+00:00")
    # Sneak in a forged attestation post-sign.
    m.attestations.append(Attestation(
        member_pubkey=forged_pub, enc_pubkey=None,
        display_name="Intruder", unit="x", role="board",
        issued_at="2026-06-02T00:00:00+00:00", expires_at=None,
        issuer=root_pub, sig="0" * 128,
    ))
    assert verify_directory_manifest(m) is False


def test_manifest_with_invalid_contained_attestation_fails(root_keypair, keypair):
    """A manifest's root sig is necessary but not sufficient — every contained
    attestation must also verify under its own issuer."""
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub)
    att.role = "board"                       # tamper BEFORE manifest is signed
    m = issue_directory(root_priv, org=root_pub,
                        attestations=[att], revocations=[],
                        updated_at="2026-06-01T00:00:00+00:00")
    # The OUTER root sig is valid (the tampered att was the input), but the
    # inner attestation sig no longer matches the tampered content.
    assert verify_directory_manifest(m) is False


def test_directory_from_manifest_loads_signed_state(root_keypair, keypair):
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    other_priv, other_pub = crypto.generate_keypair()
    att = _issue(root_priv, root_pub, member_pub, role="board")
    rev = Revocation(pubkey=other_pub, revoked_at="2026-02-01T00:00:00+00:00",
                     reason="left")
    m = issue_directory(root_priv, org=root_pub,
                        attestations=[att], revocations=[rev],
                        updated_at="2026-06-01T00:00:00+00:00")

    d = Directory.from_manifest(m)
    assert d.resolve(member_pub).role == "board"
    assert d.is_revoked(other_pub) is True


def test_directory_from_manifest_rejects_invalid_sig(root_keypair, keypair):
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub)
    m = issue_directory(root_priv, org=root_pub,
                        attestations=[att], revocations=[],
                        updated_at="2026-06-01T00:00:00+00:00")
    m.updated_at = "2030-01-01T00:00:00+00:00"   # forge after signing
    with pytest.raises(ValueError):
        Directory.from_manifest(m)
