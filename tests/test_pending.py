"""Pending-registration registry. v0.4.0 — on-device keygen + push approval.

Spec: when a member generates a keypair on-device, they register a pending
request (POST /pending), then hold a WebSocket on /pending/watch until the
keymaster issues an attestation. The hub's role is to:
  1. Hold the queue of pending registrations so the admin UI can list them.
  2. Match a newly-attested pubkey against open watchers and push notice.
  3. Stay reconnect-safe: a client whose WS dropped during attestation,
     reconnecting later, must get the same push immediately on handshake
     — they shouldn't have to know to re-poll.

The registry itself is pure-Python; the API layer wires its watcher events
to the FastAPI WebSocket. Sync API (register/list/clear) is callable from
threadpool handlers; async signaling (mark_attested → asyncio.Event) is
awaited by the WS coroutine.
"""
from __future__ import annotations

import asyncio

import pytest

from cove.pending import PendingRegistry


def test_register_then_list_returns_request():
    r = PendingRegistry()
    r.register(pubkey="aa" * 32, name_hint="Jane Doe",
               requested_at="2026-06-27T12:00:00+00:00")
    rows = r.list()
    assert len(rows) == 1
    assert rows[0].pubkey == "aa" * 32
    assert rows[0].name_hint == "Jane Doe"
    assert rows[0].requested_at == "2026-06-27T12:00:00+00:00"


def test_register_is_idempotent_on_pubkey():
    """A member who resubmits (e.g. typo in name_hint corrected) should
    update the existing row, not duplicate. Otherwise the queue fills with
    stale entries the admin has to scroll past."""
    r = PendingRegistry()
    r.register(pubkey="aa" * 32, name_hint="Jane (typo)",
               requested_at="2026-06-27T12:00:00+00:00")
    r.register(pubkey="aa" * 32, name_hint="Jane Doe",
               requested_at="2026-06-27T12:01:00+00:00")
    rows = r.list()
    assert len(rows) == 1
    assert rows[0].name_hint == "Jane Doe"
    assert rows[0].requested_at == "2026-06-27T12:01:00+00:00"


def test_clear_drops_a_pending_entry():
    r = PendingRegistry()
    r.register(pubkey="aa" * 32, name_hint="Jane",
               requested_at="2026-06-27T12:00:00+00:00")
    r.register(pubkey="bb" * 32, name_hint="Bob",
               requested_at="2026-06-27T12:00:00+00:00")
    r.clear("aa" * 32)
    rows = r.list()
    assert len(rows) == 1
    assert rows[0].pubkey == "bb" * 32


def test_clear_unknown_pubkey_is_noop():
    """Admin rejecting an already-cleared (e.g. just-attested) pubkey should
    not raise — concurrent admin actions are fine."""
    r = PendingRegistry()
    r.clear("aa" * 32)
    assert r.list() == []


def test_is_pending_reflects_state():
    r = PendingRegistry()
    assert r.is_pending("aa" * 32) is False
    r.register(pubkey="aa" * 32, name_hint="x", requested_at="2026-06-27T12:00:00+00:00")
    assert r.is_pending("aa" * 32) is True
    r.clear("aa" * 32)
    assert r.is_pending("aa" * 32) is False


def test_list_sorted_by_requested_at_ascending():
    """Oldest requests first — admin processes FIFO unless they choose otherwise."""
    r = PendingRegistry()
    r.register(pubkey="bb" * 32, name_hint="Bob",
               requested_at="2026-06-27T12:01:00+00:00")
    r.register(pubkey="aa" * 32, name_hint="Alice",
               requested_at="2026-06-27T12:00:00+00:00")
    r.register(pubkey="cc" * 32, name_hint="Carol",
               requested_at="2026-06-27T12:02:00+00:00")
    rows = r.list()
    assert [row.pubkey for row in rows] == ["aa" * 32, "bb" * 32, "cc" * 32]


# ---- async signaling -----------------------------------------------------

@pytest.mark.asyncio
async def test_mark_attested_wakes_an_awaiter():
    """A watcher awaiting attestation must be woken when mark_attested
    fires for its pubkey. This is the WebSocket push primitive."""
    r = PendingRegistry()
    pubkey = "aa" * 32
    r.register(pubkey=pubkey, name_hint="Jane",
               requested_at="2026-06-27T12:00:00+00:00")
    event = r.watcher_event(pubkey)

    async def attest_after_delay():
        await asyncio.sleep(0.01)
        r.mark_attested(pubkey)

    task = asyncio.create_task(attest_after_delay())
    await asyncio.wait_for(event.wait(), timeout=1.0)
    await task

    # And the pending entry is cleared — once attested, it's not pending.
    assert r.is_pending(pubkey) is False


@pytest.mark.asyncio
async def test_mark_attested_wakes_all_awaiters_for_same_pubkey():
    """Two devices for the same pending pubkey (member's phone + laptop,
    both showing the QR) must both wake. asyncio.Event broadcast semantics."""
    r = PendingRegistry()
    pubkey = "aa" * 32
    r.register(pubkey=pubkey, name_hint="Jane",
               requested_at="2026-06-27T12:00:00+00:00")
    event_a = r.watcher_event(pubkey)
    event_b = r.watcher_event(pubkey)
    assert event_a is event_b, "shared Event so both awaiters wake on one set"

    r.mark_attested(pubkey)
    assert event_a.is_set() and event_b.is_set()


@pytest.mark.asyncio
async def test_watcher_event_for_unknown_pubkey_creates_one():
    """A reconnecting client whose pending entry was cleared (already
    attested) may still call watcher_event — it must return a fresh,
    not-yet-set Event so the WS handler can immediately check
    'is this pubkey attested?' and push without waiting."""
    r = PendingRegistry()
    event = r.watcher_event("aa" * 32)
    assert event.is_set() is False


@pytest.mark.asyncio
async def test_mark_attested_for_unwatched_pubkey_is_noop():
    """Admin attests a pubkey nobody is watching (member closed app):
    must not raise. The reconnect-safe path covers them via the
    handshake-time directory check, not via an Event."""
    r = PendingRegistry()
    r.mark_attested("aa" * 32)   # no event registered
    assert True   # didn't raise
