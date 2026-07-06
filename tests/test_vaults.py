"""Identity-vault HTTP wire contract. v0.4.76.

Pins the vault storage endpoints:
  GET  /vault/{pubkey}   — public; opaque ciphertext, returns 404 or the body
  PUT  /vault/{pubkey}   — auth'd; member-signed, chained via prev_vault_hash

The hub never decrypts. All ciphertext blobs in these tests are random
b64 fill — the hub's job is envelope validation (shape, sig, chain, quota),
not content. AES-GCM decryption is exercised on the client side.
"""
from __future__ import annotations

import base64
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from cove import crypto
from cove.identity import (
    hash_manifest, issue_attestation, issue_directory,
)
from cove.vaults import GENESIS_PREV, hash_vault


# ---- helpers -----------------------------------------------------------

def _b64(nbytes: int) -> str:
    """URL-safe b64 of `nbytes` random bytes, no padding — matches what
    the client emits from crypto.getRandomValues."""
    raw = secrets.token_bytes(nbytes)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _slot(kind: str = "passphrase", *, label: str = "Backup") -> dict:
    """Build a shape-valid slot with random ciphertext filler. The hub
    doesn't decrypt, so the wrap ciphertext can be any 48-byte b64 blob."""
    common = {
        "id": secrets.token_hex(8),
        "wrap_iv": _b64(12),
        "wrap_ciphertext": _b64(48),
        "label": label,
        "created_at": "2026-07-05T12:00:00+00:00",
    }
    if kind == "passphrase":
        return {
            **common,
            "type": "passphrase",
            "algo": "PBKDF2-SHA256-AES-GCM-256-v1",
            "kdf_salt": _b64(16),
            "kdf_iterations": 600_000,
        }
    if kind == "passkey":
        return {
            **common,
            "type": "passkey",
            "algo": "PRF-HKDF-AES-GCM-256-v1",
            "credential_id": _b64(16),
            "rp_id": "cove.oap.dev",
            "prf_salt_tag": "cove-vault-kek-v1",
            "hkdf_info": "cove-vault-kek-v1",
        }
    raise ValueError(f"unknown slot kind: {kind}")


def _mint_vault(*, priv: str, pub: str,
                prev: str = GENESIS_PREV,
                updated_at: str = "2026-07-05T12:00:00+00:00",
                slots: list[dict] | None = None) -> dict:
    """Build a signed vault dict ready to PUT. The content_ciphertext is
    filler — the hub doesn't decrypt. Sig covers canonicalize(record - sig)."""
    body = {
        "pubkey": pub,
        "version": 1,
        "prev_vault_hash": prev,
        "content_algo": "AES-GCM-256-v1",
        "content_iv": _b64(12),
        "content_ciphertext": _b64(96),
        "method_slots": slots if slots is not None else [_slot("passphrase")],
        "updated_at": updated_at,
    }
    sig = crypto.sign(priv, crypto.canonicalize(body))
    body["sig"] = sig
    return body


def _authed_client_for(hub, member_priv: str, member_pub: str) -> TestClient:
    """Spin up a TestClient auth'd as a specific member (must be an attested,
    non-revoked pubkey in hub['directory'])."""
    client = TestClient(hub["app"])
    ch = client.post("/auth/challenge").json()
    sig = crypto.sign(member_priv, ch["nonce"].encode())
    sess = client.post("/auth/verify", json={
        "pubkey": member_pub, "nonce": ch["nonce"], "sig": sig,
    }).json()
    client.headers["Authorization"] = f"Bearer {sess['token']}"
    return client


def _attest_new(hub, *, role: str, pub: str, display_name: str = "Test",
                affiliation: str = "U-X",
                issued_at: str = "2026-06-01T00:00:00+00:00",
                updated_at: str = "2026-06-15T00:00:00+00:00") -> None:
    """Append a fresh attestation via /admin/attest. Local copy of
    test_pending_api._attest_new since `tests` isn't a package."""
    current = hub["directory"].manifest
    new_att = issue_attestation(
        hub["root_priv"], member_pubkey=pub, display_name=display_name,
        affiliation=affiliation, role=role, title=None,
        issuer_pubkey=hub["root_pub"], issued_at=issued_at,
    )
    new_manifest = issue_directory(
        hub["root_priv"], org=hub["root_pub"],
        attestations=list(current.attestations) + [new_att],
        revocations=list(current.revocations),
        updated_at=updated_at,
        prev_manifest_hash=hash_manifest(current),
    )
    from dataclasses import asdict
    manifest_dict = {
        "org": new_manifest.org,
        "attestations": [asdict(a) for a in new_manifest.attestations],
        "revocations": [asdict(r) for r in new_manifest.revocations],
        "updated_at": new_manifest.updated_at,
        "prev_manifest_hash": new_manifest.prev_manifest_hash,
        "sig": new_manifest.sig,
    }
    r = hub["client"].post(
        "/admin/attest", json={"manifest": manifest_dict})
    assert r.status_code == 200, r.text


def _forge_session_for(hub, pubkey: str) -> TestClient:
    """Bypass /auth/verify and stamp a session onto the internal registry.
    Used only for tests that need the PUT endpoint to reach its own
    membership check with a caller pubkey that /auth/verify would refuse
    (revoked, unattested). Not a security hole — production auth still
    calls verify_and_issue_session; the tests are just skipping past
    that gate to unit-test what's downstream."""
    from cove.auth import Session
    token = secrets.token_hex(32)
    # AuthService uses time.time() (wall clock), not time.monotonic. Match
    # its clock so resolve_session doesn't mark the forged session expired.
    hub["auth"]._sessions[token] = Session(  # noqa: SLF001
        token=token, pubkey=pubkey,
        expires_at=time.time() + 3600,
    )
    client = TestClient(hub["app"])
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---- GET ---------------------------------------------------------------

def test_vault_get_empty_returns_404(hub):
    unauth = TestClient(hub["app"])
    r = unauth.get(f"/vault/{hub['member_pub']}")
    assert r.status_code == 404
    assert r.json()["error"] == "vault_not_found"


# ---- PUT round-trip ----------------------------------------------------

def test_vault_put_get_round_trip(hub):
    v = _mint_vault(priv=hub["member_priv"], pub=hub["member_pub"])
    r = hub["client"].put(f"/vault/{hub['member_pub']}", json=v)
    assert r.status_code == 200, r.text
    put_meta = r.json()
    assert put_meta["pubkey"] == hub["member_pub"]
    assert put_meta["version"] == 1
    assert put_meta["hash"] == hash_vault(v)

    # GET is PUBLIC. Body comes back as b64url so JSON transport is clean.
    unauth = TestClient(hub["app"])
    got = unauth.get(f"/vault/{hub['member_pub']}").json()
    assert got["hash"] == put_meta["hash"]
    assert got["updated_at"] == v["updated_at"]
    body_bytes = base64.urlsafe_b64decode(got["body"] + "==")
    # Bytes are the JCS canonicalization of the record the client posted.
    assert body_bytes == crypto.canonicalize(v)


# ---- Access-control checks --------------------------------------------

def test_vault_put_rejects_wrong_caller(hub):
    """Session A cannot write to /vault/{B}. Prevents a malicious member
    from bricking another member's vault-slot list."""
    # Attest a second member so we have two valid identities.
    other_priv, other_pub = crypto.generate_keypair()
    _attest_new(hub, role="member", pub=other_pub,
                display_name="Second", affiliation="U-9",
                updated_at="2026-07-01T00:00:00+00:00")

    # hub['client'] is signed in as member A; try to write B's vault.
    v = _mint_vault(priv=other_priv, pub=other_pub)
    r = hub["client"].put(f"/vault/{other_pub}", json=v)
    assert r.status_code == 403
    assert r.json()["reason"] == "pubkey_mismatch"


def test_vault_put_rejects_pubkey_url_mismatch(hub):
    """Body's `pubkey` field must equal the URL {pubkey}."""
    v = _mint_vault(priv=hub["member_priv"], pub=hub["member_pub"])
    # Corrupt the body's pubkey to something else attested.
    _, other_pub = crypto.generate_keypair()
    _attest_new(hub, role="member", pub=other_pub, display_name="Third",
                affiliation="U-7", updated_at="2026-07-02T00:00:00+00:00")
    v["pubkey"] = other_pub
    # Re-sign so the sig verifies against member_priv over the tampered body,
    # to prove the pubkey-check catches it BEFORE the sig check.
    unsigned = {k: v[k] for k in v if k != "sig"}
    v["sig"] = crypto.sign(hub["member_priv"], crypto.canonicalize(unsigned))

    r = hub["client"].put(f"/vault/{hub['member_pub']}", json=v)
    assert r.status_code == 400
    assert r.json()["reason"] == "pubkey_url_mismatch"


def test_vault_put_rejects_bad_sig(hub):
    v = _mint_vault(priv=hub["member_priv"], pub=hub["member_pub"])
    v["sig"] = "ff" * 64
    r = hub["client"].put(f"/vault/{hub['member_pub']}", json=v)
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_signature"


def test_vault_put_rejects_non_member(hub):
    """A caller whose pubkey is revoked (or was never attested) can't
    write a vault. Prevents non-members from filling hub storage."""
    # Use the fixture's revoked pubkey. Auth would refuse, so forge a
    # session token to reach the membership check.
    revoked_priv = None  # we don't have priv for revoked; use a fresh unattested key
    unattested_priv, unattested_pub = crypto.generate_keypair()
    client = _forge_session_for(hub, unattested_pub)

    v = _mint_vault(priv=unattested_priv, pub=unattested_pub)
    r = client.put(f"/vault/{unattested_pub}", json=v)
    assert r.status_code == 403
    assert r.json()["reason"] == "not_a_member"


# ---- Chain / CAS -------------------------------------------------------

def test_vault_put_rejects_stale_prev_hash(hub):
    v1 = _mint_vault(priv=hub["member_priv"], pub=hub["member_pub"])
    r1 = hub["client"].put(f"/vault/{hub['member_pub']}", json=v1)
    assert r1.status_code == 200
    first_hash = r1.json()["hash"]

    # Second PUT with GENESIS_PREV would be stale (we already have a head).
    v_bad = _mint_vault(priv=hub["member_priv"], pub=hub["member_pub"],
                        prev=GENESIS_PREV,
                        updated_at="2026-07-05T12:01:00+00:00")
    r2 = hub["client"].put(f"/vault/{hub['member_pub']}", json=v_bad)
    assert r2.status_code == 409
    body = r2.json()
    assert body["reason"] == "stale_prev_hash"
    assert body["head_hash"] == first_hash

    # Retry with the correct chain succeeds.
    v_good = _mint_vault(priv=hub["member_priv"], pub=hub["member_pub"],
                         prev=first_hash,
                         updated_at="2026-07-05T12:02:00+00:00")
    r3 = hub["client"].put(f"/vault/{hub['member_pub']}", json=v_good)
    assert r3.status_code == 200
    assert r3.json()["version"] == 2


def test_vault_cas_two_concurrent_puts_only_one_wins(hub, monkeypatch):
    """Two parallel PUTs racing on the same prev_vault_hash must result
    in exactly one 200 + one 409. Mirrors test_pipeline_atomicity.py's
    monkeypatch-and-race pattern."""
    from cove import vaults as vaults_mod

    barrier = threading.Barrier(2)
    original_put = vaults_mod.VaultStore.put_atomic

    def racy_put(self, *args, **kwargs):
        # Force both threads to reach the critical section at the same
        # time. The lock inside put_atomic then serializes them.
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_put(self, *args, **kwargs)

    monkeypatch.setattr(vaults_mod.VaultStore, "put_atomic", racy_put)

    v_a = _mint_vault(priv=hub["member_priv"], pub=hub["member_pub"])
    v_b = _mint_vault(priv=hub["member_priv"], pub=hub["member_pub"],
                      updated_at="2026-07-05T12:00:01+00:00")

    def do_put(body: dict) -> int:
        # Each thread gets its own TestClient sharing the same app —
        # httpx is not thread-safe on a single client instance.
        c = _authed_client_for(hub, hub["member_priv"], hub["member_pub"])
        return c.put(f"/vault/{hub['member_pub']}", json=body).status_code

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(do_put, v_a)
        f2 = ex.submit(do_put, v_b)
        codes = sorted([f1.result(), f2.result()])
    assert codes == [200, 409]


# ---- Size + shape ------------------------------------------------------

def test_vault_put_rejects_too_large(hub):
    """A vault exceeding max_vault_body_bytes must be rejected without
    burning CAS state."""
    # Stuff the label field with junk until the canonical body busts the
    # default 64 KiB cap. label validation happens at shape-check time,
    # so use a MAX_LABEL_LEN-compliant label but many slots.
    huge = _mint_vault(
        priv=hub["member_priv"], pub=hub["member_pub"],
        slots=[_slot("passphrase", label="A" * 60) for _ in range(2)]
        + [dict(_slot("passphrase"),
                # Oversize the wrap_ciphertext blob — hub doesn't cap the
                # per-field length (only overall body length), so we can
                # inflate here to trigger the 413.
                wrap_ciphertext=_b64(80_000))],
    )
    r = hub["client"].put(f"/vault/{hub['member_pub']}", json=huge)
    assert r.status_code == 413
    assert r.json()["reason"] == "too_large"


def test_vault_put_shape_validation(hub):
    v = _mint_vault(priv=hub["member_priv"], pub=hub["member_pub"])

    # Missing required top-level field.
    bad_missing = {k: v[k] for k in v if k != "content_algo"}
    unsigned = {k: bad_missing[k] for k in bad_missing if k != "sig"}
    bad_missing["sig"] = crypto.sign(hub["member_priv"],
                                     crypto.canonicalize(unsigned))
    r = hub["client"].put(f"/vault/{hub['member_pub']}", json=bad_missing)
    assert r.status_code == 400
    assert r.json()["reason"] == "malformed"

    # Unknown slot type.
    bad_slot = _mint_vault(priv=hub["member_priv"], pub=hub["member_pub"])
    bad_slot["method_slots"][0]["type"] = "smartcard"
    unsigned = {k: bad_slot[k] for k in bad_slot if k != "sig"}
    bad_slot["sig"] = crypto.sign(hub["member_priv"],
                                  crypto.canonicalize(unsigned))
    r = hub["client"].put(f"/vault/{hub['member_pub']}", json=bad_slot)
    assert r.status_code == 400
    assert r.json()["reason"] == "malformed"

    # Empty method_slots.
    empty = _mint_vault(priv=hub["member_priv"], pub=hub["member_pub"], slots=[])
    r = hub["client"].put(f"/vault/{hub['member_pub']}", json=empty)
    assert r.status_code == 400
    assert r.json()["reason"] == "malformed"

    # Too many method_slots (>8).
    too_many = _mint_vault(priv=hub["member_priv"], pub=hub["member_pub"],
                           slots=[_slot("passphrase") for _ in range(9)])
    r = hub["client"].put(f"/vault/{hub['member_pub']}", json=too_many)
    assert r.status_code == 400
    assert r.json()["reason"] == "malformed"


# ---- Storage quota accounting -----------------------------------------

def test_vault_storage_quota_charged_and_released(hub):
    """Uploading a bigger vault charges the delta. Uploading a smaller
    replacement releases the delta. Prevents both under-billing (leaks
    across rotations) and over-billing (drift high on every shrink)."""
    throttler = hub["throttler"]
    caller = hub["member_pub"]

    # First PUT — full body billed.
    v1 = _mint_vault(priv=hub["member_priv"], pub=caller,
                     slots=[_slot("passphrase") for _ in range(4)])
    r = hub["client"].put(f"/vault/{caller}", json=v1)
    assert r.status_code == 200
    used_after_first = throttler._state[caller].storage_used  # noqa: SLF001
    size_v1 = len(crypto.canonicalize(v1))
    assert used_after_first == size_v1

    # Second PUT is larger — delta added.
    v2 = _mint_vault(priv=hub["member_priv"], pub=caller,
                     prev=hash_vault(v1),
                     updated_at="2026-07-05T12:00:01+00:00",
                     slots=[_slot("passphrase") for _ in range(6)])
    r = hub["client"].put(f"/vault/{caller}", json=v2)
    assert r.status_code == 200
    used_after_second = throttler._state[caller].storage_used  # noqa: SLF001
    assert used_after_second == len(crypto.canonicalize(v2))

    # Third PUT is smaller — delta released.
    v3 = _mint_vault(priv=hub["member_priv"], pub=caller,
                     prev=hash_vault(v2),
                     updated_at="2026-07-05T12:00:02+00:00",
                     slots=[_slot("passphrase")])
    r = hub["client"].put(f"/vault/{caller}", json=v3)
    assert r.status_code == 200
    used_after_third = throttler._state[caller].storage_used  # noqa: SLF001
    assert used_after_third == len(crypto.canonicalize(v3))
    assert used_after_third < used_after_second


# ---- Passkey slot (v0.4.76 phase 1 includes both slot types) ----------

def test_vault_put_accepts_passkey_slot(hub):
    v = _mint_vault(priv=hub["member_priv"], pub=hub["member_pub"],
                    slots=[_slot("passphrase"), _slot("passkey", label="iCloud")])
    r = hub["client"].put(f"/vault/{hub['member_pub']}", json=v)
    assert r.status_code == 200, r.text
