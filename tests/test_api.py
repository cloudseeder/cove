"""HTTP wire contract. Spec §7.

End-to-end with real modules (store, translog, pipeline, overview, ledger,
directory) wired through create_app. Auth and WebSocket fan-out are out of
scope here — they land alongside §5 and §7's WS slice.

Pins the public contract clients depend on:
  - POST /entries returns (id, seq) on accept, structured 429 body on throttle,
    400 on bad-entry rejection
  - GET /sync returns delta-sync entries strictly after `since` in seq order
  - GET /sth returns a Signed Tree Head that verify_sth accepts
  - GET /proof/inclusion returns a proof that verify_inclusion accepts
  - GET /proof/consistency returns a proof verify_consistency accepts
  - GET /overview returns child map + seq order
  - GET /directory returns the root-signed manifest
  - GET /ledger partitions members into acked / not_acked
"""
from __future__ import annotations

from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from cove import crypto
from cove.api import create_app
from cove.auth import AuthService
from cove.entry import Entry, sign_entry
from cove.identity import (
    Directory, Revocation, issue_attestation, issue_directory,
)
from cove.index import Ledger, Overview
from cove.pipeline import Pipeline
from cove.store import EventStore
from cove.throttle import Throttler
from cove.translog import (
    ConsistencyProof, InclusionProof, STH, TamperEvidentLog,
    verify_consistency, verify_inclusion, verify_sth,
)
from starlette.websockets import WebSocketDisconnect


# ---- fixtures: a fully-wired hub --------------------------------------

@pytest.fixture
def hub(tmp_path, root_keypair, hub_keypair, keypair):
    root_priv, root_pub = root_keypair
    hub_priv, hub_pub = hub_keypair
    member_priv, member_pub = keypair

    att_member = issue_attestation(
        root_priv, member_pubkey=member_pub, display_name="Alice",
        unit="U-1", role="member", issuer_pubkey=root_pub,
        issued_at="2026-01-01T00:00:00+00:00",
    )
    # Revoked member: attested in Jan, revoked in Feb. The attestation stays
    # in the manifest so the ledger can still surface them as a historical
    # recipient (§2.3 — events signed before revocation remain valid).
    revoked_priv, revoked_pub = crypto.generate_keypair()
    att_revoked = issue_attestation(
        root_priv, member_pubkey=revoked_pub, display_name="Bob (left)",
        unit="U-2", role="member", issuer_pubkey=root_pub,
        issued_at="2026-01-01T00:00:00+00:00",
    )
    rev = Revocation(pubkey=revoked_pub,
                     revoked_at="2026-02-01T00:00:00+00:00", reason="left")
    manifest = issue_directory(root_priv, org=root_pub,
                               attestations=[att_member, att_revoked],
                               revocations=[rev],
                               updated_at="2026-06-01T00:00:00+00:00")

    store = EventStore(str(tmp_path / "hub.db"))
    translog = TamperEvidentLog(hub_priv, hub_pub)
    overview = Overview()
    ledger = Ledger()
    directory = Directory.from_manifest(manifest)
    throttler = Throttler()
    pipeline = Pipeline(store=store, directory=directory, translog=translog,
                        overview=overview, ledger=ledger, throttler=throttler)
    auth = AuthService(directory=directory)

    app = create_app(pipeline=pipeline, store=store, translog=translog,
                     overview=overview, ledger=ledger,
                     directory=directory, directory_manifest=manifest,
                     auth=auth)

    client = TestClient(app)
    # Pre-auth the test client so existing tests of gated routes Just Work.
    # An unauth'd client is constructed inline in the dedicated 401 tests.
    ch = client.post("/auth/challenge").json()
    sig = crypto.sign(member_priv, ch["nonce"].encode())
    sess = client.post("/auth/verify", json={
        "pubkey": member_pub, "nonce": ch["nonce"], "sig": sig,
    }).json()
    client.headers["Authorization"] = f"Bearer {sess['token']}"

    return {
        "client": client,
        "app": app,
        "member_priv": member_priv, "member_pub": member_pub,
        "revoked_pub": revoked_pub,
        "root_pub": root_pub, "hub_pub": hub_pub,
        "store": store, "translog": translog,
        "overview": overview, "ledger": ledger,
        "auth": auth, "directory": directory,
        "session_token": sess["token"],
    }


def _entry_payload(ev: Entry) -> dict:
    """Wire format: everything on the Entry, blobs already as dicts."""
    return asdict(ev)


def _signed_post(member_priv, member_pub, *, thread="t1", body="hi",
                 parents=None) -> Entry:
    return sign_entry(Entry(
        thread=thread, author=member_pub, kind="post",
        created_at="2026-01-01T00:00:00Z", body=body,
        parents=parents or [],
    ), member_priv)


# ---- /healthz ---------------------------------------------------------

def test_healthz_ok(hub):
    r = hub["client"].get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---- /auth/challenge + /auth/verify (§5) -----------------------------

def test_auth_challenge_returns_nonce_and_expiry(hub):
    r = hub["client"].post("/auth/challenge")
    assert r.status_code == 200
    body = r.json()
    assert len(body["nonce"]) == 64
    assert body["expires_at"] > 0


def test_auth_verify_happy_path_returns_bound_session(hub):
    ch = hub["client"].post("/auth/challenge").json()
    sig = crypto.sign(hub["member_priv"], ch["nonce"].encode())
    r = hub["client"].post("/auth/verify", json={
        "pubkey": hub["member_pub"],
        "nonce": ch["nonce"],
        "sig": sig,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pubkey"] == hub["member_pub"]
    assert len(body["token"]) == 64
    # The minted token resolves on the service to the same pubkey.
    assert hub["auth"].resolve_session(body["token"]) == hub["member_pub"]


def test_auth_verify_bad_signature_returns_401(hub):
    ch = hub["client"].post("/auth/challenge").json()
    other_priv, _ = crypto.generate_keypair()
    sig = crypto.sign(other_priv, ch["nonce"].encode())
    r = hub["client"].post("/auth/verify", json={
        "pubkey": hub["member_pub"], "nonce": ch["nonce"], "sig": sig,
    })
    assert r.status_code == 401
    assert r.json()["error"] == "auth_failed"


def test_auth_verify_unknown_pubkey_returns_401(hub):
    """Pubkey not in directory — caught at verify, not at challenge,
    so /auth/challenge doesn't leak membership."""
    other_priv, other_pub = crypto.generate_keypair()
    ch = hub["client"].post("/auth/challenge").json()
    sig = crypto.sign(other_priv, ch["nonce"].encode())
    r = hub["client"].post("/auth/verify", json={
        "pubkey": other_pub, "nonce": ch["nonce"], "sig": sig,
    })
    assert r.status_code == 401


def test_auth_verify_unknown_nonce_returns_401(hub):
    """Replay defense — a nonce the hub never issued is rejected."""
    sig = crypto.sign(hub["member_priv"], (b"\x00" * 32).hex().encode())
    r = hub["client"].post("/auth/verify", json={
        "pubkey": hub["member_pub"], "nonce": "00" * 32, "sig": sig,
    })
    assert r.status_code == 401


def test_auth_verify_consumed_nonce_returns_401_on_replay(hub):
    """Spec §5 single-use: a replay of a previously-used (nonce, sig)
    must fail. The first call succeeds; the second is rejected."""
    ch = hub["client"].post("/auth/challenge").json()
    sig = crypto.sign(hub["member_priv"], ch["nonce"].encode())
    payload = {"pubkey": hub["member_pub"], "nonce": ch["nonce"], "sig": sig}
    assert hub["client"].post("/auth/verify", json=payload).status_code == 200
    r2 = hub["client"].post("/auth/verify", json=payload)
    assert r2.status_code == 401


def test_auth_verify_missing_fields_returns_400(hub):
    r = hub["client"].post("/auth/verify", json={})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


# ---- gating: data routes require a valid session ---------------------

GATED_GETS = ["/sync?thread=t1&since=-1", "/overview?thread=t1",
              "/directory", "/ledger?entry=anything"]


@pytest.mark.parametrize("path", GATED_GETS)
def test_gated_get_without_auth_header_returns_401(hub, path):
    """No Authorization header at all -> 401 auth_required, BEFORE the
    handler runs (no entry id lookup, no 404)."""
    unauth = TestClient(hub["app"])
    r = unauth.get(path)
    assert r.status_code == 401
    body = r.json()
    assert body["error"] == "auth_required"
    assert "missing" in body["reason"].lower()


@pytest.mark.parametrize("path", GATED_GETS)
def test_gated_get_with_bogus_bearer_returns_401(hub, path):
    unauth = TestClient(hub["app"])
    unauth.headers["Authorization"] = "Bearer " + ("ff" * 32)
    r = unauth.get(path)
    assert r.status_code == 401
    assert "invalid" in r.json()["reason"].lower()


def test_post_entries_without_auth_returns_401(hub):
    ev = _signed_post(hub["member_priv"], hub["member_pub"])
    unauth = TestClient(hub["app"])
    r = unauth.post("/entries", json=_entry_payload(ev))
    assert r.status_code == 401
    # Gate runs BEFORE the pipeline — so even a perfectly-signed entry by
    # an attested member is rejected without a token.
    assert hub["store"].exists(ev.id) is False


def test_public_routes_remain_accessible_without_auth(hub):
    """Verification artifacts (sth, proofs) and the auth handshake stay
    public — CT-style transparency for the head and proofs."""
    unauth = TestClient(hub["app"])
    assert unauth.get("/healthz").status_code == 200
    assert unauth.get("/sth").status_code == 200
    assert unauth.post("/auth/challenge").status_code == 200


# ---- WS /stream (§7, §7.1 step 10) -----------------------------------

def test_stream_rejects_without_token(hub):
    """No Authorization header AND no ?token= — server closes the WS with
    code 1008 (policy violation). TestClient accepts then sees the close
    frame on the first receive."""
    unauth = TestClient(hub["app"])
    with unauth.websocket_connect("/stream") as ws:
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
        assert ei.value.code == 1008


def test_stream_rejects_invalid_token(hub):
    unauth = TestClient(hub["app"])
    with unauth.websocket_connect(
            "/stream", headers={"Authorization": "Bearer " + "ff" * 32}) as ws:
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
        assert ei.value.code == 1008


def test_stream_accepts_via_query_token_for_browser_clients(hub):
    """Browsers can't set Authorization on a WS handshake — ?token= is the
    documented fallback. Connecting via the query string must work."""
    unauth = TestClient(hub["app"])
    with unauth.websocket_connect(
            f"/stream?token={hub['session_token']}") as ws:
        # Connection accepted; trigger an entry and verify it's delivered.
        ev = _signed_post(hub["member_priv"], hub["member_pub"], body="qstring")
        hub["client"].post("/entries", json=_entry_payload(ev))
        msg = ws.receive_json()
        assert msg["entry"]["id"] == ev.id


def test_stream_pushes_accepted_entry(hub):
    """§7.1 step 10: an entry accepted into the log is fanned out to every
    live subscriber. The push includes the full signed entry so the client
    can verify origin without a separate /sync round trip."""
    with hub["client"].websocket_connect("/stream") as ws:
        ev = _signed_post(hub["member_priv"], hub["member_pub"], body="push")
        r = hub["client"].post("/entries", json=_entry_payload(ev))
        assert r.status_code == 200
        msg = ws.receive_json()
        assert msg["type"] == "entry"
        assert msg["entry"]["id"] == ev.id
        assert msg["seq"] == r.json()["seq"]
        # Sig + id survived serialization — the client can verify_entry it.
        assert msg["entry"]["sig"] == ev.sig


def test_stream_delivers_to_every_subscriber(hub):
    """Two subscribers, one POST -> both receive the push."""
    with hub["client"].websocket_connect("/stream") as ws1:
        with hub["client"].websocket_connect("/stream") as ws2:
            ev = _signed_post(hub["member_priv"], hub["member_pub"], body="multi")
            hub["client"].post("/entries", json=_entry_payload(ev))
            m1, m2 = ws1.receive_json(), ws2.receive_json()
            assert m1["entry"]["id"] == m2["entry"]["id"] == ev.id


def test_stream_disconnect_unregisters_so_next_broadcast_succeeds(hub):
    """A disconnected client must not stay in the fan-out registry —
    otherwise the next broadcast tries to send into a dead socket and
    might pollute the loop. We confirm by reconnecting and seeing a
    later push delivered cleanly."""
    with hub["client"].websocket_connect("/stream"):
        pass        # exit immediately disconnects

    # A POST after the disconnect must still succeed and not blow up.
    a = _signed_post(hub["member_priv"], hub["member_pub"], body="post-disco")
    r = hub["client"].post("/entries", json=_entry_payload(a))
    assert r.status_code == 200

    # A fresh subscriber gets the NEXT entry — confirms the fan-out is alive.
    with hub["client"].websocket_connect("/stream") as ws:
        b = _signed_post(hub["member_priv"], hub["member_pub"], body="after")
        hub["client"].post("/entries", json=_entry_payload(b))
        msg = ws.receive_json()
        assert msg["entry"]["id"] == b.id


def test_stream_does_not_replay_history_only_pushes_post_subscribe(hub):
    """v1 push semantics: subscribers get entries accepted AFTER they
    connect. Pre-subscribe history is the /sync responsibility (§7).
    Without this, every reconnect would re-deliver the whole log."""
    # An entry accepted BEFORE any subscriber.
    pre = _signed_post(hub["member_priv"], hub["member_pub"], body="pre")
    hub["client"].post("/entries", json=_entry_payload(pre))

    with hub["client"].websocket_connect("/stream") as ws:
        # POST after subscribe — that's the one we expect to receive.
        post = _signed_post(hub["member_priv"], hub["member_pub"], body="post")
        hub["client"].post("/entries", json=_entry_payload(post))
        msg = ws.receive_json()
        assert msg["entry"]["id"] == post.id    # not pre.id


def test_session_invalidated_when_member_revoked(hub):
    """§5 invariant: the session is bound to a *non-revoked* attested key.
    Revoke the member mid-session; the next gated request must fail."""
    # Confirm the pre-auth client works first.
    assert hub["client"].get("/directory").status_code == 200
    # Mutate the directory to revoke the member.
    hub["directory"]._revoked[hub["member_pub"]] = Revocation(
        pubkey=hub["member_pub"],
        revoked_at="2026-02-01T00:00:00+00:00", reason="key compromise",
    )
    # Same client, same token — now denied.
    r = hub["client"].get("/directory")
    assert r.status_code == 401


# ---- POST /entries ---------------------------------------------------

def test_post_entries_accepts_valid_entry(hub):
    ev = _signed_post(hub["member_priv"], hub["member_pub"], body="hello")
    r = hub["client"].post("/entries", json=_entry_payload(ev))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == ev.id
    assert body["seq"] == 0
    # round-trip through the store
    assert hub["store"].exists(ev.id) is True


def test_post_entries_rejects_bad_signature(hub):
    ev = _signed_post(hub["member_priv"], hub["member_pub"])
    payload = _entry_payload(ev)
    payload["body"] = "tampered after signing"
    r = hub["client"].post("/entries", json=payload)
    assert r.status_code == 400
    assert r.json()["error"] == "rejected"


def test_post_entries_rejects_unknown_author(hub):
    other_priv, other_pub = crypto.generate_keypair()    # not in directory
    ev = sign_entry(Entry(thread="t1", author=other_pub, kind="post",
                          created_at="2026-01-01T00:00:00Z", body="hi"),
                    other_priv)
    r = hub["client"].post("/entries", json=_entry_payload(ev))
    assert r.status_code == 400
    assert "unknown" in r.json()["reason"].lower()


def test_post_entries_returns_structured_throttle_body(hub):
    """Spec §7.2.3: a throttled client sees {error, scope, limit, retry_after_s, detail}.

    Drain the member burst, then assert the (burst+1)th call comes back 429
    with the structured body and a Retry-After header.
    """
    from cove.config import TIERS
    burst = TIERS["member"].burst
    for i in range(burst):
        ev = _signed_post(hub["member_priv"], hub["member_pub"], body=f"m{i}")
        r = hub["client"].post("/entries", json=_entry_payload(ev))
        assert r.status_code == 200

    over = _signed_post(hub["member_priv"], hub["member_pub"], body="overflow")
    r = hub["client"].post("/entries", json=_entry_payload(over))
    assert r.status_code == 429
    body = r.json()
    assert body["error"] == "throttled"
    assert body["scope"] == "rate"
    assert body["limit"] == TIERS["member"].entries_per_min
    assert body["retry_after_s"] is not None
    assert "Retry-After" in r.headers


# ---- GET /sync --------------------------------------------------------

def test_sync_returns_entries_strictly_after_since(hub):
    ids = []
    for i in range(3):
        ev = _signed_post(hub["member_priv"], hub["member_pub"], body=f"m{i}")
        hub["client"].post("/entries", json=_entry_payload(ev))
        ids.append(ev.id)
    r = hub["client"].get("/sync", params={"thread": "t1", "since": 0})
    assert r.status_code == 200
    got = [e["id"] for e in r.json()["entries"]]
    assert got == ids[1:]   # seq 0 excluded, 1 and 2 returned


def test_sync_from_beginning_with_since_minus_one(hub):
    ev = _signed_post(hub["member_priv"], hub["member_pub"])
    hub["client"].post("/entries", json=_entry_payload(ev))
    r = hub["client"].get("/sync", params={"thread": "t1", "since": -1})
    assert [e["id"] for e in r.json()["entries"]] == [ev.id]


# ---- GET /sth --------------------------------------------------------

def test_sth_returns_signed_tree_head(hub):
    # Empty log first.
    r = hub["client"].get("/sth")
    assert r.status_code == 200
    sth0 = STH(**r.json())
    assert sth0.tree_size == 0
    assert verify_sth(sth0) is True

    ev = _signed_post(hub["member_priv"], hub["member_pub"])
    hub["client"].post("/entries", json=_entry_payload(ev))

    r = hub["client"].get("/sth")
    sth1 = STH(**r.json())
    assert sth1.tree_size == 1
    assert verify_sth(sth1) is True


# ---- GET /proof/inclusion -------------------------------------------

def test_inclusion_proof_verifies_against_sth(hub):
    ev = _signed_post(hub["member_priv"], hub["member_pub"])
    seq = hub["client"].post("/entries", json=_entry_payload(ev)).json()["seq"]

    r = hub["client"].get("/proof/inclusion", params={"entry": ev.id})
    assert r.status_code == 200
    proof = InclusionProof(**r.json())

    sth = STH(**hub["client"].get("/sth").json())
    assert verify_inclusion(ev.id, seq, proof, sth) is True


def test_inclusion_proof_404_for_unknown_entry(hub):
    r = hub["client"].get("/proof/inclusion", params={"entry": "sha256:" + "ff" * 32})
    assert r.status_code == 404


# ---- GET /proof/consistency -----------------------------------------

def test_consistency_proof_verifies_growth(hub):
    # size 1
    a = _signed_post(hub["member_priv"], hub["member_pub"], body="a")
    hub["client"].post("/entries", json=_entry_payload(a))
    old = STH(**hub["client"].get("/sth").json())
    # grow to size 3
    for body in ("b", "c"):
        ev = _signed_post(hub["member_priv"], hub["member_pub"], body=body)
        hub["client"].post("/entries", json=_entry_payload(ev))
    new = STH(**hub["client"].get("/sth").json())
    assert (old.tree_size, new.tree_size) == (1, 3)

    r = hub["client"].get("/proof/consistency",
                          params={"from_size": old.tree_size, "to_size": new.tree_size})
    assert r.status_code == 200
    proof = ConsistencyProof(**r.json())
    assert verify_consistency(proof, old, new) is True


# ---- GET /overview --------------------------------------------------

def test_overview_returns_child_map_and_seq_order(hub):
    # a (root), b (child of a), c (child of a)
    a = _signed_post(hub["member_priv"], hub["member_pub"], body="root")
    a_resp = hub["client"].post("/entries", json=_entry_payload(a))
    assert a_resp.status_code == 200
    b = _signed_post(hub["member_priv"], hub["member_pub"], body="b", parents=[a.id])
    hub["client"].post("/entries", json=_entry_payload(b))
    c = _signed_post(hub["member_priv"], hub["member_pub"], body="c", parents=[a.id])
    hub["client"].post("/entries", json=_entry_payload(c))

    r = hub["client"].get("/overview", params={"thread": "t1"})
    assert r.status_code == 200
    payload = r.json()
    assert payload["thread"] == "t1"
    rows = payload["entries"]
    by_id = {row["id"]: row for row in rows}
    assert [row["id"] for row in rows] == [a.id, b.id, c.id]   # seq order
    assert sorted(by_id[a.id]["children"]) == sorted([b.id, c.id])
    assert by_id[b.id]["parents"] == [a.id]
    assert by_id[c.id]["children"] == []


# ---- GET /directory -------------------------------------------------

def test_directory_returns_signed_manifest(hub):
    r = hub["client"].get("/directory")
    assert r.status_code == 200
    payload = r.json()
    assert payload["org"] == hub["root_pub"]
    assert payload["sig"]
    assert any(att["member_pubkey"] == hub["member_pub"] for att in payload["attestations"])
    assert any(rev["pubkey"] == hub["revoked_pub"] for rev in payload["revocations"])


# ---- GET /ledger ----------------------------------------------------

def test_ledger_partitions_members_into_acked_and_not(hub):
    """Post a broadcast (the notice), then submit a receipt covering it.
    /ledger?entry=<notice_id> must report alice as acked, anyone else not_acked.
    """
    notice = _signed_post(hub["member_priv"], hub["member_pub"], body="notice")
    notice_seq = hub["client"].post("/entries", json=_entry_payload(notice)).json()["seq"]

    # Bypass the pipeline for the receipt — the receipt-acceptance path needs
    # the receipt body shape to land, which is a later slice. Hand the ack to
    # the ledger directly so /ledger can exercise the response contract.
    sth = STH(**hub["client"].get("/sth").json())
    hub["ledger"].apply_receipt(hub["member_pub"], "t1", notice_seq,
                                (sth.tree_size, sth.root_hash))

    r = hub["client"].get("/ledger", params={"entry": notice.id})
    assert r.status_code == 200
    payload = r.json()
    assert hub["member_pub"] in payload["acked"]
    # revoked member never acked — should appear in not_acked.
    assert hub["revoked_pub"] in payload["not_acked"]


def test_ledger_404_for_unknown_entry(hub):
    r = hub["client"].get("/ledger", params={"entry": "sha256:" + "ff" * 32})
    assert r.status_code == 404


# ---- restart path: lifespan rebuilds the translog from the store ----

def test_startup_reconciles_in_memory_translog_with_on_disk_store(
        tmp_path, hub_keypair, root_keypair, keypair):
    """The translog isn't persisted (translog-notes §6); on process restart
    the in-memory tree is empty even when the store on disk is not. The
    create_app lifespan must rebuild from store.iter_global() BEFORE the
    first request — otherwise /sth and /proof/* would serve correct-looking
    but stale responses against a tree_size=0 root.

    This is the rebuild-equals-incremental property exercised through the
    restart path specifically rather than the fault path.
    """
    hub_priv, hub_pub = hub_keypair
    root_priv, root_pub = root_keypair
    member_priv, member_pub = keypair

    # --- prior life: a process that already accepted entries ---
    store = EventStore(str(tmp_path / "hub.db"))
    pre_restart_translog = TamperEvidentLog(hub_priv, hub_pub)
    overview = Overview()
    ledger = Ledger()

    att = issue_attestation(
        root_priv, member_pubkey=member_pub, display_name="Alice",
        unit="U-1", role="member", issuer_pubkey=root_pub,
        issued_at="2026-01-01T00:00:00+00:00",
    )
    directory = Directory(attestations=[att])

    pipeline = Pipeline(store=store, directory=directory,
                        translog=pre_restart_translog,
                        overview=overview, ledger=ledger,
                        throttler=Throttler())

    pre_restart_ids: list[tuple[str, int]] = []
    for i in range(4):
        ev = sign_entry(Entry(thread="t1", author=member_pub, kind="post",
                              created_at="2026-01-01T00:00:00Z", body=f"pre-{i}"),
                        member_priv)
        seq = pipeline.accept(ev)
        pre_restart_ids.append((ev.id, seq))
    pre_restart_root = pre_restart_translog.current_sth().root_hash

    # --- restart: store stays on disk; everything in-memory is reborn empty ---
    store2 = EventStore(str(tmp_path / "hub.db"))
    fresh_translog = TamperEvidentLog(hub_priv, hub_pub)
    fresh_overview = Overview()
    fresh_ledger = Ledger()
    fresh_pipeline = Pipeline(store=store2, directory=directory,
                              translog=fresh_translog,
                              overview=fresh_overview, ledger=fresh_ledger,
                              throttler=Throttler())

    assert fresh_translog.current_sth().tree_size == 0   # empty before startup

    app = create_app(pipeline=fresh_pipeline, store=store2,
                     translog=fresh_translog, overview=fresh_overview,
                     ledger=fresh_ledger, directory=directory)

    # TestClient as context manager triggers lifespan startup.
    with TestClient(app) as client:
        sth = STH(**client.get("/sth").json())
        # Size matches the on-disk store.
        assert sth.tree_size == len(pre_restart_ids)
        # Root matches what the pre-restart translog computed: rebuild is
        # equivalent to incremental append for the same input sequence.
        assert sth.root_hash == pre_restart_root
        # Every pre-restart entry is provable under the post-restart STH.
        for entry_id, seq in pre_restart_ids:
            proof = InclusionProof(**client.get(
                "/proof/inclusion", params={"entry": entry_id}
            ).json())
            assert verify_inclusion(entry_id, seq, proof, sth) is True
