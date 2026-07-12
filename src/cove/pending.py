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

v0.5.1: durable across hub restart. Pre-v0.5.1 pending was pure
in-memory; a restart wiped the queue and the admin had to wait for each
prospective member to reopen their app + re-POST /pending before the row
came back. Now backed by SQLite (same file as EventStore + VaultStore +
InviteRegistry, distinct table). Constructor takes an optional db_path;
in-memory only when omitted (test convenience). The async Event dict
stays in-memory — those Events wake WebSockets in the CURRENT process
and are meaningless after a restart anyway (WSes died with the process).
"""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from typing import Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending (
  pubkey       TEXT PRIMARY KEY,
  name_hint    TEXT NOT NULL,
  requested_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class PendingRegistration:
    """Wire shape of a queued pending request. Carries no secrets and no
    grants — the admin UI displays this, then attestation issues
    elsewhere (out-of-band root-signed)."""
    pubkey: str
    name_hint: str
    requested_at: str   # rfc3339, client-supplied


class PendingRegistry:
    """Pending queue + per-pubkey wake-up events.

    v0.5.1: pass `db_path` for persistence. The pending-registration
    dict is persisted so a restart doesn't force every prospective
    member to reopen their app before the admin sees them again. The
    per-pubkey asyncio.Event dict stays in-memory — those Events are
    for waking WebSockets in the CURRENT process; after a restart the
    WS reconnects and re-fetches the event via watcher_event().
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._entries: dict[str, PendingRegistration] = {}
        # Per-pubkey asyncio.Event. Created lazily; survives clear() so a
        # late mark_attested fires correctly even after the entry was
        # processed.
        self._events: dict[str, asyncio.Event] = {}
        self._conn: Optional[sqlite3.Connection] = None
        if db_path is not None:
            self._conn = sqlite3.connect(
                db_path, check_same_thread=False, isolation_level=None,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._load_from_disk()

    def _load_from_disk(self) -> None:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT pubkey, name_hint, requested_at FROM pending"
        ).fetchall()
        for pubkey, hint, requested in rows:
            self._entries[pubkey] = PendingRegistration(
                pubkey=pubkey, name_hint=hint, requested_at=requested,
            )

    # ---- sync API (admin UI + WS handshake call these) ------------------

    def register(self, pubkey: str, name_hint: str, requested_at: str) -> None:
        """Idempotent on pubkey — re-registering replaces the row.

        A member who corrects a typo in their name shouldn't add a
        duplicate; the admin sees one entry, updated."""
        self._entries[pubkey] = PendingRegistration(
            pubkey=pubkey, name_hint=name_hint, requested_at=requested_at,
        )
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO pending"
                " (pubkey, name_hint, requested_at) VALUES (?, ?, ?)",
                (pubkey, name_hint, requested_at),
            )

    def list(self) -> list[PendingRegistration]:
        """Snapshot, oldest first. Admin processes FIFO unless they
        choose to skip ahead."""
        return sorted(self._entries.values(), key=lambda r: r.requested_at)

    def clear(self, pubkey: str) -> None:
        """Drop a pending entry. Idempotent — clearing a non-pending
        pubkey is a no-op so concurrent admin actions don't race."""
        self._entries.pop(pubkey, None)
        if self._conn is not None:
            self._conn.execute("DELETE FROM pending WHERE pubkey = ?", (pubkey,))

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
        if self._conn is not None:
            self._conn.execute("DELETE FROM pending WHERE pubkey = ?", (pubkey,))
