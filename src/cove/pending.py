"""Pending-registration registry. v0.4.0 — on-device keygen + push approval.

A member generating a keypair on-device submits POST /pending with their
pubkey + a self-reported name hint, then holds a WebSocket on /pending/watch.
The keymaster sees the queue in the admin UI, verifies the request
(via the channel the QR/link arrived on — see client-spec §X), and issues
a root-signed attestation. The hub matches the attested pubkey against
open watchers and pushes notice; the client unlocks instantly.

Trust model: this module does NOT grant anything. A pending entry is just
routing metadata — "this device is awaiting attestation, push to its
WebSocket when the directory next includes its pubkey." All trust still
flows from the root signature on the attestation. /pending POST is
intentionally public; an attacker submitting a pending entry under
someone else's name fails at the human-verification step (admin checks
the QR's provenance, not just the queue).

This module is API-layer-agnostic: the API wires watcher events into a
FastAPI WebSocket; tests poke them directly. Sync API (register/list/
clear) is callable from threadpool handlers; async signaling is owned
by asyncio.Event so the WS coroutine can `await event.wait()`.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class PendingRegistration:
    """Wire shape of a queued pending request. Carries no secrets and no
    grants — the admin UI displays this, then attestation issues
    elsewhere (out-of-band root-signed)."""
    pubkey: str
    name_hint: str
    requested_at: str   # rfc3339, client-supplied


class PendingRegistry:
    """In-memory pending queue + per-pubkey wake-up events.

    Persistence is intentionally out of scope for the pilot — pending
    state evaporates on hub restart, which is fine: the member's app
    reconnects, re-POSTs /pending, and the admin sees the entry again.
    A persistent registry would also require thinking about queue
    eviction (TTL on stale entries); skipping that until the pilot
    proves it matters.
    """

    def __init__(self) -> None:
        self._entries: dict[str, PendingRegistration] = {}
        # Per-pubkey asyncio.Event. Created lazily; survives clear() so a
        # late mark_attested fires correctly even after the entry was
        # processed.
        self._events: dict[str, asyncio.Event] = {}

    # ---- sync API (admin UI + WS handshake call these) ------------------

    def register(self, pubkey: str, name_hint: str, requested_at: str) -> None:
        """Idempotent on pubkey — re-registering replaces the row.

        A member who corrects a typo in their name shouldn't add a
        duplicate; the admin sees one entry, updated."""
        self._entries[pubkey] = PendingRegistration(
            pubkey=pubkey, name_hint=name_hint, requested_at=requested_at,
        )

    def list(self) -> list[PendingRegistration]:
        """Snapshot, oldest first. Admin processes FIFO unless they
        choose to skip ahead."""
        return sorted(self._entries.values(), key=lambda r: r.requested_at)

    def clear(self, pubkey: str) -> None:
        """Drop a pending entry. Idempotent — clearing a non-pending
        pubkey is a no-op so concurrent admin actions don't race."""
        self._entries.pop(pubkey, None)

    def is_pending(self, pubkey: str) -> bool:
        return pubkey in self._entries

    # ---- async signaling (WS coroutine + admin attest hook use these) ---

    def watcher_event(self, pubkey: str) -> asyncio.Event:
        """Return the wake-up Event for this pubkey, creating one if
        absent. Shared across all watchers of the same pubkey so a single
        mark_attested broadcasts to every open WS for that key (e.g.
        member's phone + laptop both showing the QR)."""
        event = self._events.get(pubkey)
        if event is None:
            event = asyncio.Event()
            self._events[pubkey] = event
        return event

    def mark_attested(self, pubkey: str) -> None:
        """Called by the /admin/attest hook after a successful manifest
        update. Wakes any awaiters and clears the pending entry. Safe to
        call with no awaiters and no pending entry — the directory check
        on the WS handshake is the reconnect-safe path that doesn't go
        through this signal."""
        event = self._events.get(pubkey)
        if event is not None:
            event.set()
        self._entries.pop(pubkey, None)
