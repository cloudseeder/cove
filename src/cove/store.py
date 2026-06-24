"""Append-only entry store (SQLite). Spec: server-hub-spec.md §9.

Source of truth. Indexed by (thread, seq) and by id. The overview index and the
tamper-evident log are DERIVED from this and rebuildable. Never mutate an entry
in place; edits/revisions are new entries with `supersedes` (§3.3).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import asdict
from typing import Iterable, Optional

from . import crypto
from .entry import BlobRef, Entry


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id          TEXT PRIMARY KEY,
    thread      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    author      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    content     BLOB NOT NULL,    -- JCS bytes of entry.content() (everything but id, sig)
    sig         TEXT NOT NULL,
    UNIQUE(thread, seq)
);
CREATE INDEX IF NOT EXISTS idx_entries_thread_seq ON entries(thread, seq);
"""


class EventStore:
    def __init__(self, path: str = "data/hub.db") -> None:
        self._path = path
        if path != ":memory:":
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        # check_same_thread=False so the WebSocket/HTTP workers can share one
        # connection; we serialize next_seq → append via _lock below.
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()
        self._next_seq: dict[str, int] = {}
        self._warm_seq_cache()

    def _warm_seq_cache(self) -> None:
        cur = self._conn.execute("SELECT thread, MAX(seq) FROM entries GROUP BY thread")
        for thread, max_seq in cur.fetchall():
            self._next_seq[thread] = max_seq + 1

    def close(self) -> None:
        self._conn.close()

    # ---- writes ---------------------------------------------------------
    def next_seq(self, thread: str) -> int:
        """Monotonic per-thread sequence. §6. Reserved under a lock so the
        pipeline's next_seq → append pair is race-free within this process.
        """
        with self._lock:
            s = self._next_seq.get(thread, 0)
            self._next_seq[thread] = s + 1
            return s

    def append(self, ev: Entry, seq: int) -> None:
        """Persist an accepted entry with its assigned per-thread seq. §7.1 step 8.

        Append-only: the PRIMARY KEY on id and the UNIQUE (thread, seq) catch
        any attempt to overwrite. The pipeline has already validated the entry;
        the store enforces that an accepted entry lands exactly once.
        """
        content_bytes = crypto.canonicalize(ev.content())
        self._conn.execute(
            "INSERT INTO entries (id, thread, seq, author, kind, created_at, content, sig)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ev.id, ev.thread, seq, ev.author, ev.kind, ev.created_at,
             content_bytes, ev.sig),
        )

    # ---- reads ----------------------------------------------------------
    def get(self, entry_id: str) -> Optional[Entry]:
        row = self._conn.execute(
            "SELECT id, content, sig FROM entries WHERE id=?", (entry_id,)
        ).fetchone()
        return _row_to_entry(row) if row is not None else None

    def exists(self, entry_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM entries WHERE id=? LIMIT 1", (entry_id,)
        ).fetchone() is not None

    def since(self, thread: str, seq: int) -> Iterable[Entry]:
        """Delta-sync: entries in thread strictly AFTER `seq`, in seq order. §7 /sync.

        Pass seq=-1 (or any value < 0) to walk the thread from the beginning.
        Materialized into a list so the caller can iterate without holding a
        live SQLite cursor.
        """
        rows = self._conn.execute(
            "SELECT id, content, sig FROM entries WHERE thread=? AND seq>?"
            " ORDER BY seq",
            (thread, seq),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]


# ---- (de)serialization --------------------------------------------------
def _row_to_entry(row: tuple) -> Entry:
    """Rebuild an Entry from (id, content_bytes, sig). The content bytes are
    the JCS canonicalization, so re-canonicalizing the rebuilt entry yields
    the same bytes — id and sig remain verifiable."""
    import json
    entry_id, content_blob, sig = row
    content = json.loads(content_blob)
    blobs = [BlobRef(**b) for b in content.pop("blobs", [])]
    ev = Entry(blobs=blobs, **content)
    ev.id = entry_id
    ev.sig = sig
    return ev
