"""Pending-registration HTTP wire contract. v0.4.0 — keygen approval push.

Pins the four endpoints that close the on-device-keygen approval loop:
  POST   /pending                  — public; the device registers itself
  GET    /pending                  — board-auth; list the queue for admin UI
  DELETE /pending/{pubkey}         — board-auth; admin rejects/clears
  WS     /pending/watch?pubkey=X   — public; held open until pubkey attested

And the wiring: a successful /admin/attest that adds a new attestation
must fire mark_attested so any open watcher for that pubkey gets pushed.

Existing test_api.py `hub` fixture (member-attested, member-authed) is
reused; a small helper attests a fresh board-role member and returns
their session token for the admin-gated routes.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cove import crypto
from cove.identity import issue_attestation, issue_directory, hash_manifest


# ---- helpers ----------------------------------------------------------

def _manifest_dict(m) -> dict:
    return {
        "org": m.org,
        "attestations": [asdict(a) for a in m.attestations],
        "revocations": [asdict(r) for r in m.revocations],
        "updated_at": m.updated_at,
        "prev_manifest_hash": m.prev_manifest_hash,
        "sig": m.sig,
    }


def _attest_new(hub, *, role: str, pub: str, display_name: str = "Test",
                affiliation: str = "U-X", title=None,
                issued_at: str = "2026-06-01T00:00:00+00:00",
                updated_at: str = "2026-06-15T00:00:00+00:00"):
    """Append a fresh attestation by issuing a new root-signed manifest
    chained off the current head, posting to /admin/attest. Returns the
    posted manifest — caller asserts hub["directory"] now resolves the key.
    """
    current = hub["directory"].manifest
    new_att = issue_attestation(
        hub["root_priv"], member_pubkey=pub, display_name=display_name,
        affiliation=affiliation, role=role, title=title,
        issuer_pubkey=hub["root_pub"], issued_at=issued_at,
    )
    new_manifest = issue_directory(
        hub["root_priv"], org=hub["root_pub"],
        attestations=list(current.attestations) + [new_att],
        revocations=list(current.revocations),
        updated_at=updated_at,
        prev_manifest_hash=hash_manifest(current),
    )
    r = hub["client"].post(
        "/admin/attest", json={"manifest": _manifest_dict(new_manifest)})
    assert r.status_code == 200, r.text
    return new_manifest


def _board_client(hub) -> TestClient:
    """Spin up a TestClient authenticated as a freshly-attested board member."""
    board_priv, board_pub = crypto.generate_keypair()
    _attest_new(hub, role="board", pub=board_pub,
                display_name="Chair", affiliation="board")

    client = TestClient(hub["app"])
    ch = client.post("/auth/challenge").json()
    sig = crypto.sign(board_priv, ch["nonce"].encode())
    sess = client.post("/auth/verify", json={
        "pubkey": board_pub, "nonce": ch["nonce"], "sig": sig,
    }).json()
    client.headers["Authorization"] = f"Bearer {sess['token']}"
    return client


def _mint_code(hub, ttl_seconds: int = 3600,
               name_hint: str = "test invite") -> str:
    """v0.4.33: every /pending POST now needs an invite code. Tests mint
    a fresh one inline via the registry (skipping the root-signed POST
    plumbing since that's exercised separately by the /admin/invites
    tests)."""
    return hub["invites"].mint(
        ttl_seconds=ttl_seconds, name_hint=name_hint).code


# ---- POST /pending (public) -------------------------------------------

def test_post_pending_registers_a_request(hub):
    """A device about to be approved POSTs its pubkey + self-reported
    name hint. Endpoint is public — the requester doesn't have a
    session yet (their attestation is exactly what they're asking for)."""
    unauth = TestClient(hub["app"])
    pubkey = "aa" * 32
    r = unauth.post("/pending", json={
        "pubkey": pubkey, "name_hint": "Jane Doe",
        "requested_at": "2026-06-27T12:00:00+00:00",
        "invite": _mint_code(hub, name_hint="for Jane Doe"),
    })
    assert r.status_code == 200
    # The queue lives where the admin can list it; we don't need to
    # surface it through the public API.
    board = _board_client(hub)
    queue = board.get("/pending").json()
    assert any(row["pubkey"] == pubkey and row["name_hint"] == "Jane Doe"
               for row in queue["pending"])


def test_post_pending_is_idempotent_on_pubkey(hub):
    """A typo'd name-hint re-submit doesn't duplicate the row. Both
    POSTs need their own invite — first one is consumed, second one
    needs a fresh code (matches the v0.4.33 'one code, one submission'
    rule)."""
    unauth = TestClient(hub["app"])
    pubkey = "bb" * 32
    unauth.post("/pending", json={"pubkey": pubkey, "name_hint": "Typo",
                                  "requested_at": "2026-06-27T12:00:00+00:00",
                                  "invite": _mint_code(hub)})
    unauth.post("/pending", json={"pubkey": pubkey, "name_hint": "Bob",
                                  "requested_at": "2026-06-27T12:01:00+00:00",
                                  "invite": _mint_code(hub)})
    board = _board_client(hub)
    rows = [r for r in board.get("/pending").json()["pending"]
            if r["pubkey"] == pubkey]
    assert len(rows) == 1
    assert rows[0]["name_hint"] == "Bob"


def test_post_pending_bad_payload_returns_400(hub):
    unauth = TestClient(hub["app"])
    r = unauth.post("/pending", json={"pubkey": "short"})
    assert r.status_code == 400


def test_post_pending_rejects_already_attested_pubkey(hub):
    """If the pubkey is already in the directory, queuing it is
    nonsensical — return a clear error so the client transitions to
    the auth flow instead of showing a stale 'waiting' state."""
    unauth = TestClient(hub["app"])
    r = unauth.post("/pending", json={
        "pubkey": hub["member_pub"], "name_hint": "Alice",
        "requested_at": "2026-06-27T12:00:00+00:00",
        "invite": _mint_code(hub),
    })
    assert r.status_code == 409
    assert r.json()["error"] == "already_attested"


# ---- GET /pending (board-auth) ----------------------------------------

def test_get_pending_requires_session(hub):
    unauth = TestClient(hub["app"])
    r = unauth.get("/pending")
    assert r.status_code == 401


def test_get_pending_requires_board_role(hub):
    """The default fixture member has role='member' — not allowed to
    see the queue. Prevents arbitrary members from enumerating who's
    in onboarding."""
    r = hub["client"].get("/pending")
    assert r.status_code == 403


def test_get_pending_respects_custom_capabilities_by_role(hub):
    """v0.4.25: the require_capability gate reads the manifest's
    capabilities_by_role map. An org that grants 'admin' to its
    'officer' role lets officer-tier members hit /pending — no
    code change. Inverse: revoking 'admin' from 'board' locks them
    out under the same gate."""
    # Step 1: attest an officer and verify they get 403 under the
    # default mapping (officer has no caps by default).
    officer_priv, officer_pub = crypto.generate_keypair()
    _attest_new(hub, role="officer", pub=officer_pub,
                display_name="Treasurer", affiliation="board",
                issued_at="2026-06-01T00:00:00+00:00",
                updated_at="2026-06-15T00:00:00+00:00")
    officer = TestClient(hub["app"])
    ch = officer.post("/auth/challenge").json()
    sig = crypto.sign(officer_priv, ch["nonce"].encode())
    sess = officer.post("/auth/verify", json={
        "pubkey": officer_pub, "nonce": ch["nonce"], "sig": sig,
    }).json()
    officer.headers["Authorization"] = f"Bearer {sess['token']}"
    assert officer.get("/pending").status_code == 403

    # Step 2: root re-issues the manifest with an explicit map that
    # grants officer the 'admin' capability. Now the same session token
    # works against /pending.
    current = hub["directory"].manifest
    remapped = issue_directory(
        hub["root_priv"], org=hub["root_pub"],
        attestations=list(current.attestations),
        revocations=list(current.revocations),
        updated_at="2026-06-15T00:00:01+00:00",
        prev_manifest_hash=hash_manifest(current),
        capabilities_by_role={
            "board":   ["admin", "archive"],
            "officer": ["admin"],
        },
    )
    r = hub["client"].post(
        "/admin/attest", json={"manifest": _manifest_dict_v25(remapped)})
    assert r.status_code == 200, r.text
    assert officer.get("/pending").status_code == 200


def _manifest_dict_v25(m) -> dict:
    """Wire-form manifest serializer that includes v0.4.25's
    capabilities_by_role field. The local _manifest_dict above pre-dates
    that field and is kept narrow on purpose."""
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
    return out


def test_get_pending_returns_queue_in_order(hub):
    board = _board_client(hub)
    unauth = TestClient(hub["app"])
    unauth.post("/pending", json={"pubkey": "11" * 32, "name_hint": "B",
                                  "requested_at": "2026-06-27T12:01:00+00:00",
                                  "invite": _mint_code(hub)})
    unauth.post("/pending", json={"pubkey": "22" * 32, "name_hint": "A",
                                  "requested_at": "2026-06-27T12:00:00+00:00",
                                  "invite": _mint_code(hub)})
    queue = board.get("/pending").json()["pending"]
    assert [r["pubkey"] for r in queue] == ["22" * 32, "11" * 32]


# ---- DELETE /pending/{pubkey} (board-auth) ----------------------------

def test_delete_pending_requires_board_role(hub):
    r = hub["client"].delete("/pending/" + "aa" * 32)
    assert r.status_code == 403


def test_delete_pending_clears_entry(hub):
    board = _board_client(hub)
    unauth = TestClient(hub["app"])
    unauth.post("/pending", json={"pubkey": "cc" * 32, "name_hint": "X",
                                  "requested_at": "2026-06-27T12:00:00+00:00",
                                  "invite": _mint_code(hub)})
    assert board.delete("/pending/" + "cc" * 32).status_code == 200
    assert all(r["pubkey"] != "cc" * 32
               for r in board.get("/pending").json()["pending"])


def test_delete_pending_unknown_pubkey_is_idempotent(hub):
    board = _board_client(hub)
    # Already-cleared (race with attestation push) — succeed silently.
    assert board.delete("/pending/" + "ee" * 32).status_code == 200


# ---- WS /pending/watch (public, push) ---------------------------------

def test_pending_watch_pushes_when_admin_attest_fires(hub):
    """The whole point of v0.4.0: a member's device holds /pending/watch
    open; the keymaster's /admin/attest call lands the attestation; the
    device gets pushed immediately. No polling."""
    unauth = TestClient(hub["app"])
    new_priv, new_pub = crypto.generate_keypair()
    unauth.post("/pending", json={
        "pubkey": new_pub, "name_hint": "Carol",
        "requested_at": "2026-06-27T12:00:00+00:00",
        "invite": _mint_code(hub),
    })

    with unauth.websocket_connect(f"/pending/watch?pubkey={new_pub}") as ws:
        # The admin issues the attestation while the WS is held open.
        _attest_new(hub, role="member", pub=new_pub, display_name="Carol")
        msg = ws.receive_json()
        assert msg["type"] == "attested"
        assert msg["pubkey"] == new_pub
        # The pushed payload carries the new manifest_hash so the client
        # can fetch /directory and verify before transitioning.
        assert "manifest_hash" in msg


def test_pending_watch_already_attested_pushes_immediately(hub):
    """Reconnect-safe path: a device whose WS dropped DURING attestation
    must, on reconnect, immediately see the 'attested' push without
    waiting on a future event. The handshake checks the directory."""
    unauth = TestClient(hub["app"])
    with unauth.websocket_connect(
            f"/pending/watch?pubkey={hub['member_pub']}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "attested"
        assert msg["pubkey"] == hub["member_pub"]


def test_pending_watch_rejects_missing_pubkey(hub):
    unauth = TestClient(hub["app"])
    with unauth.websocket_connect("/pending/watch") as ws:
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
        assert ei.value.code == 1008


def test_pending_watch_drops_pending_entry_after_attest(hub):
    """Once attested, the pending entry should be cleared — the admin
    queue must not still show a row for someone who's already in. The
    /admin/attest hook owns this."""
    unauth = TestClient(hub["app"])
    new_priv, new_pub = crypto.generate_keypair()
    unauth.post("/pending", json={
        "pubkey": new_pub, "name_hint": "Carol",
        "requested_at": "2026-06-27T12:00:00+00:00",
        "invite": _mint_code(hub),
    })
    _attest_new(hub, role="member", pub=new_pub, display_name="Carol")

    board = _board_client(hub)
    assert all(r["pubkey"] != new_pub
               for r in board.get("/pending").json()["pending"])


# ---- /admin/invites + invite-gate (v0.4.33) --------------------------

def _sign_admin_payload(root_priv: str, payload: dict) -> str:
    return crypto.sign(root_priv, crypto.canonicalize(payload))


def test_pending_without_invite_returns_401(hub):
    """The whole point of v0.4.33: an unauthenticated submitter without
    a valid code can't even reach the keymaster's queue."""
    unauth = TestClient(hub["app"])
    r = unauth.post("/pending", json={
        "pubkey": "aa" * 32, "name_hint": "Spammer",
        "requested_at": "2026-06-27T12:00:00+00:00",
    })
    assert r.status_code == 401
    assert r.json()["error"] == "invite_required"


def test_pending_with_unknown_code_returns_401(hub):
    unauth = TestClient(hub["app"])
    r = unauth.post("/pending", json={
        "pubkey": "aa" * 32, "name_hint": "Spammer",
        "requested_at": "2026-06-27T12:00:00+00:00",
        "invite": "deadbeef" * 4,
    })
    assert r.status_code == 401
    assert r.json()["error"] == "invite_unusable"
    assert r.json()["reason"] == "unknown"


def test_pending_with_used_code_returns_401(hub):
    """Single-use: a second POST with the same code is rejected."""
    unauth = TestClient(hub["app"])
    code = _mint_code(hub)
    r1 = unauth.post("/pending", json={
        "pubkey": "aa" * 32, "name_hint": "First",
        "requested_at": "2026-06-27T12:00:00+00:00", "invite": code,
    })
    assert r1.status_code == 200
    r2 = unauth.post("/pending", json={
        "pubkey": "bb" * 32, "name_hint": "Replay",
        "requested_at": "2026-06-27T12:00:01+00:00", "invite": code,
    })
    assert r2.status_code == 401
    assert r2.json()["reason"] == "already_used"


def test_pending_with_expired_code_returns_401(hub):
    """An expired code is rejected — wall-clock-independent test using
    the registry's time_fn injection wouldn't be reachable through the
    public API, so we mint with a 0-second TTL which means 'expires the
    moment it's created' and verify the next .consume rejects."""
    unauth = TestClient(hub["app"])
    # ttl_seconds=0 is rejected at mint; we mint with ttl=1 and sleep
    # past it. Test runs in ~1.1s — acceptable for the suite.
    inv = hub["invites"].mint(ttl_seconds=1, name_hint=None)
    import time
    time.sleep(1.1)
    r = unauth.post("/pending", json={
        "pubkey": "aa" * 32, "name_hint": "TooLate",
        "requested_at": "2026-06-27T12:00:00+00:00", "invite": inv.code,
    })
    assert r.status_code == 401
    assert r.json()["reason"] == "expired"


def test_admin_invites_mint_round_trip(hub):
    """Root-signed mint returns a code + expires_at; the new code is
    immediately listed under /admin/invites for the board to see."""
    payload = {"ttl_seconds": 3600, "name_hint": "Alice's neighbor"}
    body = {"payload": payload,
            "sig": _sign_admin_payload(hub["root_priv"], payload)}
    r = hub["client"].post("/admin/invites", json=body)
    assert r.status_code == 200
    minted = r.json()
    assert isinstance(minted["code"], str) and len(minted["code"]) == 32
    assert minted["name_hint"] == "Alice's neighbor"

    board = _board_client(hub)
    listing = board.get("/admin/invites").json()["invites"]
    assert any(i["code"] == minted["code"] for i in listing)


def test_admin_invites_mint_rejects_unsigned_payload(hub):
    payload = {"ttl_seconds": 3600}
    body = {"payload": payload, "sig": "ff" * 64}
    r = hub["client"].post("/admin/invites", json=body)
    assert r.status_code == 401


def test_admin_invites_revoke_round_trip(hub):
    code = _mint_code(hub)
    payload = {"code": code}
    body = {"payload": payload,
            "sig": _sign_admin_payload(hub["root_priv"], payload)}
    # TestClient.delete signature varies across httpx versions; use the
    # generic .request() to keep the body+json contract explicit.
    r = hub["client"].request("DELETE", f"/admin/invites/{code}", json=body)
    assert r.status_code == 200

    # Now /pending with that code is rejected as revoked.
    unauth = TestClient(hub["app"])
    r2 = unauth.post("/pending", json={
        "pubkey": "aa" * 32, "name_hint": "Locked out",
        "requested_at": "2026-06-27T12:00:00+00:00", "invite": code,
    })
    assert r2.status_code == 401
    assert r2.json()["reason"] == "revoked"


def test_admin_invites_list_skips_used_codes(hub):
    """Once a code is consumed by /pending, it falls out of the
    active-invites listing — the keymaster's UI shouldn't keep showing
    'this code is outstanding' for a code that's already been spent."""
    code = _mint_code(hub)
    unauth = TestClient(hub["app"])
    r = unauth.post("/pending", json={
        "pubkey": "aa" * 32, "name_hint": "Used",
        "requested_at": "2026-06-27T12:00:00+00:00", "invite": code,
    })
    assert r.status_code == 200
    board = _board_client(hub)
    listing = board.get("/admin/invites").json()["invites"]
    assert not any(i["code"] == code for i in listing)


def test_admin_invites_list_requires_admin_capability(hub):
    """A plain member can't enumerate active codes — they'd be useful
    information for a malicious member (could phish a peer with one)."""
    unauth = TestClient(hub["app"])
    assert unauth.get("/admin/invites").status_code == 401
    # The default fixture member is role=member (no admin cap by default).
    assert hub["client"].get("/admin/invites").status_code == 403


def test_pending_for_already_attested_does_not_burn_invite(hub):
    """If the pubkey is already attested, /pending returns 409 BEFORE
    consuming the invite. A re-onboarding member with stale state
    shouldn't have their code spent — the keymaster mints once, the
    member uses it whether they're new or re-installing."""
    code = _mint_code(hub)
    unauth = TestClient(hub["app"])
    r = unauth.post("/pending", json={
        "pubkey": hub["member_pub"], "name_hint": "Re-attempt",
        "requested_at": "2026-06-27T12:00:00+00:00", "invite": code,
    })
    assert r.status_code == 409
    assert r.json()["error"] == "already_attested"
    # Code is still active and usable by someone else (didn't get burned).
    inv = hub["invites"].get(code)
    assert inv.is_active
