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
from cove.identity import (
    Directory, Revocation, hash_manifest,
    issue_attestation, issue_directory,
)
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


def _bootstrap_hub_at(*, paths: dict, root_keypair, hub_keypair, members):
    """Wire every module against a CALLER-PROVIDED set of paths so a test
    can shut the process down and re-bootstrap against the same disk
    layout — the restart-survival case.

    `paths` carries `db`, `blobs`, `manifest_jsonl`. Returns the same
    dict shape as `_bootstrap_hub`.
    """
    root_priv, root_pub = root_keypair
    hub_priv, hub_pub = hub_keypair
    attestations = [
        issue_attestation(
            root_priv, member_pubkey=pub, display_name=name,
            affiliation=unit, role=role, issuer_pubkey=root_pub,
            issued_at="2026-01-01T00:00:00+00:00",
        )
        for pub, name, unit, role in members
    ]
    seed_manifest = issue_directory(
        root_priv, org=root_pub,
        attestations=attestations, revocations=[],
        updated_at="2026-06-01T00:00:00+00:00",
    )
    store = EventStore(str(paths["db"]))
    translog = TamperEvidentLog(hub_priv, hub_pub)
    overview = Overview()
    ledger = Ledger()

    # Bootstrap pattern: load any prior chain from disk; if none, seed
    # with the genesis manifest. Either way, attach persistence so
    # subsequent admin updates write through.
    directory = Directory.load_chain(paths["manifest_jsonl"])
    if directory.manifest is None:
        directory.update_from(seed_manifest)
    directory.attach_persistence(paths["manifest_jsonl"])

    throttler = Throttler()
    blobs = BlobStore(str(paths["blobs"]))
    pipeline = Pipeline(
        store=store, directory=directory, translog=translog,
        overview=overview, ledger=ledger, throttler=throttler, blobs=blobs,
    )
    auth = AuthService(directory=directory)
    app = create_app(
        pipeline=pipeline, store=store, translog=translog,
        overview=overview, ledger=ledger, directory=directory,
        directory_manifest=directory.manifest, auth=auth, blobs=blobs,
    )
    return {
        "app": app, "store": store, "translog": translog,
        "overview": overview, "ledger": ledger,
        "directory": directory, "blobs": blobs, "auth": auth,
        "pipeline": pipeline,
    }


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
            affiliation=unit, role=role, issuer_pubkey=root_pub,
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


def _manifest_dict_for_wire(m) -> dict:
    """Match the api's _manifest_to_dict: include every signed field, or
    the sig fails to verify after a round trip through JSON."""
    from dataclasses import asdict
    return {
        "org": m.org,
        "attestations": [asdict(a) for a in m.attestations],
        "revocations": [asdict(r) for r in m.revocations],
        "updated_at": m.updated_at,
        "prev_manifest_hash": m.prev_manifest_hash,
        "sig": m.sig,
    }


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
        assert any(e["entry"]["id"] == notice.id for e in alice_resync)

        # 7. Each member verifies origin end-to-end.
        for push, client in [(alice_push, alice_client),
                             (bob_push,   bob_client)]:
            # 7a. Reconstruct the Entry from the wire payload and check
            # signature over canonical content (client-spec §5 step 3).
            received = _entry_from_pushed(push["entry"])
            assert verify_entry(received) is True

            # 7b. Inclusion proof under the CURRENT STH (§5 step 4).
            # v0.4.31: /proof/inclusion now bundles the STH atomically
            # with the proof — single round-trip, no race window
            # between separate /sth and /proof/inclusion calls.
            body = client.get(
                "/proof/inclusion", params={"entry": received.id},
            ).json()
            sth = STH(**body.pop("sth"))
            assert verify_sth(sth) is True
            proof = InclusionProof(**body)
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
    for receipt_ev in (alice_receipt, bob_receipt):
        seq = hub_["store"].seq_of(receipt_ev.id)
        # v0.4.31: bundled proof + STH from one atomic snapshot.
        body = board_client.get(
            "/proof/inclusion", params={"entry": receipt_ev.id},
        ).json()
        sth = STH(**body.pop("sth"))
        proof = InclusionProof(**body)
        assert verify_inclusion(receipt_ev.id, seq, proof, sth) is True

    # The /ledger acked partition is independent of equivocation — both
    # members acked the same high_water_seq, so both still appear in
    # acked. The equivocation evidence is surfaced separately, where it
    # belongs (a governance signal, not an ack signal).
    status = board_client.get("/ledger", params={"entry": notice.id}).json()
    assert sorted(status["acked"]) == sorted([alice_pub, bob_pub])


def test_revocation_mid_session_immediately_cuts_off_revoked_member(
        tmp_path, root_keypair, hub_keypair):
    """Revocation lands while a member is in mid-flow — walked end-to-end.

    Setup: board, alice, bob — all attested, all authenticated, all with
    a live session token. Board posts a notice; alice and bob both ack
    via signed receipts; /ledger shows both as acked. Then an admin
    pushes a /admin/revoke with a new root-signed, chain-linked manifest
    that tombstones bob.

    Six invariants pinned end-to-end:

      1. bob's existing session token DIES immediately. The next gated
         request returns 401 because resolve_session checks is_revoked
         on every lookup — the §5 'session bound to a NON-REVOKED
         attested key' guarantee.

      2. bob CANNOT re-authenticate. A fresh /auth/challenge +
         /auth/verify with a valid signature still returns 401, because
         the directory check at verify time sees the revocation.

      3. bob's POST /entries with his old token returns 401 from the
         auth gate BEFORE the pipeline runs. Defense-in-depth — even
         if the auth gate were bypassed, the pipeline's step-2
         directory.is_revoked check would also reject.

      4. §2.3 historical-entry survival. The receipt bob already signed
         is durably in the log, inclusion-proves under the post-revoke
         STH, and verify_entry still passes. is_revoked(bob,
         as_of=receipt.created_at) is False — entries signed before
         revocation remain valid as of the moment they were signed.
         The board's signed paper trail with the now-departed member
         is NOT retroactively invalidated.

      5. /ledger still acks bob. The partition is based on signed
         receipts that exist; it does NOT consult current attestation
         status. The board can answer 'did bob ack this notice?'
         affirmatively forever — the cryptographic record stands
         independently of bob's current membership.

      6. Board and alice are unaffected — their sessions still resolve,
         their gated routes still work, alice can still post.

    Known v1 gap (intentionally NOT asserted here): a WebSocket /stream
    connection bob opened BEFORE the revoke would continue to receive
    pushed entries. The auth check runs at WS connect time only, not
    per-broadcast. Production should either re-check resolve_session
    per-broadcast or close stale connections on revoke; the pilot's
    session TTL (1h) is the operational bound. Pinning the broken
    behavior here would resist a future fix.
    """
    root_priv, root_pub = root_keypair

    # === Bootstrap ===
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
    store = hub_["store"]

    # All three auth and capture live sessions.
    board_client = _auth_client(app, board_priv, board_pub)
    alice_client = _auth_client(app, alice_priv, alice_pub)
    bob_client   = _auth_client(app, bob_priv,   bob_pub)

    THREAD = "annual-meeting"

    # === Board posts notice; both members ack — PRE-revoke evidence ===
    notice = sign_entry(Entry(
        thread=THREAD, author=board_pub, kind="notice",
        created_at="2026-06-15T18:00:00Z",
        body="Annual meeting Wednesday 7pm in the clubhouse.",
    ), board_priv)
    notice_seq = board_client.post(
        "/entries", json=_payload(notice),
    ).json()["seq"]

    receipts = {}
    for name, client, priv, pub in [
        ("alice", alice_client, alice_priv, alice_pub),
        ("bob",   bob_client,   bob_priv,   bob_pub),
    ]:
        observed = STH(**client.get("/sth").json())
        receipts[name] = _post_receipt(
            client, thread=THREAD, priv=priv, pub=pub,
            high_water_seq=notice_seq,
            observed_size=observed.tree_size,
            observed_root=observed.root_hash,
            created_at="2026-06-15T18:00:30Z",
        )
    pre_status = board_client.get(
        "/ledger", params={"entry": notice.id},
    ).json()
    assert sorted(pre_status["acked"]) == sorted([alice_pub, bob_pub])

    # === Admin pushes a chained, root-signed manifest that revokes bob ===
    current = directory.manifest
    rev_bob = Revocation(pubkey=bob_pub,
                         revoked_at="2026-06-15T19:00:00+00:00",
                         reason="key compromise")
    new_manifest = issue_directory(
        root_priv, org=root_pub,
        attestations=list(current.attestations),         # unchanged
        revocations=list(current.revocations) + [rev_bob],
        updated_at="2026-06-15T19:00:01+00:00",
        prev_manifest_hash=hash_manifest(current),
    )
    # /admin/revoke is self-authenticating via the root sig on the
    # manifest — any client can POST.
    admin_resp = board_client.post(
        "/admin/revoke",
        json={"manifest": _manifest_dict_for_wire(new_manifest)},
    )
    assert admin_resp.status_code == 200, admin_resp.text

    # === Invariant 1: bob's existing session is dead immediately ===
    # /directory is public as of v0.4.0; use a gated route to observe
    # the session-invalidation effect.
    r = bob_client.get("/threads")
    assert r.status_code == 401
    assert r.json()["error"] == "auth_required"

    # === Invariant 2: bob cannot re-authenticate ===
    fresh = TestClient(app)
    ch = fresh.post("/auth/challenge").json()
    sig = crypto.sign(bob_priv, ch["nonce"].encode())
    r = fresh.post("/auth/verify", json={
        "pubkey": bob_pub, "nonce": ch["nonce"], "sig": sig,
    })
    assert r.status_code == 401
    assert r.json()["error"] == "auth_failed"

    # === Invariant 3: bob cannot post new entries with his old token ===
    post_revoke_ev = sign_entry(Entry(
        thread=THREAD, author=bob_pub, kind="post",
        created_at="2026-06-15T19:30:00Z", body="should not land",
    ), bob_priv)
    r = bob_client.post("/entries", json=_payload(post_revoke_ev))
    assert r.status_code == 401
    assert store.exists(post_revoke_ev.id) is False

    # === Invariant 4: §2.3 — historical receipt survives ===
    bob_receipt = receipts["bob"]
    assert store.exists(bob_receipt.id)

    # Inclusion-proves under the CURRENT post-revoke STH.
    # v0.4.31: /proof/inclusion bundles the STH from the same snapshot.
    body = board_client.get(
        "/proof/inclusion", params={"entry": bob_receipt.id},
    ).json()
    sth_now = STH(**body.pop("sth"))
    proof = InclusionProof(**body)
    seq = store.seq_of(bob_receipt.id)
    assert verify_inclusion(bob_receipt.id, seq, proof, sth_now) is True

    # The signed content still verifies — the directory revocation does
    # NOT retroactively invalidate signatures from before that time.
    reread = store.get(bob_receipt.id)
    assert verify_entry(reread) is True

    # As-of-time: at the moment bob signed his receipt he was not revoked.
    # Current: he is revoked. Both views co-exist correctly.
    assert directory.is_revoked(bob_pub, as_of=bob_receipt.created_at) is False
    assert directory.is_revoked(bob_pub) is True

    # === Invariant 5: /ledger still acks bob ===
    # The ack partition is the set of signed receipts that exist; it
    # is independent of who's currently attested. bob's signed ack
    # against this notice will remain in 'acked' as long as the
    # receipt entry is in the log.
    post_status = board_client.get(
        "/ledger", params={"entry": notice.id},
    ).json()
    assert sorted(post_status["acked"]) == sorted([alice_pub, bob_pub])

    # === Invariant 6: board and alice are unaffected ===
    assert board_client.get("/directory").status_code == 200
    assert alice_client.get("/directory").status_code == 200
    later = sign_entry(Entry(
        thread=THREAD, author=alice_pub, kind="post",
        created_at="2026-06-15T19:35:00Z", body="discussion item",
    ), alice_priv)
    r = alice_client.post("/entries", json=_payload(later))
    assert r.status_code == 200


def test_hub_state_survives_full_restart(tmp_path, root_keypair, hub_keypair):
    """The lifecycle gap between 'passes tests' and 'survives a restart' —
    closed end-to-end. Simulates a process that:

      1. boots, takes some entries and an admin action, shuts down;
      2. starts again against the same disk layout;
      3. resumes with every guarantee intact — durable AND derived
         state both reconciled before the first request.

    What MUST survive the restart:

      - the entry store (it's on disk — sanity check, not the property
        under test)
      - the translog head (rebuilt from the store via the lifespan
        hook; verify_sth on the post-restart head plus inclusion
        proofs for every pre-restart entry must verify)
      - the overview index (rebuilt from store via the SAME lifespan
        hook; /overview must return the threads it returned before)
      - the directory chain INCLUDING admin updates that landed
        between boots (persisted to JSONL; load_chain re-applies
        through update_from so the chain check + revocation-superset
        rules are re-verified on every load)

    What does NOT survive (by design — operational, transient state
    per §9): session tokens, throttle buckets, fan-out registry,
    /admin/limits overrides. Clients re-authenticate on reconnect.
    """
    paths = {
        "db": tmp_path / "hub.db",
        "blobs": tmp_path / "blobs",
        "manifest_jsonl": tmp_path / "directory.jsonl",
    }

    # === Boot 1: take some entries and an admin action, then shut down ===
    board_priv, board_pub = crypto.generate_keypair()
    alice_priv, alice_pub = crypto.generate_keypair()
    bob_priv,   bob_pub   = crypto.generate_keypair()
    extra_priv, extra_pub = crypto.generate_keypair()       # added mid-life

    hub_a = _bootstrap_hub_at(
        paths=paths, root_keypair=root_keypair, hub_keypair=hub_keypair,
        members=[
            (board_pub, "Board", "B-1", "board"),
            (alice_pub, "Alice", "U-1", "member"),
            (bob_pub,   "Bob",   "U-2", "member"),
        ],
    )
    app_a = hub_a["app"]

    board_client = _auth_client(app_a, board_priv, board_pub)
    alice_client = _auth_client(app_a, alice_priv, alice_pub)
    bob_client   = _auth_client(app_a, bob_priv,   bob_pub)

    THREAD = "annual-meeting"
    notice = sign_entry(Entry(
        thread=THREAD, author=board_pub, kind="notice",
        created_at="2026-06-15T18:00:00Z",
        body="Annual meeting Wednesday 7pm in the clubhouse.",
    ), board_priv)
    notice_seq = board_client.post(
        "/entries", json=_payload(notice),
    ).json()["seq"]

    # Receipts from alice and bob.
    receipts = {}
    for name, client, priv, pub in [
        ("alice", alice_client, alice_priv, alice_pub),
        ("bob",   bob_client,   bob_priv,   bob_pub),
    ]:
        observed = STH(**client.get("/sth").json())
        receipts[name] = _post_receipt(
            client, thread=THREAD, priv=priv, pub=pub,
            high_water_seq=notice_seq,
            observed_size=observed.tree_size,
            observed_root=observed.root_hash,
        )

    # Admin action: extend the directory with a new attestation. This
    # is the durability story that MUST survive — pre-restart admin
    # updates that aren't persisted would be silently lost.
    root_priv, root_pub = root_keypair
    pre_restart_manifest = hub_a["directory"].manifest
    att_extra = issue_attestation(
        root_priv, member_pubkey=extra_pub, display_name="Dana",
        affiliation="U-4", role="member", issuer_pubkey=root_pub,
        issued_at="2026-06-15T18:05:00+00:00",
    )
    next_m = issue_directory(
        root_priv, org=root_pub,
        attestations=list(pre_restart_manifest.attestations) + [att_extra],
        revocations=list(pre_restart_manifest.revocations),
        updated_at="2026-06-15T18:05:01+00:00",
        prev_manifest_hash=hash_manifest(pre_restart_manifest),
    )
    admin_resp = board_client.post("/admin/attest", json={
        "manifest": _manifest_dict_for_wire(next_m),
    })
    assert admin_resp.status_code == 200

    # Capture state for post-restart comparison.
    pre_restart_sth = STH(**board_client.get("/sth").json())
    pre_restart_overview = board_client.get(
        "/overview", params={"thread": THREAD},
    ).json()
    pre_restart_chain_head_hash = hash_manifest(hub_a["directory"].manifest)

    # Shut down — close the DB connection so we know the new boot
    # is reading from disk and not piggybacking on the open handle.
    hub_a["store"].close()
    hub_a["blobs"].close()

    # === Boot 2: same disk, fresh process ===
    hub_b = _bootstrap_hub_at(
        paths=paths, root_keypair=root_keypair, hub_keypair=hub_keypair,
        # The bootstrap's `members` only matters when there's NO chain on
        # disk (genesis case). Here load_chain finds the chain and uses
        # it; the members arg is moot.
        members=[
            (board_pub, "Board", "B-1", "board"),
            (alice_pub, "Alice", "U-1", "member"),
            (bob_pub,   "Bob",   "U-2", "member"),
        ],
    )
    app_b = hub_b["app"]

    # Use TestClient as a context manager so the lifespan startup runs.
    with TestClient(app_b) as client:
        # Re-auth board for the gated read paths.
        ch = client.post("/auth/challenge").json()
        sig = crypto.sign(board_priv, ch["nonce"].encode())
        tok = client.post("/auth/verify", json={
            "pubkey": board_pub, "nonce": ch["nonce"], "sig": sig,
        }).json()["token"]
        client.headers["Authorization"] = f"Bearer {tok}"

        # --- translog rebuilt from store ---
        post_restart_sth = STH(**client.get("/sth").json())
        assert post_restart_sth.tree_size == pre_restart_sth.tree_size
        assert post_restart_sth.root_hash == pre_restart_sth.root_hash
        assert verify_sth(post_restart_sth) is True

        # Inclusion proofs for every pre-restart entry verify under
        # the post-restart head — the rebuild reproduced the same tree.
        # v0.4.31: discard the bundled STH; this test uses post_restart_sth.
        for ev in (notice, receipts["alice"], receipts["bob"]):
            seq = hub_b["store"].seq_of(ev.id)
            body = client.get(
                "/proof/inclusion", params={"entry": ev.id},
            ).json()
            body.pop("sth", None)
            proof = InclusionProof(**body)
            assert verify_inclusion(ev.id, seq, proof, post_restart_sth) is True

        # --- overview rebuilt from store ---
        post_restart_overview = client.get(
            "/overview", params={"thread": THREAD},
        ).json()
        assert post_restart_overview == pre_restart_overview

        # --- directory chain (admin updates) survived ---
        # The chain head matches: the admin /admin/attest from boot 1
        # is on disk and got loaded into the boot 2 chain.
        assert hash_manifest(hub_b["directory"].manifest) == pre_restart_chain_head_hash
        # The new attestation is queryable.
        assert hub_b["directory"].resolve(extra_pub) is not None
        # And the manifest history has both manifests.
        assert len(hub_b["directory"].manifest_history()) == 2

        # --- ledger derives from receipts in the store ---
        # ledger.apply_receipt state is in-memory; we DON'T expect /ledger
        # to be populated post-restart without re-applying receipts from
        # the store. (Ledger reconcile is a separate slice; documented
        # here so the absence isn't mysterious.)
        status = client.get(
            "/ledger", params={"entry": notice.id},
        ).json()
        # Without ledger-reconcile-from-store, acked is empty post-restart.
        # The cryptographic evidence (the receipt entries themselves) is
        # still durable in the store and inclusion-provable.
        assert status["acked"] == []
        for r in (receipts["alice"], receipts["bob"]):
            assert hub_b["store"].exists(r.id)
