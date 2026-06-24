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


def _bootstrap_hub(tmp_path, root_keypair, hub_keypair, members):
    """Wire every module against a tmp_path-rooted store + blob dir.

    `members` is a list of (pubkey, display_name, unit, role) tuples that
    become attestations in the genesis manifest. Returns a dict of the
    components each test reaches into directly. The wiring itself is
    deliberately uniform so the per-test prose is about the SCENARIO,
    not which Stub/Real combination the bootstrap chose.
    """
    root_priv, root_pub = root_keypair
    hub_priv, hub_pub = hub_keypair
    attestations = [
        issue_attestation(
            root_priv, member_pubkey=pub, display_name=name,
            unit=unit, role=role, issuer_pubkey=root_pub,
            issued_at="2026-01-01T00:00:00+00:00",
        )
        for pub, name, unit, role in members
    ]
    manifest = issue_directory(
        root_priv, org=root_pub,
        attestations=attestations, revocations=[],
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
    return {
        "app": app, "store": store, "translog": translog,
        "overview": overview, "ledger": ledger,
        "directory": directory, "blobs": blobs, "auth": auth,
        "pipeline": pipeline,
    }


def _post_receipt(client: TestClient, *, thread: str, priv: str, pub: str,
                  high_water_seq: int, observed_size: int, observed_root: str,
                  created_at: str = "2026-06-15T18:00:30Z") -> Entry:
    """Sign + POST a kind='receipt' entry; return the signed Entry so the
    caller can introspect it. Asserts 200 — receipt acceptance failures
    in an e2e are the test's bug, not the system's."""
    ev = sign_entry(Entry(
        thread=thread, author=pub, kind="receipt",
        created_at=created_at, body="",
        receipt=Receipt(high_water_seq=high_water_seq,
                        observed_sth_size=observed_size,
                        observed_sth_root=observed_root),
    ), priv)
    r = client.post("/entries", json=_payload(ev))
    assert r.status_code == 200, r.text
    return ev


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
    board_priv, board_pub = crypto.generate_keypair()
    alice_priv, alice_pub = crypto.generate_keypair()
    bob_priv,   bob_pub   = crypto.generate_keypair()
    hub_ = _bootstrap_hub(tmp_path, root_keypair, hub_keypair, members=[
        (board_pub, "Board", "B-1", "board"),
        (alice_pub, "Alice", "U-1", "member"),
        (bob_pub,   "Bob",   "U-2", "member"),
    ])
    app = hub_["app"]
    directory = hub_["directory"]
    ledger = hub_["ledger"]

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
        _post_receipt(client, thread=THREAD, priv=priv, pub=pub,
                      high_water_seq=notice_seq,
                      observed_size=observed.tree_size,
                      observed_root=observed.root_hash)

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


# ---- adversarial scenarios -------------------------------------------
# The happy path proves 'the system delivers when everyone cooperates.'
# The LWCCOA pilot exists because email cannot prove the OPPOSITE — who
# DIDN'T get the notice, and whether the substrate's correctness holds
# when somebody misbehaves. The two tests below cover the half of the
# guarantee a happy-path test silently skips.


def test_silent_member_is_provably_in_not_acked(tmp_path, root_keypair, hub_keypair):
    """The headline LWCCOA claim, walked end-to-end: 'we can prove who
    did NOT receive the notice.' That's the §8 actionable-non-delivery
    list — the feature email structurally cannot offer.

    Scenario: directory has board + three members. Two members
    (alice, bob) auth and ack. The third (carol) never connects — she
    is the SILENT member. Board pulls /ledger and the substrate must
    list carol in `not_acked` based on the absence of a signed receipt
    bound to the notice's seq.

    The legal-grade claim this pins: the entry log proves which
    receipts exist; the directory proves who is attested; the partition
    is mechanical. carol-not-in-acked is not 'we forgot to ask carol';
    it's 'no signed cryptographic ack from carol exists at this thread
    head,' and that statement is verifiable from public data.
    """
    board_priv, board_pub = crypto.generate_keypair()
    alice_priv, alice_pub = crypto.generate_keypair()
    bob_priv,   bob_pub   = crypto.generate_keypair()
    _,          carol_pub = crypto.generate_keypair()    # never participates
    hub_ = _bootstrap_hub(tmp_path, root_keypair, hub_keypair, members=[
        (board_pub, "Board", "B-1", "board"),
        (alice_pub, "Alice", "U-1", "member"),
        (bob_pub,   "Bob",   "U-2", "member"),
        (carol_pub, "Carol", "U-3", "member"),
    ])
    app = hub_["app"]

    # Only alice and bob auth. Carol never does.
    board_client = _auth_client(app, board_priv, board_pub)
    alice_client = _auth_client(app, alice_priv, alice_pub)
    bob_client   = _auth_client(app, bob_priv,   bob_pub)

    THREAD = "annual-meeting"
    notice = sign_entry(Entry(
        thread=THREAD, author=board_pub, kind="notice",
        created_at="2026-06-15T18:00:00Z",
        body="Annual meeting Wednesday 7pm in the clubhouse.",
    ), board_priv)
    notice_seq = board_client.post(
        "/entries", json=_payload(notice),
    ).json()["seq"]

    # Cooperating members ack normally.
    for client, priv, pub in [(alice_client, alice_priv, alice_pub),
                              (bob_client,   bob_priv,   bob_pub)]:
        observed = STH(**client.get("/sth").json())
        _post_receipt(client, thread=THREAD, priv=priv, pub=pub,
                      high_water_seq=notice_seq,
                      observed_size=observed.tree_size,
                      observed_root=observed.root_hash)

    # The actionable partition.
    status = board_client.get("/ledger", params={"entry": notice.id}).json()
    assert sorted(status["acked"]) == sorted([alice_pub, bob_pub])
    # carol is the headline assertion — attested in the manifest, NEVER
    # connected, surfaced in not_acked by mechanical partition over the
    # signed receipts that exist (or don't).
    assert carol_pub in status["not_acked"]
    # board self-author is also in not_acked (didn't ack itself). The
    # client-side render filters self-author; the substrate's partition
    # is mechanical and documents that here.
    assert board_pub in status["not_acked"]

    # The receipts log proves carol's absence is REAL — no entry in the
    # store is authored by carol against this thread, period.
    thread_entries = hub_["store"].since(THREAD, -1)
    assert all(e.author != carol_pub for e in thread_entries)


def test_equivocation_substrate_fires_on_divergent_observed_sths(tmp_path, root_keypair, hub_keypair):
    """§6.4.3 split-view detection wired through the real receipt-entry
    wire path — the deepest cryptographic guarantee and the hardest to
    get right end to end.

    Two members submit signed receipts that ATTEST to having observed
    different STH roots at the same tree_size. In production this
    means either (a) the hub equivocated and showed each member a
    different head, or (b) one member is lying in their signed
    attestation. The protocol does not arbitrate which; it RECORDS the
    signed claims and surfaces the conflict via
    Ledger.equivocation_signals(). The board investigates from there.

    A subtle property under test: the pipeline MUST accept a receipt
    with any observed_sth_root the recipient signs over — the hub does
    not validate the recipient's claim against its own STH history
    (which would require persistent STH storage AND would let a
    fraudulent hub silently suppress evidence by rejecting receipts
    that don't match its current view). A well-intentioned 'validate
    observed_sth' guard in the pipeline would break this; the test
    pins it as a positive requirement, not a property to assume.
    """
    board_priv, board_pub = crypto.generate_keypair()
    alice_priv, alice_pub = crypto.generate_keypair()
    bob_priv,   bob_pub   = crypto.generate_keypair()
    hub_ = _bootstrap_hub(tmp_path, root_keypair, hub_keypair, members=[
        (board_pub, "Board", "B-1", "board"),
        (alice_pub, "Alice", "U-1", "member"),
        (bob_pub,   "Bob",   "U-2", "member"),
    ])
    app = hub_["app"]
    ledger = hub_["ledger"]

    board_client = _auth_client(app, board_priv, board_pub)
    alice_client = _auth_client(app, alice_priv, alice_pub)
    bob_client   = _auth_client(app, bob_priv,   bob_pub)

    THREAD = "annual-meeting"
    notice = sign_entry(Entry(
        thread=THREAD, author=board_pub, kind="notice",
        created_at="2026-06-15T18:00:00Z", body="Notice",
    ), board_priv)
    notice_seq = board_client.post(
        "/entries", json=_payload(notice),
    ).json()["seq"]

    # The HONEST current head, as the hub would actually show it.
    real_sth = STH(**board_client.get("/sth").json())
    real_size = real_sth.tree_size
    real_root = real_sth.root_hash

    # Alice attests to the real head (honest path).
    alice_receipt = _post_receipt(
        alice_client, thread=THREAD, priv=alice_priv, pub=alice_pub,
        high_water_seq=notice_seq,
        observed_size=real_size, observed_root=real_root,
    )

    # Bob attests to a DIVERGENT root at the SAME size — either the
    # hub equivocated and showed bob this other history, or bob is
    # lying about what he saw. The protocol does not judge; it
    # captures the cryptographic conflict.
    fake_root = "f" * 64
    assert fake_root != real_root
    bob_receipt = _post_receipt(
        bob_client, thread=THREAD, priv=bob_priv, pub=bob_pub,
        high_water_seq=notice_seq,
        observed_size=real_size, observed_root=fake_root,
        created_at="2026-06-15T18:00:31Z",
    )

    # The detector fires: ONE tree_size with TWO observed roots is the
    # cryptographic signal of split view.
    signals = ledger.equivocation_signals()
    assert real_size in signals
    assert {real_root, fake_root} <= signals[real_size]

    # Both receipts are durably committed and inclusion-provable — the
    # cryptographic evidence binding each member to their attested
    # observation is preserved. A governance dispute can produce both
    # entries with their signatures intact and let the dispute proceed
    # on the signed record.
    final_sth = STH(**board_client.get("/sth").json())
    for receipt_ev in (alice_receipt, bob_receipt):
        seq = hub_["store"].seq_of(receipt_ev.id)
        proof = InclusionProof(**board_client.get(
            "/proof/inclusion", params={"entry": receipt_ev.id},
        ).json())
        assert verify_inclusion(receipt_ev.id, seq, proof, final_sth) is True

    # The /ledger acked partition is independent of equivocation — both
    # members acked the same high_water_seq, so both still appear in
    # acked. The equivocation evidence is surfaced separately, where it
    # belongs (a governance signal, not an ack signal).
    status = board_client.get("/ledger", params={"entry": notice.id}).json()
    assert sorted(status["acked"]) == sorted([alice_pub, bob_pub])
