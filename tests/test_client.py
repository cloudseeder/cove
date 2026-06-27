"""Cove client library — exercises the real wire from outside the FastAPI
TestClient harness, against the real app via httpx's ASGITransport.

The properties pinned here are the joint guarantees the Tauri/TS UI layer
will reimplement against the same hub:

  - auth flow takes a keypair to a session token;
  - sync runs the FULL §5 verification chain on every entry and returns
    VerifiedEntry objects ready for ceremony render — UI never re-implements
    the math;
  - any link in the chain failing raises VerificationError (never silent
    drop) and does NOT advance the high-water seq.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cove import crypto
from cove.api import create_app
from cove.auth import AuthService
from cove.blobs import BlobStore
from cove.client import (
    AuthenticationError, Client, ClientError, VerificationError, VerifiedEntry,
)
from cove.entry import Entry, Receipt, sign_entry
from cove.identity import Directory, issue_attestation, issue_directory
from cove.index import Ledger, Overview
from cove.pipeline import Pipeline
from cove.store import EventStore
from cove.throttle import Throttler
from cove.translog import TamperEvidentLog


@pytest.fixture
def hub(tmp_path, root_keypair, hub_keypair):
    root_priv, root_pub = root_keypair
    hub_priv, hub_pub = hub_keypair
    board_priv, board_pub = crypto.generate_keypair()
    alice_priv, alice_pub = crypto.generate_keypair()

    att_board = issue_attestation(
        root_priv, member_pubkey=board_pub, display_name="Board",
        affiliation="B-1", role="board", issuer_pubkey=root_pub,
        issued_at="2026-01-01T00:00:00+00:00",
    )
    att_alice = issue_attestation(
        root_priv, member_pubkey=alice_pub, display_name="Alice",
        affiliation="U-1", role="member", issuer_pubkey=root_pub,
        issued_at="2026-01-01T00:00:00+00:00",
    )
    manifest = issue_directory(
        root_priv, org=root_pub,
        attestations=[att_board, att_alice], revocations=[],
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
        "app": app, "store": store, "directory": directory,
        "board_priv": board_priv, "board_pub": board_pub,
        "alice_priv": alice_priv, "alice_pub": alice_pub,
    }


def _make_client(app, priv: str, pub: str) -> Client:
    """Wire the Client to the ASGI app via Starlette's TestClient (which IS
    an httpx.Client subclass). The Client treats it like any other HTTP
    transport — same code path as production against a real hub."""
    return Client(hub_url="http://testserver",
                  private_key=priv, public_key=pub,
                  http=TestClient(app))


# ---- auth -----------------------------------------------------------

def test_authenticate_yields_a_session_token(hub):
    c = _make_client(hub["app"], hub["alice_priv"], hub["alice_pub"])
    tok = c.authenticate()
    assert isinstance(tok, str) and len(tok) == 64
    assert c.authenticated is True


def test_authenticate_propagates_failure_as_AuthenticationError(hub):
    other_priv, other_pub = crypto.generate_keypair()    # not in directory
    c = _make_client(hub["app"], other_priv, other_pub)
    with pytest.raises(AuthenticationError):
        c.authenticate()


def test_gated_operations_require_authenticate_first(hub):
    c = _make_client(hub["app"], hub["alice_priv"], hub["alice_pub"])
    with pytest.raises(AuthenticationError):
        c.sync("t1")
    with pytest.raises(AuthenticationError):
        c.fetch_directory()


# ---- directory + STH ------------------------------------------------

def test_fetch_directory_verifies_root_signature(hub):
    c = _make_client(hub["app"], hub["alice_priv"], hub["alice_pub"])
    c.authenticate()
    d = c.fetch_directory()
    assert d.resolve(hub["board_pub"]).role == "board"
    assert d.resolve(hub["alice_pub"]).role == "member"


def test_fetch_sth_verifies_hub_signature(hub):
    c = _make_client(hub["app"], hub["alice_priv"], hub["alice_pub"])
    c.authenticate()
    sth = c.fetch_sth()
    assert sth.tree_size == 0


# ---- sync — the §5 verification chain ------------------------------

def test_sync_returns_verified_entries_with_render_ready_state(hub):
    # Board client posts a notice; alice client syncs it.
    board = _make_client(hub["app"], hub["board_priv"], hub["board_pub"])
    board.authenticate()
    notice = sign_entry(Entry(
        thread="annual-meeting", author=hub["board_pub"], kind="notice",
        created_at="2026-06-15T18:00:00Z",
        body="Annual meeting Wednesday.",
    ), hub["board_priv"])
    board.post(notice)

    alice = _make_client(hub["app"], hub["alice_priv"], hub["alice_pub"])
    alice.authenticate()
    verified = alice.sync("annual-meeting")

    assert len(verified) == 1
    ve = verified[0]
    assert isinstance(ve, VerifiedEntry)
    assert ve.entry.id == notice.id
    assert ve.seq == 0
    # Render-ready state for the ceremony UX:
    assert ve.role == "board"
    assert ve.display_name == "Board"
    assert "Board" in ve.sig_summary
    assert "board" in ve.sig_summary
    assert "inclusion proof position 0" in ve.sig_summary


def test_sync_advances_high_water_only_after_full_success(hub):
    """Partial advancement on failure would silently swallow the
    rejected entry on the next sync. Must advance ONLY when every entry
    in the batch passed verification."""
    board = _make_client(hub["app"], hub["board_priv"], hub["board_pub"])
    board.authenticate()
    for i in range(3):
        board.post(sign_entry(Entry(
            thread="t1", author=hub["board_pub"], kind="post",
            created_at="2026-06-15T18:00:00Z", body=f"m{i}",
        ), hub["board_priv"]))

    alice = _make_client(hub["app"], hub["alice_priv"], hub["alice_pub"])
    alice.authenticate()
    assert alice.high_water("t1") == -1
    verified = alice.sync("t1")
    assert len(verified) == 3
    assert alice.high_water("t1") == 2   # max seq

    # Second sync from the new high-water — empty, high-water unchanged.
    again = alice.sync("t1")
    assert again == []
    assert alice.high_water("t1") == 2


def test_sync_raises_VerificationError_on_tampered_signature(hub, monkeypatch):
    """A real hub doesn't ship tampered sigs, but if any link were to
    fail, the client MUST refuse the entry. Simulate by patching
    verify_entry to return False — the verification chain has to react.
    """
    board = _make_client(hub["app"], hub["board_priv"], hub["board_pub"])
    board.authenticate()
    board.post(sign_entry(Entry(
        thread="t1", author=hub["board_pub"], kind="post",
        created_at="2026-06-15T18:00:00Z", body="real",
    ), hub["board_priv"]))

    alice = _make_client(hub["app"], hub["alice_priv"], hub["alice_pub"])
    alice.authenticate()
    monkeypatch.setattr("cove.client.client.verify_entry", lambda ev: False)
    with pytest.raises(VerificationError, match="id/sig"):
        alice.sync("t1")
    # And high-water is NOT advanced — the rejected entry will be
    # re-attempted on the next sync after the underlying issue is fixed.
    assert alice.high_water("t1") == -1


def test_sync_raises_VerificationError_on_unattested_author(hub, monkeypatch):
    """Defense-in-depth: even if the hub were to accept an entry from
    an unattested key, the client's verification chain MUST reject
    on the directory resolution step."""
    board = _make_client(hub["app"], hub["board_priv"], hub["board_pub"])
    board.authenticate()
    board.post(sign_entry(Entry(
        thread="t1", author=hub["board_pub"], kind="post",
        created_at="2026-06-15T18:00:00Z", body="real",
    ), hub["board_priv"]))

    alice = _make_client(hub["app"], hub["alice_priv"], hub["alice_pub"])
    alice.authenticate()
    # Stub the cached directory so it claims to have no attestation.
    alice.fetch_directory()
    monkeypatch.setattr(alice._directory, "resolve", lambda pk: None)
    with pytest.raises(VerificationError, match="not attested"):
        alice.sync("t1")


def test_sync_raises_VerificationError_on_inclusion_proof_failure(hub, monkeypatch):
    """Even with a valid sig + attested author, an entry that doesn't
    inclusion-prove under the current STH is rejected. This is what
    the §6.4.3 hub-equivocation defense reduces to client-side."""
    board = _make_client(hub["app"], hub["board_priv"], hub["board_pub"])
    board.authenticate()
    board.post(sign_entry(Entry(
        thread="t1", author=hub["board_pub"], kind="post",
        created_at="2026-06-15T18:00:00Z", body="real",
    ), hub["board_priv"]))

    alice = _make_client(hub["app"], hub["alice_priv"], hub["alice_pub"])
    alice.authenticate()
    monkeypatch.setattr("cove.client.client.verify_inclusion",
                        lambda *a, **kw: False)
    with pytest.raises(VerificationError, match="inclusion proof"):
        alice.sync("t1")


# ---- post + receipt -----------------------------------------------

def test_post_signs_and_submits_when_entry_unsigned(hub):
    """The library signs unsigned entries — caller doesn't have to know
    how. (The private key stays in the Client.)"""
    c = _make_client(hub["app"], hub["alice_priv"], hub["alice_pub"])
    c.authenticate()
    # Unsigned entry — id and sig are None.
    ev = Entry(thread="t1", author=hub["alice_pub"], kind="post",
               created_at="2026-06-15T18:00:00Z", body="from helper")
    assert ev.id is None and ev.sig is None
    seq = c.post(ev)
    assert seq == 0
    # The mutation happens in place: ev now has id + sig populated.
    assert ev.id is not None and ev.sig is not None


def test_post_passes_through_an_already_signed_entry(hub):
    c = _make_client(hub["app"], hub["alice_priv"], hub["alice_pub"])
    c.authenticate()
    ev = sign_entry(Entry(
        thread="t1", author=hub["alice_pub"], kind="post",
        created_at="2026-06-15T18:00:00Z", body="pre-signed",
    ), hub["alice_priv"])
    seq = c.post(ev)
    assert seq == 0
    assert hub["store"].exists(ev.id)


def test_post_receipt_builds_signs_and_submits_with_observed_sth(hub):
    """Receipt assembly + sign + post in one call — every UI surface
    needs this and shouldn't be writing Receipt(...) by hand."""
    # Board posts a notice.
    board = _make_client(hub["app"], hub["board_priv"], hub["board_pub"])
    board.authenticate()
    notice = sign_entry(Entry(
        thread="annual-meeting", author=hub["board_pub"], kind="notice",
        created_at="2026-06-15T18:00:00Z", body="notice",
    ), hub["board_priv"])
    notice_seq = board.post(notice)

    # Alice acks via the library helper.
    alice = _make_client(hub["app"], hub["alice_priv"], hub["alice_pub"])
    alice.authenticate()
    sth = alice.fetch_sth()
    alice.post_receipt(
        thread="annual-meeting",
        high_water_seq=notice_seq, observed_sth=sth,
    )

    # The ledger now has alice's ack on the notice.
    # (Pulling /ledger via the raw http client since the helper for that
    # is a follow-up surface.)
    r = board._http.get("/ledger", params={"entry": notice.id})
    assert r.status_code == 200
    body = r.json()
    assert hub["alice_pub"] in body["acked"]


# ---- keyfile loading ----------------------------------------------

def test_from_keyfile_loads_paired_priv_and_pub(tmp_path, hub):
    """Mirrors scripts/gen_keys.py output: alice.priv + alice.pub."""
    basename = str(tmp_path / "alice")
    Path(basename + ".priv").write_text(hub["alice_priv"] + "\n")
    Path(basename + ".pub").write_text(hub["alice_pub"] + "\n")
    c = Client.from_keyfile(hub_url="http://testserver",
                            key_basename=basename,
                            http=TestClient(hub["app"]))
    c.authenticate()
    assert c.authenticated is True
