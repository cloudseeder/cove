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
           display_name="Jane", affiliation="U-1") -> Attestation:
    return issue_attestation(
        root_priv,
        member_pubkey=member_pub,
        display_name=display_name,
        affiliation=affiliation,
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
        display_name="x", affiliation="x", role="board",
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
                          display_name="x", affiliation="x", role="member",
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


# ---- v0.4.13: default_thread soft hint ---------------------------------
#
# Optional field: when None, omitted from the canonical payload AND the
# wire-form dict so pre-v0.4.13 manifests round-trip byte-identical
# (their signatures still verify). When set, included in both.

def test_manifest_without_default_thread_omits_field_from_wire(root_keypair, keypair):
    """Backward-compat: an unset default_thread must NOT appear in JSON."""
    from cove.identity import manifest_to_dict
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub)
    m = issue_directory(root_priv, org=root_pub,
                        attestations=[att], revocations=[],
                        updated_at="2026-06-01T00:00:00+00:00")
    d = manifest_to_dict(m)
    assert "default_thread" not in d
    assert verify_directory_manifest(m) is True


def test_manifest_with_default_thread_round_trips(root_keypair, keypair):
    """A manifest carrying default_thread verifies, the field round-trips
    through manifest_from_dict, and the canonical payload changes — i.e.
    setting the field actually affects the signature."""
    from cove.identity import manifest_from_dict, manifest_to_dict
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub)
    m = issue_directory(root_priv, org=root_pub,
                        attestations=[att], revocations=[],
                        updated_at="2026-06-01T00:00:00+00:00",
                        default_thread="announcements")
    assert verify_directory_manifest(m) is True
    d = manifest_to_dict(m)
    assert d["default_thread"] == "announcements"
    # Round-trip through dict reconstructs an equivalent manifest that
    # still verifies under the original signature.
    m2 = manifest_from_dict(d)
    assert m2.default_thread == "announcements"
    assert verify_directory_manifest(m2) is True


def test_manifest_with_and_without_default_thread_have_different_sigs(root_keypair, keypair):
    """Sanity: setting the field is observable in the signature."""
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub)
    m_no = issue_directory(root_priv, org=root_pub,
                           attestations=[att], revocations=[],
                           updated_at="2026-06-01T00:00:00+00:00")
    m_yes = issue_directory(root_priv, org=root_pub,
                            attestations=[att], revocations=[],
                            updated_at="2026-06-01T00:00:00+00:00",
                            default_thread="announcements")
    assert m_no.sig != m_yes.sig


def test_manifest_tampered_default_thread_fails_verify(root_keypair, keypair):
    """Changing default_thread post-sign invalidates the manifest signature."""
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub)
    m = issue_directory(root_priv, org=root_pub,
                        attestations=[att], revocations=[],
                        updated_at="2026-06-01T00:00:00+00:00",
                        default_thread="announcements")
    assert verify_directory_manifest(m) is True
    m.default_thread = "sneaky-redirect"
    assert verify_directory_manifest(m) is False


# ---- capabilities_by_role (v0.4.25) ----------------------------------

def test_capabilities_for_role_default_grants_board_admin_and_archive():
    from cove.identity import capabilities_for_role
    # No manifest → use the hardcoded default mapping (board → admin + archive).
    assert capabilities_for_role("board", None) == {"admin", "archive"}
    assert capabilities_for_role("officer", None) == set()
    assert capabilities_for_role("member", None) == set()
    assert capabilities_for_role(None, None) == set()


def test_capabilities_for_role_honors_explicit_manifest_mapping(root_keypair, keypair):
    """When the manifest sets capabilities_by_role, that map is
    authoritative — a role missing from it has no caps, not the
    default. This is the protocol-level seam that lets a non-LWCCOA
    org rename 'board' to 'director' (or whatever) without code change."""
    from cove.identity import capabilities_for_role
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub, role="director")
    m = issue_directory(
        root_priv, org=root_pub, attestations=[att], revocations=[],
        updated_at="2026-06-01T00:00:00+00:00",
        capabilities_by_role={"director": ["admin", "archive"]},
    )
    assert capabilities_for_role("director", m) == {"admin", "archive"}
    # Roles not in the explicit map get NOTHING — default does not bleed in.
    assert capabilities_for_role("board", m) == set()


def test_manifest_with_capabilities_round_trips(root_keypair, keypair):
    from cove.identity import manifest_from_dict, manifest_to_dict
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub)
    m = issue_directory(
        root_priv, org=root_pub, attestations=[att], revocations=[],
        updated_at="2026-06-01T00:00:00+00:00",
        capabilities_by_role={"board": ["archive", "admin"]},  # input unsorted
    )
    assert verify_directory_manifest(m) is True
    d = manifest_to_dict(m)
    # Normalized on the way out (sorted dedupe per role) so the wire form
    # is stable across input orderings.
    assert d["capabilities_by_role"] == {"board": ["admin", "archive"]}
    m2 = manifest_from_dict(d)
    assert m2.capabilities_by_role == {"board": ["admin", "archive"]}
    assert verify_directory_manifest(m2) is True


def test_manifest_without_capabilities_omits_field_from_wire(root_keypair, keypair):
    """Byte-identical canonicalization for pre-v0.4.25 manifests: when
    the field is None it must NOT appear on the wire (so old verifiers
    that don't know the field can still re-canonicalize and verify)."""
    from cove.identity import manifest_to_dict
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub)
    m = issue_directory(
        root_priv, org=root_pub, attestations=[att], revocations=[],
        updated_at="2026-06-01T00:00:00+00:00",
    )
    d = manifest_to_dict(m)
    assert "capabilities_by_role" not in d


def test_manifest_with_and_without_capabilities_have_different_sigs(root_keypair, keypair):
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub)
    base_args = dict(
        org=root_pub, attestations=[att], revocations=[],
        updated_at="2026-06-01T00:00:00+00:00",
    )
    m_no = issue_directory(root_priv, **base_args)
    m_yes = issue_directory(
        root_priv, **base_args,
        capabilities_by_role={"board": ["admin"]},
    )
    assert m_no.sig != m_yes.sig


def test_directory_caller_capabilities_resolves_via_manifest(root_keypair, keypair):
    """End-to-end through Directory.caller_capabilities: an officer-role
    member with an officer-grants-admin manifest gets admin. Same
    member under the default mapping gets nothing."""
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub, role="officer")

    # Default mapping — officer has no caps.
    m_default = issue_directory(
        root_priv, org=root_pub, attestations=[att], revocations=[],
        updated_at="2026-06-01T00:00:00+00:00",
    )
    d = Directory.from_manifest(m_default)
    assert d.caller_capabilities(member_pub) == set()

    # Explicit mapping — officer gets admin.
    m_custom = issue_directory(
        root_priv, org=root_pub, attestations=[att], revocations=[],
        updated_at="2026-06-01T00:00:01+00:00",
        capabilities_by_role={"officer": ["admin"]},
    )
    d2 = Directory.from_manifest(m_custom)
    assert d2.caller_capabilities(member_pub) == {"admin"}


def test_tampered_capabilities_field_fails_verify(root_keypair, keypair):
    """Post-sign edit to the cap map invalidates the signature."""
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub)
    m = issue_directory(
        root_priv, org=root_pub, attestations=[att], revocations=[],
        updated_at="2026-06-01T00:00:00+00:00",
        capabilities_by_role={"board": ["admin"]},
    )
    assert verify_directory_manifest(m) is True
    m.capabilities_by_role = {"board": ["admin", "archive"]}
    assert verify_directory_manifest(m) is False


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
        display_name="Intruder", affiliation="x", role="board", title=None,
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


# ---- persistence (manifest chain JSONL) -----------------------------

def test_directory_persistence_survives_full_reload(tmp_path, root_keypair, keypair):
    """Genesis + admin update → close → reload from disk. The reloaded
    Directory must have the same state AND the same chain history.
    The chain on disk is re-walked through update_from on load, so
    every loaded manifest passes sig + chain + revocation-superset
    validation — a tampered file fails loudly at load time."""
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    extra_priv, extra_pub = crypto.generate_keypair()

    att_m = _issue(root_priv, root_pub, member_pub, role="member")
    seed = issue_directory(root_priv, org=root_pub,
                           attestations=[att_m], revocations=[],
                           updated_at="2026-06-01T00:00:00+00:00")

    path = tmp_path / "directory.jsonl"
    d1 = Directory.from_manifest(seed)
    d1.attach_persistence(path)

    # Admin update — adds extra as attested.
    from cove.identity import hash_manifest
    att_extra = _issue(root_priv, root_pub, extra_pub, role="member")
    next_m = issue_directory(
        root_priv, org=root_pub,
        attestations=[att_m, att_extra], revocations=[],
        updated_at="2026-06-15T00:00:00+00:00",
        prev_manifest_hash=hash_manifest(seed),
    )
    d1.update_from(next_m)

    # === reload ===
    d2 = Directory.load_chain(path)
    assert d2.resolve(member_pub) is not None
    assert d2.resolve(extra_pub) is not None
    assert len(d2.manifest_history()) == 2
    # Chain integrity verified on load via update_from's chain check.
    assert hash_manifest(d2.manifest_history()[-1]) == hash_manifest(next_m)


def test_load_chain_detects_tampered_manifest_on_disk(tmp_path, root_keypair, keypair):
    """If someone edits the JSONL chain (changes an attestation, etc.)
    update_from's sig check during load fails — the load raises rather
    than silently producing a Directory that doesn't match its own
    audit trail."""
    root_priv, root_pub = root_keypair
    _, member_pub = keypair
    att = _issue(root_priv, root_pub, member_pub)
    seed = issue_directory(root_priv, org=root_pub,
                           attestations=[att], revocations=[],
                           updated_at="2026-06-01T00:00:00+00:00")

    path = tmp_path / "tampered.jsonl"
    d = Directory.from_manifest(seed)
    d.attach_persistence(path)

    # Tamper with the on-disk record after-the-fact.
    raw = path.read_text()
    tampered = raw.replace('"member"', '"board"')   # privilege escalation
    assert tampered != raw
    path.write_text(tampered)

    with pytest.raises(Exception):
        Directory.load_chain(path)


def test_load_chain_on_missing_path_yields_empty_directory(tmp_path):
    """Bootstrap pattern: 'load_chain returns empty if no file; caller
    then issues the seed manifest and calls attach_persistence to
    start writing.'"""
    d = Directory.load_chain(tmp_path / "doesnt-exist.jsonl")
    assert d.manifest is None
    assert d.manifest_history() == []


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
