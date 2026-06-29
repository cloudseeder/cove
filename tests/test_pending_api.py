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
    })
    assert r.status_code == 200
    # The queue lives where the admin can list it; we don't need to
    # surface it through the public API.
    board = _board_client(hub)
    queue = board.get("/pending").json()
    assert any(row["pubkey"] == pubkey and row["name_hint"] == "Jane Doe"
               for row in queue["pending"])


def test_post_pending_is_idempotent_on_pubkey(hub):
    unauth = TestClient(hub["app"])
    pubkey = "bb" * 32
    unauth.post("/pending", json={"pubkey": pubkey, "name_hint": "Typo",
                                  "requested_at": "2026-06-27T12:00:00+00:00"})
    unauth.post("/pending", json={"pubkey": pubkey, "name_hint": "Bob",
                                  "requested_at": "2026-06-27T12:01:00+00:00"})
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
                                  "requested_at": "2026-06-27T12:01:00+00:00"})
    unauth.post("/pending", json={"pubkey": "22" * 32, "name_hint": "A",
                                  "requested_at": "2026-06-27T12:00:00+00:00"})
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
                                  "requested_at": "2026-06-27T12:00:00+00:00"})
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
    })
    _attest_new(hub, role="member", pub=new_pub, display_name="Carol")

    board = _board_client(hub)
    assert all(r["pubkey"] != new_pub
               for r in board.get("/pending").json()["pending"])
