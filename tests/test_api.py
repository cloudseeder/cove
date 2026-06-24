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
    Attestation, Directory, Revocation, hash_manifest,
    issue_attestation, issue_directory,
)
from cove.config import TIERS
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
        "root_priv": root_priv, "root_pub": root_pub,
        "revoked_pub": revoked_pub,
        "hub_pub": hub_pub,
        "store": store, "translog": translog,
        "overview": overview, "ledger": ledger,
        "auth": auth, "directory": directory,
        "throttler": throttler,
        "att_member": att_member,
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


# ---- /admin/* (§7, §7.2.2) -------------------------------------------

def _chained(hub, *, attestations, revocations, updated_at):
    """Build a root-signed manifest that chains to the current directory head.
    Convenience for admin tests — exactly what an admin tool would do
    after pulling /directory and computing hash_manifest on the response.
    """
    return issue_directory(
        hub["root_priv"], org=hub["root_pub"],
        attestations=attestations, revocations=revocations,
        updated_at=updated_at,
        prev_manifest_hash=hash_manifest(hub["directory"].manifest),
    )


def test_admin_attest_replaces_directory_with_new_signed_manifest(hub):
    """The admin tool (offline) builds a new manifest including the new
    attestation, chains it via prev_manifest_hash, and root-signs it.
    POST /admin/attest receives the signed manifest; the hub validates
    chain + sig and replaces its in-memory directory. The hub itself
    never touches the root key — non-negotiable #1."""
    current = hub["directory"].manifest
    new_priv, new_pub = crypto.generate_keypair()
    att_new = issue_attestation(
        hub["root_priv"], member_pubkey=new_pub, display_name="Carol",
        unit="U-3", role="member", issuer_pubkey=hub["root_pub"],
        issued_at="2026-06-01T00:00:00+00:00",
    )
    new_manifest = _chained(
        hub,
        attestations=list(current.attestations) + [att_new],
        revocations=list(current.revocations),     # carry forward
        updated_at="2026-06-15T00:00:00+00:00",
    )

    r = hub["client"].post("/admin/attest", json={"manifest": _manifest_dict(new_manifest)})
    assert r.status_code == 200, r.text
    assert r.json()["manifest_hash"] == hash_manifest(new_manifest)

    # The directory now resolves the new member; AuthService — which holds
    # the SAME Directory object — sees the update too (no stale reference).
    assert hub["directory"].resolve(new_pub) is not None
    served = hub["client"].get("/directory").json()
    assert any(a["member_pubkey"] == new_pub for a in served["attestations"])


def test_admin_attest_rejects_unsigned_or_forged_manifest(hub):
    """A manifest the root didn't sign must NOT update the directory.
    Otherwise an attacker hits /admin/attest with their own keys and
    grants themselves an attestation."""
    forged_priv, _ = crypto.generate_keypair()
    forged = issue_directory(
        forged_priv,                                  # signed by NOT-root
        org=hub["root_pub"],                          # claims to be the real org
        attestations=[hub["att_member"]], revocations=[],
        updated_at="2026-06-15T00:00:00+00:00",
    )
    r = hub["client"].post("/admin/attest", json={"manifest": _manifest_dict(forged)})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_signature"


def test_admin_revoke_propagates_through_to_session_gate(hub):
    """End-to-end: admin pushes a revocation via /admin/revoke; the next
    gated request on the previously-valid session is denied. Backs the
    'revocation has immediate effect on the running system' invariant
    from the auth slice."""
    assert hub["client"].get("/directory").status_code == 200

    current = hub["directory"].manifest
    rev = Revocation(pubkey=hub["member_pub"],
                     revoked_at="2026-06-15T00:00:00+00:00",
                     reason="key compromise")
    new_manifest = _chained(
        hub,
        attestations=list(current.attestations),
        revocations=list(current.revocations) + [rev],
        updated_at="2026-06-15T00:00:00+00:00",
    )
    r = hub["client"].post("/admin/revoke", json={"manifest": _manifest_dict(new_manifest)})
    assert r.status_code == 200
    # Same client, same token — now denied.
    assert hub["client"].get("/directory").status_code == 401


def test_admin_attest_missing_payload_returns_400(hub):
    r = hub["client"].post("/admin/attest", json={})
    assert r.status_code == 400


def test_admin_rejects_stale_manifest_so_concurrent_updates_do_not_silently_lose(hub):
    """Two officers, two manifests built from the same starting point;
    whichever POSTs second was built on a stale base. Without chain
    enforcement, the second silently overwrites the first under a valid
    root signature — exactly the failure mode that turns into a
    governance dispute about whose action 'really' happened. The chain
    check turns 'last-writer-silently-wins' into 'stale update rejected,
    admin re-pulls and re-applies'."""
    current = hub["directory"].manifest

    # Two admin actions built atop the SAME prior state.
    carol_priv, carol_pub = crypto.generate_keypair()
    att_carol = issue_attestation(
        hub["root_priv"], member_pubkey=carol_pub, display_name="Carol",
        unit="U-3", role="member", issuer_pubkey=hub["root_pub"],
        issued_at="2026-06-01T00:00:00+00:00",
    )
    dave_priv, dave_pub = crypto.generate_keypair()
    att_dave = issue_attestation(
        hub["root_priv"], member_pubkey=dave_pub, display_name="Dave",
        unit="U-4", role="member", issuer_pubkey=hub["root_pub"],
        issued_at="2026-06-01T00:00:01+00:00",
    )
    m_carol = _chained(
        hub,
        attestations=list(current.attestations) + [att_carol],
        revocations=list(current.revocations),
        updated_at="2026-06-15T00:00:00+00:00",
    )
    m_dave = _chained(
        hub,
        attestations=list(current.attestations) + [att_dave],
        revocations=list(current.revocations),
        updated_at="2026-06-15T00:00:01+00:00",
    )
    # m_carol lands first.
    r1 = hub["client"].post("/admin/attest", json={"manifest": _manifest_dict(m_carol)})
    assert r1.status_code == 200
    # m_dave was built atop the same prior head — now stale. Must be rejected,
    # not silently overwrite Carol.
    r2 = hub["client"].post("/admin/attest", json={"manifest": _manifest_dict(m_dave)})
    assert r2.status_code == 409
    assert r2.json()["error"] == "stale_manifest"
    # The response hands the admin tool the current head so it can rebuild.
    assert r2.json()["current_head"] == hash_manifest(hub["directory"].manifest)
    # State: Carol survives, Dave was never added.
    assert hub["directory"].resolve(carol_pub) is not None
    assert hub["directory"].resolve(dave_pub) is None


def test_admin_rejects_manifest_that_drops_a_prior_revocation(hub):
    """A new manifest that omits a prior tombstone must be rejected.
    Otherwise an admin could silently un-revoke a key by submitting a
    manifest 'cleansed' of an inconvenient revocation — the equivalent
    of editing the audit log."""
    current = hub["directory"].manifest
    extra_priv, extra_pub = crypto.generate_keypair()
    rev_extra = Revocation(pubkey=extra_pub,
                           revoked_at="2026-06-10T00:00:00+00:00",
                           reason="test")
    m_with_rev = _chained(
        hub,
        attestations=list(current.attestations),
        revocations=list(current.revocations) + [rev_extra],
        updated_at="2026-06-10T00:00:01+00:00",
    )
    r1 = hub["client"].post("/admin/revoke", json={"manifest": _manifest_dict(m_with_rev)})
    assert r1.status_code == 200, r1.text

    # Now an admin tries to push a manifest that drops rev_extra.
    head = hub["directory"].manifest
    m_drop = issue_directory(
        hub["root_priv"], org=hub["root_pub"],
        attestations=list(head.attestations),
        revocations=[r for r in head.revocations if r.pubkey != extra_pub],
        updated_at="2026-06-11T00:00:00+00:00",
        prev_manifest_hash=hash_manifest(head),
    )
    r2 = hub["client"].post("/admin/revoke", json={"manifest": _manifest_dict(m_drop)})
    assert r2.status_code == 409
    assert r2.json()["error"] == "revocation_dropped"


def test_directory_manifest_history_is_walkable_and_chains(hub):
    """The chain doubles as audit history: after multiple admin actions,
    every transition is preserved with the root signature that authorized
    it. A governance dispute can walk the chain to see who revoked whom,
    when, and under whose root sig. Each entry's prev_manifest_hash must
    match the prior entry's hash — the chain integrity is verifiable."""
    history_before = len(hub["directory"].manifest_history())

    for i in range(3):
        new_priv, new_pub = crypto.generate_keypair()
        att_new = issue_attestation(
            hub["root_priv"], member_pubkey=new_pub, display_name=f"M{i}",
            unit=f"U-{i}", role="member", issuer_pubkey=hub["root_pub"],
            issued_at=f"2026-06-1{i}T00:00:00+00:00",
        )
        current = hub["directory"].manifest
        m_new = _chained(
            hub,
            attestations=list(current.attestations) + [att_new],
            revocations=list(current.revocations),
            updated_at=f"2026-06-1{i}T00:00:01+00:00",
        )
        assert hub["client"].post("/admin/attest",
                                  json={"manifest": _manifest_dict(m_new)}
                                  ).status_code == 200

    history = hub["directory"].manifest_history()
    assert len(history) == history_before + 3
    # Chain integrity: each manifest commits to its predecessor's hash.
    for i in range(1, len(history)):
        assert history[i].prev_manifest_hash == hash_manifest(history[i - 1])


def test_pre_revocation_entry_remains_verifiable_after_revoke_lands(hub):
    """§2.3: 'events signed before a key's revocation remain verifiable.'
    The revoke test proves FUTURE requests are blocked. This is the other
    half — protecting HISTORY, not restricting the future.

    The pilot's whole audit story depends on this property. If a member
    leaves and we revoke them, the board's signed paper trail with them
    must NOT retroactively become 'unverifiable' — that would let any
    revocation event delete the historical record."""
    # Member signs an entry while still attested.
    pre = sign_entry(Entry(
        thread="t1", author=hub["member_pub"], kind="post",
        created_at="2026-05-01T00:00:00+00:00",     # well before revocation
        body="historical record",
    ), hub["member_priv"])
    assert hub["client"].post("/entries", json=_entry_payload(pre)).status_code == 200

    # Admin revokes the member at a LATER time.
    current = hub["directory"].manifest
    rev = Revocation(pubkey=hub["member_pub"],
                     revoked_at="2026-06-15T00:00:00+00:00",
                     reason="left the board")
    m_rev = _chained(
        hub,
        attestations=list(current.attestations),
        revocations=list(current.revocations) + [rev],
        updated_at="2026-06-15T00:00:01+00:00",
    )
    # Need a fresh client because the member's session is about to die.
    unauth = TestClient(hub["app"])
    assert unauth.post("/admin/revoke",
                       json={"manifest": _manifest_dict(m_rev)}).status_code == 200

    # The directory now reports the member as currently revoked.
    assert hub["directory"].is_revoked(hub["member_pub"]) is True
    # BUT as-of the pre-revocation entry's created_at, they were not.
    assert hub["directory"].is_revoked(
        hub["member_pub"], as_of=pre.created_at) is False
    # And the entry itself still verifies — no fields tampered.
    from cove.entry import verify_entry
    assert verify_entry(pre) is True


# ---- /admin/limits (§7.2.2) ------------------------------------------

def _sign_admin_payload(root_priv: str, payload: dict) -> str:
    return crypto.sign(root_priv, crypto.canonicalize(payload))


def test_admin_limits_applies_tier_override(hub):
    """An override raises the throttle ceiling for a specific identity —
    spec example: 'temporarily raise the board's limit for an
    annual-meeting mailing'. After the override, the identity gets the
    BOARD tier even though their directory role is 'member'."""
    payload = {"pubkey": hub["member_pub"], "tier": "board"}
    body = {"payload": payload,
            "sig": _sign_admin_payload(hub["root_priv"], payload)}
    r = hub["client"].post("/admin/limits", json=body)
    assert r.status_code == 200, r.text

    # The override is now live on the throttler. A member-tier burst+1
    # would normally trip rate; with the board override it must succeed.
    # Drain past the member burst and confirm.
    over_member = TIERS["member"].burst + 1
    for i in range(over_member):
        ev = _signed_post(hub["member_priv"], hub["member_pub"], body=f"m{i}")
        r = hub["client"].post("/entries", json=_entry_payload(ev))
        assert r.status_code == 200, f"member burst {i+1}: {r.text}"


def test_admin_limits_rejects_unsigned_payload(hub):
    payload = {"pubkey": hub["member_pub"], "tier": "board"}
    body = {"payload": payload, "sig": "ff" * 64}     # garbage sig
    r = hub["client"].post("/admin/limits", json=body)
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_signature"


def test_admin_limits_rejects_non_root_signer(hub):
    """A sig from a non-root key (even an attested member) is not enough
    to drive an admin op — admin authority is root authority."""
    payload = {"pubkey": hub["member_pub"], "tier": "board"}
    body = {"payload": payload,
            "sig": crypto.sign(hub["member_priv"],
                               crypto.canonicalize(payload))}
    r = hub["client"].post("/admin/limits", json=body)
    assert r.status_code == 401


def test_admin_limits_rejects_unknown_tier(hub):
    payload = {"pubkey": hub["member_pub"], "tier": "platinum"}
    body = {"payload": payload,
            "sig": _sign_admin_payload(hub["root_priv"], payload)}
    r = hub["client"].post("/admin/limits", json=body)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_tier"


def _manifest_dict(m) -> dict:
    """Convenience for tests — wire-format dict from a DirectoryManifest.
    Must include every signed field, or the sig fails to verify after
    a round trip through JSON."""
    from dataclasses import asdict
    return {
        "org": m.org,
        "attestations": [asdict(a) for a in m.attestations],
        "revocations": [asdict(r) for r in m.revocations],
        "updated_at": m.updated_at,
        "prev_manifest_hash": m.prev_manifest_hash,
        "sig": m.sig,
    }


# ---- stream/sync reconciliation (client-spec §4.1) -------------------

def test_subscribe_then_sync_catches_pre_subscribe_entries(hub):
    """The accept→broadcast seam: an entry committed BEFORE the subscriber
    joined /stream is not in any fan-out snapshot. The client-spec §4.1
    reconciliation contract — subscribe first, then /sync from
    last-known seq — catches it via the sync side. This is the union
    invariant the joint protocol relies on: every committed entry is
    in (stream messages received during this session) ∪ (sync results
    from last-known seq), never neither.
    """
    last_known_seq = -1

    # Entry committed before the client subscribes — models the race
    # window between accept-commit and any later stream subscribe.
    ev = _signed_post(hub["member_priv"], hub["member_pub"], body="pre-subscribe")
    assert hub["client"].post("/entries", json=_entry_payload(ev)).status_code == 200

    # §4.1 ordering: subscribe BEFORE syncing.
    with hub["client"].websocket_connect("/stream") as ws:
        sync = hub["client"].get(
            "/sync", params={"thread": "t1", "since": last_known_seq}).json()
        sync_ids = {e["id"] for e in sync["entries"]}

    # The entry is recovered via the sync channel — the stream had nothing
    # to deliver for an entry committed before this subscriber existed.
    assert ev.id in sync_ids


def test_stream_and_sync_overlap_is_dedupable_by_seq(hub):
    """An entry committed AFTER subscribe arrives on /stream. The same
    entry also shows up in a /sync covering the window (the channels
    overlap by design). The dedup key the spec mandates — (thread, seq)
    — must be identical on both channels for the dedup rule to be
    implementable client-side."""
    with hub["client"].websocket_connect("/stream") as ws:
        ev = _signed_post(hub["member_priv"], hub["member_pub"], body="overlap")
        post_seq = hub["client"].post(
            "/entries", json=_entry_payload(ev)).json()["seq"]

        # Stream side
        stream_msg = ws.receive_json()
        # Sync side covering the same window
        sync_entries = hub["client"].get(
            "/sync", params={"thread": "t1", "since": -1}).json()["entries"]

    assert stream_msg["entry"]["id"] == ev.id
    sync_hit = next(e for e in sync_entries if e["id"] == ev.id)

    # The dedup key MUST be identical on both channels.
    assert stream_msg["seq"] == post_seq
    assert sync_hit["thread"] == ev.thread
    assert stream_msg["entry"]["thread"] == sync_hit["thread"]


def test_broadcast_failure_leaves_entry_durably_in_log(hub, monkeypatch):
    """The asymmetric failure mode the user flagged: pipeline.accept()
    commits durably, then fanout.broadcast() raises. Per client-spec
    §4.1, this is the TOLERABLE failure direction — the entry is
    committed, provable, and recoverable via /sync. The push miss is
    benign so long as the client treats /stream as a latency optimization
    over the log.

    This test pins that property: even with a guaranteed broadcast
    failure, POST /entries durably commits and the entry shows up in
    /sync. The 'log is authoritative' rule is backed by mechanics, not
    just docs.
    """
    from cove.api import FanOut

    async def boom(self, payload):
        raise RuntimeError("simulated push failure")
    monkeypatch.setattr(FanOut, "broadcast", boom)

    # POST surfaces the 500 (no silent drops — broadcast failures show up
    # in the response, just like the spec rule says they're not silent).
    client = TestClient(hub["app"], raise_server_exceptions=False)
    client.headers["Authorization"] = hub["client"].headers["Authorization"]
    ev = _signed_post(hub["member_priv"], hub["member_pub"], body="broadcast-fails")
    r = client.post("/entries", json=_entry_payload(ev))
    assert r.status_code == 500

    # But the entry IS durably in the log — committed before broadcast ran.
    assert hub["store"].exists(ev.id)
    # And recoverable via /sync, which is what the client-spec contract
    # promises clients can rely on.
    sync = client.get("/sync", params={"thread": "t1", "since": -1}).json()
    assert any(e["id"] == ev.id for e in sync["entries"])


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
