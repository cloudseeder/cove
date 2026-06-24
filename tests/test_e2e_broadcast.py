"""End-to-end: the broadcast-notice path through the real HTTP API.

This is the joint property no single slice proves on its own — directory
bootstrap, auth, subscribe-then-sync reconciliation, accept-pipeline,
push fan-out, signature + inclusion verification, receipts, ledger
partition, and consistency proofs all have to align for the
'accountable-notice channel' (CLAUDE.md current focus) to actually work.

The test walks one realistic scenario:

  1. Bootstrap: root issues a manifest with board + two members. Hub
     loads it, all modules wire up.
  2. Each member authenticates via /auth/challenge + /auth/verify and
     captures a session token.
  3. Members follow the §4.1 ordering — open WS /stream FIRST, then
     /sync from last-known seq. (The window between subscribe and
     sync is the gap §4.1 closes; we exercise the ordering, not the
     gap.)
  4. Board posts the notice via POST /entries.
  5. Each member receives the notice on /stream (live push delivery).
  6. The same notice is ALSO on /sync — channels overlap by design,
     dedup-by-id is implementable client-side.
  7. Each member verifies origin: signature over canonical content
     plus inclusion proof against the current STH, plus directory
     resolution confirming the author is attested as 'board'. This
     is client-spec §5 verification, end-to-end.
  8. Each member acks via a kind='receipt' entry that carries the
     cumulative high-water seq AND their observed STH (§6.4.3
     equivocation evidence).
  9. Board queries /ledger?entry=<notice_id> and sees both members
     as acked; board itself never acked so appears in not_acked.
 10. Consistency proof from STH(after-notice) to STH(after-receipts)
     verifies the log only grew — append-only over the whole session.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cove import crypto
from cove.api import create_app
from cove.auth import AuthService
from cove.blobs import BlobStore
from cove.entry import BlobRef, Entry, Receipt, sign_entry, verify_entry
from cove.identity import Directory, issue_attestation, issue_directory
from cove.index import Ledger, Overview
from cove.pipeline import Pipeline
from cove.store import EventStore
from cove.throttle import Throttler
from cove.translog import (
    ConsistencyProof, InclusionProof, STH, TamperEvidentLog,
    verify_consistency, verify_inclusion, verify_sth,
)


# ---- helpers ---------------------------------------------------------

def _auth_client(app, priv: str, pub: str) -> TestClient:
    """Authenticate `pub` against `app` and return a client with the
    Authorization header pre-set — every subsequent gated request on
    this client carries the session token."""
    c = TestClient(app)
    ch = c.post("/auth/challenge").json()
    sig = crypto.sign(priv, ch["nonce"].encode())
    tok = c.post("/auth/verify", json={
        "pubkey": pub, "nonce": ch["nonce"], "sig": sig,
    }).json()["token"]
    c.headers["Authorization"] = f"Bearer {tok}"
    return c


def _payload(ev: Entry) -> dict:
    """Wire JSON for an Entry; dataclasses.asdict handles nested
    BlobRef + Receipt for us."""
    from dataclasses import asdict
    return asdict(ev)


def _entry_from_pushed(pushed: dict) -> Entry:
    """Reconstruct a full Entry from a /stream push payload. The
    reconstruction must be FULL-FIDELITY — any field loss invalidates
    the canonical-content hash, which invalidates the signature."""
    e = Entry(
        thread=pushed["thread"], author=pushed["author"],
        kind=pushed["kind"], created_at=pushed["created_at"],
        body=pushed["body"],
        parents=list(pushed["parents"]),
        blobs=[BlobRef(**b) for b in pushed["blobs"]],
        supersedes=pushed["supersedes"],
        receipt=Receipt(**pushed["receipt"]) if pushed.get("receipt") else None,
    )
    e.id = pushed["id"]
    e.sig = pushed["sig"]
    return e


# ---- the e2e test ----------------------------------------------------

def test_broadcast_notice_path_end_to_end(tmp_path, root_keypair, hub_keypair):
    # =========================================================
    # 1. Bootstrap — root issues a manifest, hub wires all modules
    # =========================================================
    root_priv, root_pub = root_keypair
    hub_priv, hub_pub = hub_keypair
    board_priv, board_pub = crypto.generate_keypair()
    alice_priv, alice_pub = crypto.generate_keypair()
    bob_priv, bob_pub = crypto.generate_keypair()

    def _att(pub, name, unit, role):
        return issue_attestation(
            root_priv, member_pubkey=pub, display_name=name,
            unit=unit, role=role, issuer_pubkey=root_pub,
            issued_at="2026-01-01T00:00:00+00:00",
        )

    manifest = issue_directory(
        root_priv, org=root_pub,
        attestations=[
            _att(board_pub, "Board", "B-1", "board"),
            _att(alice_pub, "Alice", "U-1", "member"),
            _att(bob_pub,   "Bob",   "U-2", "member"),
        ],
        revocations=[],
        updated_at="2026-06-01T00:00:00+00:00",
    )

    store = EventStore(str(tmp_path / "hub.db"))
    translog = TamperEvidentLog(hub_priv, hub_pub)
    overview = Overview()
    ledger = Ledger()
    directory = Directory.from_manifest(manifest)
    throttler = Throttler()
    blobs = BlobStore(str(tmp_path / "blobs"))
    pipeline = Pipeline(
        store=store, directory=directory, translog=translog,
        overview=overview, ledger=ledger, throttler=throttler, blobs=blobs,
    )
    auth = AuthService(directory=directory)
    app = create_app(
        pipeline=pipeline, store=store, translog=translog,
        overview=overview, ledger=ledger, directory=directory,
        directory_manifest=manifest, auth=auth, blobs=blobs,
    )

    # =========================================================
    # 2. Auth — each member runs challenge-response and gets a session
    # =========================================================
    board_client = _auth_client(app, board_priv, board_pub)
    alice_client = _auth_client(app, alice_priv, alice_pub)
    bob_client   = _auth_client(app, bob_priv,   bob_pub)

    # =========================================================
    # 3. SUBSCRIBE-then-SYNC (client-spec §4.1 normative ordering)
    # 4. Board posts the notice
    # 5. Members receive on /stream
    # 6. Same notice ALSO on /sync (overlap by design; dedup-by-id)
    # 7. Members verify origin: sig + inclusion proof + directory role
    # =========================================================
    THREAD = "annual-meeting"
    alice_last_seq = bob_last_seq = -1

    with (
        alice_client.websocket_connect("/stream") as alice_ws,
        bob_client.websocket_connect("/stream") as bob_ws,
    ):
        # 3a. Catch-up sync — empty at this point. The ORDER matters
        # (subscribe is already open above): an entry committed during
        # this window would be delivered on the stream, and sync would
        # return it too, and the client dedupes.
        for client, last_seq in [(alice_client, alice_last_seq),
                                 (bob_client, bob_last_seq)]:
            sync = client.get("/sync", params={
                "thread": THREAD, "since": last_seq,
            }).json()
            assert sync["entries"] == []

        # 4. Board posts the notice.
        initial_sth = STH(**board_client.get("/sth").json())
        assert initial_sth.tree_size == 0
        assert verify_sth(initial_sth) is True

        notice = sign_entry(Entry(
            thread=THREAD, author=board_pub, kind="notice",
            created_at="2026-06-15T18:00:00Z",
            body="Annual meeting Wednesday 7pm in the clubhouse.",
        ), board_priv)
        post_resp = board_client.post("/entries", json=_payload(notice))
        assert post_resp.status_code == 200, post_resp.text
        notice_seq = post_resp.json()["seq"]
        assert notice_seq == 0

        # 5. Live-push delivery to every connected subscriber.
        alice_push = alice_ws.receive_json()
        bob_push = bob_ws.receive_json()
        for push in (alice_push, bob_push):
            assert push["type"] == "entry"
            assert push["entry"]["id"] == notice.id
            assert push["seq"] == notice_seq

        # 6. The SAME notice is also visible via /sync — overlap is
        # the property the §4.1 dedup rule relies on. id is the dedup
        # key (content-address; globally unique).
        alice_resync = alice_client.get("/sync", params={
            "thread": THREAD, "since": alice_last_seq,
        }).json()["entries"]
        assert any(e["id"] == notice.id for e in alice_resync)

        # 7. Each member verifies origin end-to-end.
        for push, client in [(alice_push, alice_client),
                             (bob_push,   bob_client)]:
            # 7a. Reconstruct the Entry from the wire payload and check
            # signature over canonical content (client-spec §5 step 3).
            received = _entry_from_pushed(push["entry"])
            assert verify_entry(received) is True

            # 7b. Inclusion proof under the CURRENT STH (§5 step 4).
            sth = STH(**client.get("/sth").json())
            assert verify_sth(sth) is True
            proof = InclusionProof(**client.get(
                "/proof/inclusion", params={"entry": received.id},
            ).json())
            assert verify_inclusion(received.id, push["seq"], proof, sth) is True

            # 7c. Directory resolution: origin is BOARD, not 'some attested
            # member.' This is the 'verified-from-board' render state
            # client-spec §5 step 5 mandates.
            served = client.get("/directory").json()
            att = next(a for a in served["attestations"]
                       if a["member_pubkey"] == received.author)
            assert att["role"] == "board"

    # Snapshot the head AFTER the notice. We'll use it as the from-size
    # in the final consistency proof.
    sth_after_notice = STH(**board_client.get("/sth").json())
    assert sth_after_notice.tree_size == 1

    # =========================================================
    # 8. Each member acks via a kind='receipt' entry carrying the
    # cumulative high-water + their OBSERVED STH (§6.4.3 evidence)
    # =========================================================
    for client, priv, pub in [
        (alice_client, alice_priv, alice_pub),
        (bob_client,   bob_priv,   bob_pub),
    ]:
        observed = STH(**client.get("/sth").json())
        receipt_ev = sign_entry(Entry(
            thread=THREAD, author=pub, kind="receipt",
            created_at="2026-06-15T18:00:30Z", body="",
            receipt=Receipt(
                high_water_seq=notice_seq,
                observed_sth_size=observed.tree_size,
                observed_sth_root=observed.root_hash,
            ),
        ), priv)
        r = client.post("/entries", json=_payload(receipt_ev))
        assert r.status_code == 200, r.text

    # =========================================================
    # 9. Board queries /ledger — the actionable acked / not_acked
    # partition the spec (§8) calls the feature email cannot offer
    # =========================================================
    ledger_resp = board_client.get("/ledger", params={"entry": notice.id})
    assert ledger_resp.status_code == 200
    status = ledger_resp.json()
    assert sorted(status["acked"]) == sorted([alice_pub, bob_pub])
    # The board itself sent the notice but never acked it — that's
    # the expected partition. (A real client would skip self-ack.)
    assert board_pub in status["not_acked"]

    # =========================================================
    # 10. Consistency proof across the rest of the session — the log
    # only grew between the post-notice head and the post-receipts
    # head. §6.4.2 append-only verification.
    # =========================================================
    final_sth = STH(**board_client.get("/sth").json())
    assert final_sth.tree_size == 3   # notice + 2 receipts

    proof = ConsistencyProof(**board_client.get("/proof/consistency", params={
        "from_size": sth_after_notice.tree_size,
        "to_size":   final_sth.tree_size,
    }).json())
    assert verify_consistency(proof, sth_after_notice, final_sth) is True

    # And the receipts' observed STH is preserved in the ledger — the
    # raw material for §6.4.3 split-view detection. Both members
    # observed the SAME (size=1, root=R) head, so equivocation_signals
    # is empty (no equivocation here, just confirming the data made it
    # to the recording layer).
    assert len(ledger.observed_sths(alice_pub, THREAD)) >= 1
    assert len(ledger.observed_sths(bob_pub,   THREAD)) >= 1
    assert ledger.equivocation_signals() == {}
