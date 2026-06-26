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
from .entry import BlobRef, Entry, Receipt


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

    def append_atomic(self, ev: Entry) -> int:
        """Atomic 'assign next per-thread seq + persist'. Spec §6: 'seq must be
        assigned atomically with append.' Returns the assigned seq.

        If the INSERT fails, the cached next_seq is NOT advanced — a failed
        append cannot burn a seq number and leave a hole in the line. This is
        what the pipeline uses; the two-step (next_seq + append) is kept for
        callers that already hold a seq.
        """
        with self._lock:
            seq = self._next_seq.get(ev.thread, 0)
            self.append(ev, seq)
            self._next_seq[ev.thread] = seq + 1
            return seq

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

    def seq_of(self, entry_id: str) -> Optional[int]:
        """Per-thread seq assigned to an accepted entry, or None if absent.
        Entry objects don't carry seq (it's a store assignment); callers like
        /ledger need it to translate (entry_id) into (thread, required_seq).
        """
        row = self._conn.execute(
            "SELECT seq FROM entries WHERE id=?", (entry_id,)
        ).fetchone()
        return int(row[0]) if row is not None else None

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

    def since_with_seq(self, thread: str, seq: int) -> list[tuple[Entry, int]]:
        """Same as since() but returns (Entry, per-thread seq) pairs.

        Clients need the seq to recompute the leaf hash hash_leaf(id, seq)
        for inclusion-proof verification — the leaf commits to BOTH the
        entry id and its per-thread seq (§6.4.1). /sync uses this to
        return entries enriched with seq.
        """
        rows = self._conn.execute(
            "SELECT id, content, sig, seq FROM entries WHERE thread=? AND seq>?"
            " ORDER BY seq",
            (thread, seq),
        ).fetchall()
        return [(_row_to_entry((r[0], r[1], r[2])), int(r[3])) for r in rows]

    def iter_global(self) -> Iterable[tuple[str, int]]:
        """(entry_id, seq) for every accepted entry, in GLOBAL acceptance order.

        Drives translog rebuild (§9 integrity rule). SQLite's implicit rowid
        is monotonic in insertion order, so ORDER BY rowid IS the acceptance
        sequence — even across threads, even with thread-local seq resetting
        to 0 each thread.
        """
        rows = self._conn.execute(
            "SELECT id, seq FROM entries ORDER BY rowid"
        ).fetchall()
        return [(r[0], int(r[1])) for r in rows]

    def iter_overview_seed(self):
        """(thread, entry_id, parents, seq, branch_thread) for every accepted
        entry, in GLOBAL acceptance order. Drives Overview.rebuild on startup
        (§6 integrity rule). Parents + branch_thread are unpacked from the
        stored canonical content so the overview doesn't need to re-validate.

        branch_thread (v0.2) is present only on kind='branch' entries; None
        for everything else.
        """
        import json
        rows = self._conn.execute(
            "SELECT id, thread, content, seq FROM entries ORDER BY rowid"
        ).fetchall()
        out = []
        for entry_id, thread, content_blob, seq in rows:
            content = json.loads(content_blob)
            parents = content.get("parents", [])
            branch_thread = content.get("branch_thread")
            out.append((thread, entry_id, list(parents), int(seq), branch_thread))
        return out


# ---- (de)serialization --------------------------------------------------
def _row_to_entry(row: tuple) -> Entry:
    """Rebuild an Entry from (id, content_bytes, sig). The content bytes are
    the JCS canonicalization, so re-canonicalizing the rebuilt entry yields
    the same bytes — id and sig remain verifiable."""
    import json
    entry_id, content_blob, sig = row
    content = json.loads(content_blob)
    blobs = [BlobRef(**b) for b in content.pop("blobs", [])]
    receipt_dict = content.pop("receipt", None)
    receipt = Receipt(**receipt_dict) if receipt_dict is not None else None
    ev = Entry(blobs=blobs, receipt=receipt, **content)
    ev.id = entry_id
    ev.sig = sig
    return ev
