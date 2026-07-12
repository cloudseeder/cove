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
from .audience import authorize_audience_change
from .entry import Audience, Ballot, BlobRef, Entry, Receipt, Vote


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

-- v0.4.37: ephemeral thread registry. A thread appears here iff it was
-- opened via POST /threads/ephemeral; anything not in this table is a
-- permanent thread and follows the main tamper-evident log. Entries
-- themselves still live in the `entries` table above — the routing
-- decision is a lookup against this registry, not a column on the entry.
--
-- v0.4.38: `tombstone_entry_content` + `tombstone_entry_sig` are the
-- creator's PRE-SIGNED tombstone Entry (kind='tombstone',
-- tombstone_valid_after = created_at + ttl_seconds). Held so the
-- auto-seal path can present a member-signed tombstone at TTL
-- expiration without any live member keypair on the hub. Manual seal
-- (POST /threads/{T}/tombstone) accepts a fresh tombstone entry
-- with valid_after ≤ now, overriding this one.
CREATE TABLE IF NOT EXISTS ephemeral_threads (
    thread                   TEXT PRIMARY KEY,
    creator_pubkey           TEXT NOT NULL,
    created_at               TEXT NOT NULL,      -- RFC 3339 UTC
    ttl_seconds              INTEGER NOT NULL,
    tombstone_entry_content  BLOB NOT NULL,      -- JCS bytes of the tombstone Entry's content()
    tombstone_entry_sig      TEXT NOT NULL,      -- creator signature over that JCS
    tombstoned_at            TEXT DEFAULT NULL    -- set at seal ceremony
);

-- v0.4.38: sealed ephemeral STHs. Populated at seal time; the row
-- outlives the underlying ephemeral entries (which are deleted). Any
-- member who kept a copy of an entry can prove it was in the tree by
-- reconstructing the leaf and running verify_inclusion_ephemeral
-- against this STH.
CREATE TABLE IF NOT EXISTS ephemeral_final_sths (
    thread          TEXT PRIMARY KEY,
    tree_size       INTEGER NOT NULL,
    root_hash       TEXT NOT NULL,
    prev_sth_hash   TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    hub_key         TEXT NOT NULL,
    sig             TEXT NOT NULL
);
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

    def latest_non_receipt(self, thread: str) -> Optional[tuple[Entry, int]]:
        """v0.4.19: latest user-facing entry in a thread for inbox preview.

        Receipts have body='' by spec (§8) and are noise in an 'inbox row'
        preview — skip them and return the latest entry a reader would
        actually want to see in the thread. Returns None if the thread is
        receipt-only (or empty).
        """
        row = self._conn.execute(
            "SELECT id, content, sig, seq FROM entries"
            " WHERE thread=? AND kind!='receipt'"
            " ORDER BY seq DESC LIMIT 1",
            (thread,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_entry((row[0], row[1], row[2])), int(row[3])

    def thread_audience(
        self, thread: str,
        has_manage_audience=None,
    ) -> Optional[Audience]:
        """v0.4.27 + v0.5.0: compute the current audience scope for a thread.

        Walks kind='audience' entries oldest-first, applying the shared
        `authorize_audience_change` rule at each step:
          - Bootstrap: first entry establishes the audience (any author).
          - Subsequent: accepted iff (a) author was in the running
            audience AND (b) if the change removes anyone other than the
            author, the author had `manage_audience` at the time.

        This is defense-in-depth: the pipeline gate rejects unauthorized
        writes before they land, but a hub bug could conceivably let one
        slip past. The read layer must not surface an unauthorized change
        as if it had been accepted.

        `has_manage_audience` is a callable (author_pubkey) -> bool. When
        omitted (legacy callers that don't care about the diff-gate
        defense — e.g. bulk tests), the check reduces to the pre-v0.5.0
        author-in-audience rule.
        """
        rows = self._conn.execute(
            "SELECT id, content, sig FROM entries"
            " WHERE thread=? AND kind='audience'"
            " ORDER BY seq",
            (thread,),
        ).fetchall()
        current: Optional[Audience] = None
        for row in rows:
            ev = _row_to_entry(row)
            # Defensive: a kind='audience' entry without an audience
            # field shouldn't exist (pipeline rejects it), but a
            # malformed test fixture could; just skip it.
            if ev.audience is None:
                continue
            new_pubkeys = ev.audience.pubkeys
            if current is None:
                current = ev.audience
                continue
            reason = authorize_audience_change(
                old=current.pubkeys,
                new=new_pubkeys,
                author=ev.author,
                caller_has_manage_audience=(
                    has_manage_audience or (lambda _pk: False)
                ),
            )
            if reason is None:
                current = ev.audience
            # else: ignore — the pipeline should have rejected this write;
            # if it landed anyway, the read layer refuses to honor it.
        return current

    def caller_last_audience_seq(
        self, thread: str, caller: str,
        has_manage_audience=None,
    ) -> Optional[int]:
        """v0.5.0: for the /sync grace-period fix — return the seq of the
        last entry the caller is entitled to see in this thread.

        - Caller currently in audience → the thread's current head seq
          (or None to signal "no clamp, full history").
        - Caller was in some historical audience and has since been
          removed → the seq of the audience entry that first removed
          them (inclusive — they get to see their own removal, which is
          the final visible entry).
        - Caller never in audience → None (returns empty from the caller).

        Callers use the ternary shape (never_in, currently_in, removed)
        by combining this with `caller in thread_audience().pubkeys`.
        """
        rows = self._conn.execute(
            "SELECT id, content, sig, seq FROM entries"
            " WHERE thread=? AND kind='audience'"
            " ORDER BY seq",
            (thread,),
        ).fetchall()
        current: Optional[Audience] = None
        ever_in = False
        for eid, content_blob, sig, _seq in rows:
            ev = _row_to_entry((eid, content_blob, sig))
            if ev.audience is None:
                continue
            new_pubkeys = ev.audience.pubkeys
            if current is None:
                current = ev.audience
                if caller in new_pubkeys:
                    ever_in = True
                continue
            reason = authorize_audience_change(
                old=current.pubkeys,
                new=new_pubkeys,
                author=ev.author,
                caller_has_manage_audience=(
                    has_manage_audience or (lambda _pk: False)
                ),
            )
            if reason is not None:
                continue
            was_in = caller in current.pubkeys
            now_in = caller in new_pubkeys
            current = ev.audience
            if now_in:
                ever_in = True
                continue
            if was_in and not now_in:
                # Caller was just removed. Their /sync clamp is this seq
                # (inclusive — they get to see the removal entry itself).
                return int(_seq)
        if current is None or caller in current.pubkeys:
            # Bootstrap-less thread OR caller is still in current audience.
            return None
        if not ever_in:
            # Caller was never in the audience. Preserves the pre-v0.5.0
            # "audience-scoped thread returns empty to non-audience callers"
            # behavior via the callsite that checks `caller in audience`.
            return None
        # Ever-in but removed AND we didn't hit the removal above. Should
        # be unreachable given the return in the loop, but stays defensive.
        return None

    def threads_with_audience(
        self,
        has_manage_audience=None,
    ) -> dict[str, Audience]:
        """v0.4.27 + v0.5.0: bulk equivalent of thread_audience for the
        /threads and /inbox list endpoints. Same defense-in-depth as
        thread_audience — a hub-bug-smuggled audience entry doesn't
        surface here either.
        """
        rows = self._conn.execute(
            "SELECT thread, id, content, sig, seq FROM entries"
            " WHERE kind='audience'"
            " ORDER BY thread, seq"
        ).fetchall()
        out: dict[str, Audience] = {}
        for thread, eid, content_blob, sig, _seq in rows:
            ev = _row_to_entry((eid, content_blob, sig))
            if ev.audience is None:
                continue
            current = out.get(thread)
            if current is None:
                out[thread] = ev.audience
                continue
            reason = authorize_audience_change(
                old=current.pubkeys,
                new=ev.audience.pubkeys,
                author=ev.author,
                caller_has_manage_audience=(
                    has_manage_audience or (lambda _pk: False)
                ),
            )
            if reason is None:
                out[thread] = ev.audience
        return out

    def archive_state_per_thread(self, has_archive_capability) -> dict[str, bool]:
        """v0.4.25: per-thread archived/active state for /threads + /inbox.

        For each thread that has any archive|reopen entries, walk them
        newest-first and pick the latest one whose author had the
        `archive` capability at the time of evaluation. If that latest
        qualifying entry's kind is 'archive', the thread is archived;
        if it's 'reopen' (or none qualifies), the thread is active.

        `has_archive_capability` is a callable (author_pubkey) -> bool —
        the API layer passes a closure over Directory.caller_capabilities
        so the rule the hub enforces is exactly the rule clients see.

        Returns ONLY threads with a positive archive state — callers
        treat absence as "active." Keeps the response small for orgs
        that haven't archived anything.
        """
        rows = self._conn.execute(
            "SELECT thread, kind, author, seq FROM entries"
            " WHERE kind IN ('archive', 'reopen')"
            " ORDER BY seq DESC"
        ).fetchall()
        # Walk per-thread; first qualifying author wins (newest-first).
        seen: set[str] = set()
        out: dict[str, bool] = {}
        for thread, kind, author, _seq in rows:
            if thread in seen:
                continue
            if not has_archive_capability(author):
                continue
            seen.add(thread)
            if kind == "archive":
                out[thread] = True
        return out

    def caller_receipt_high_water(self, thread: str, author: str) -> int:
        """v0.4.19: max seq of a caller's kind='receipt' entries in a thread,
        or -1 if the caller has never receipted this thread.

        Inbox uses this against thread.latest_seq to render the unread
        indicator. We compare the seq of the receipt entry itself (not the
        receipt's high_water_seq payload) — the seq is what every other
        consumer of /sync uses as the read cursor, so this stays consistent
        with how clients already paginate.
        """
        row = self._conn.execute(
            "SELECT MAX(seq) FROM entries WHERE thread=? AND author=? AND kind='receipt'",
            (thread, author),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else -1

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

    def search_entries(
        self, term: str, limit: int = 200,
    ) -> list[tuple[Entry, int]]:
        """v0.5.2: substring search over post/reply/notice bodies + thread
        names. Returns raw (Entry, seq) matches, newest-first by created_at,
        capped at `limit`. Audience visibility is NOT filtered here — the
        API layer applies the same `_caller_sync_clamp` logic used by
        /sync so removed members don't see anything past their removal.

        Content is stored as JCS bytes; we cast to TEXT and LIKE-match
        (case-insensitive via LOWER). This can produce false positives on
        long hex pubkeys that contain the term, but at pilot scale (<10K
        entries) that's noise a snippet obviously not-from-body will
        make readable. If precision ever matters, swap for FTS5 —
        interface stays the same.
        """
        term = term.strip()
        if not term:
            return []
        pattern = f"%{term.lower()}%"
        rows = self._conn.execute(
            "SELECT id, content, sig, seq FROM entries"
            " WHERE kind IN ('post', 'reply', 'notice')"
            "   AND (LOWER(CAST(content AS TEXT)) LIKE ?"
            "        OR LOWER(thread) LIKE ?)"
            " ORDER BY created_at DESC"
            " LIMIT ?",
            (pattern, pattern, limit),
        ).fetchall()
        return [(_row_to_entry((r[0], r[1], r[2])), int(r[3])) for r in rows]

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

    # ---- ephemeral registry (v0.4.37) ----------------------------------
    def open_ephemeral(
        self, *, thread: str, creator_pubkey: str, created_at: str,
        ttl_seconds: int, tombstone_entry_content: bytes,
        tombstone_entry_sig: str,
    ) -> None:
        """Register a thread as ephemeral. Rejects re-opening an existing
        thread — a name in `entries` (permanent) OR in `ephemeral_threads`
        cannot be re-typed. Callers are expected to have verified the
        tombstone entry's signature before this call."""
        row = self._conn.execute(
            "SELECT 1 FROM ephemeral_threads WHERE thread=? LIMIT 1", (thread,),
        ).fetchone()
        if row is not None:
            raise ValueError(f"ephemeral thread {thread!r} already exists")
        row = self._conn.execute(
            "SELECT 1 FROM entries WHERE thread=? LIMIT 1", (thread,),
        ).fetchone()
        if row is not None:
            raise ValueError(
                f"thread {thread!r} already has permanent entries — cannot re-open as ephemeral",
            )
        self._conn.execute(
            "INSERT INTO ephemeral_threads"
            " (thread, creator_pubkey, created_at, ttl_seconds,"
            "  tombstone_entry_content, tombstone_entry_sig)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (thread, creator_pubkey, created_at, ttl_seconds,
             tombstone_entry_content, tombstone_entry_sig),
        )

    def is_ephemeral(self, thread: str) -> bool:
        """True iff the thread was opened as ephemeral AND has not been
        tombstoned. Used by the pipeline to route entries to the per-
        thread translog."""
        row = self._conn.execute(
            "SELECT 1 FROM ephemeral_threads WHERE thread=? AND tombstoned_at IS NULL LIMIT 1",
            (thread,),
        ).fetchone()
        return row is not None

    def is_tombstoned(self, thread: str) -> bool:
        """v0.4.49: True iff the thread name was opened as ephemeral AND
        has since been tombstoned. The pipeline uses this to REFUSE
        further writes to a sealed thread — without the check, new posts
        would land in the main log next to the tombstone entry, as if
        the name had never been used. That betrays the "sealed" promise
        the user made when they deleted the thread."""
        row = self._conn.execute(
            "SELECT 1 FROM ephemeral_threads WHERE thread=? AND tombstoned_at IS NOT NULL LIMIT 1",
            (thread,),
        ).fetchone()
        return row is not None

    def get_ephemeral(self, thread: str) -> Optional[dict]:
        """Full ephemeral thread record, or None. Includes tombstoned_at
        (nullable). Used by the API layer for the /threads listing +
        the auto-seal loop's TTL check."""
        row = self._conn.execute(
            "SELECT thread, creator_pubkey, created_at, ttl_seconds,"
            "  tombstone_entry_content, tombstone_entry_sig, tombstoned_at"
            " FROM ephemeral_threads WHERE thread=?",
            (thread,),
        ).fetchone()
        if row is None:
            return None
        return {
            "thread": row[0],
            "creator_pubkey": row[1],
            "created_at": row[2],
            "ttl_seconds": int(row[3]),
            "tombstone_entry_content": row[4],
            "tombstone_entry_sig": row[5],
            "tombstoned_at": row[6],
        }

    def all_ephemeral(self) -> list[dict]:
        """Every ephemeral thread row (tombstoned or live). Drives
        EphemeralTransLog rebuild on startup — the pipeline needs each
        live thread's chain restored before it can accept new entries."""
        rows = self._conn.execute(
            "SELECT thread, creator_pubkey, created_at, ttl_seconds,"
            "  tombstone_entry_content, tombstone_entry_sig, tombstoned_at"
            " FROM ephemeral_threads",
        ).fetchall()
        return [
            {
                "thread": r[0],
                "creator_pubkey": r[1],
                "created_at": r[2],
                "ttl_seconds": int(r[3]),
                "tombstone_entry_content": r[4],
                "tombstone_entry_sig": r[5],
                "tombstoned_at": r[6],
            }
            for r in rows
        ]

    def mark_tombstoned(self, thread: str, tombstoned_at: str) -> None:
        """Set tombstoned_at on the ephemeral_threads row. Idempotent
        for retry-safety — a later timestamp does not overwrite an
        earlier one (the first successful seal wins)."""
        self._conn.execute(
            "UPDATE ephemeral_threads SET tombstoned_at=?"
            " WHERE thread=? AND tombstoned_at IS NULL",
            (tombstoned_at, thread),
        )

    def save_final_sth(self, *, thread: str, tree_size: int, root_hash: str,
                       prev_sth_hash: str, timestamp: str, hub_key: str,
                       sig: str) -> None:
        """Pin the sealed ephemeral STH. INSERT OR IGNORE so a retry after
        a mid-flight error doesn't rewrite the sealed head — the first
        write wins, matching mark_tombstoned's semantics."""
        self._conn.execute(
            "INSERT OR IGNORE INTO ephemeral_final_sths"
            " (thread, tree_size, root_hash, prev_sth_hash, timestamp, hub_key, sig)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (thread, tree_size, root_hash, prev_sth_hash, timestamp, hub_key, sig),
        )

    def get_final_sth(self, thread: str) -> Optional[dict]:
        """Sealed ephemeral STH for a tombstoned thread, or None. Any
        member who kept a copy of an entry proves inclusion by
        re-hashing the leaf and running verify_inclusion_ephemeral
        against this STH."""
        row = self._conn.execute(
            "SELECT tree_size, root_hash, prev_sth_hash, timestamp, hub_key, sig"
            " FROM ephemeral_final_sths WHERE thread=?",
            (thread,),
        ).fetchone()
        if row is None:
            return None
        return {
            "thread": thread,
            "tree_size": int(row[0]),
            "root_hash": row[1],
            "prev_sth_hash": row[2],
            "timestamp": row[3],
            "hub_key": row[4],
            "sig": row[5],
        }

    def delete_ephemeral_entries(self, thread: str) -> int:
        """Delete every entry row for a tombstoned ephemeral thread.
        Returns the number of rows removed. The seal ceremony calls
        this after mark_tombstoned + save_final_sth so a mid-flight
        crash leaves either 'not tombstoned yet' or 'tombstoned +
        sealed STH pinned, entries gone' — never a torn state where
        the STH is missing but the leaves are gone."""
        cur = self._conn.execute(
            "DELETE FROM entries WHERE thread=?", (thread,),
        )
        # v0.4.37: iter_global excludes tombstoned threads from main-
        # log rebuild via the WHERE clause that references
        # ephemeral_threads. Once tombstoned_at is set the WHERE
        # excludes only live ephemeral. Leaf deletion here removes the
        # per-thread cache-warm seq material — reset it to 0 so a
        # tombstone entry (published shortly after via the main log)
        # lands at seq 0 in the now-empty thread.
        self._next_seq.pop(thread, None)
        return cur.rowcount

    def iter_ephemeral_entries(self, thread: str) -> list[tuple[str, int]]:
        """(entry_id, seq) for every accepted entry in an ephemeral thread,
        in seq order. Drives EphemeralTransLog.rebuild(thread, …) on startup."""
        rows = self._conn.execute(
            "SELECT id, seq FROM entries WHERE thread=? ORDER BY seq",
            (thread,),
        ).fetchall()
        return [(r[0], int(r[1])) for r in rows]

    def iter_global(self) -> Iterable[tuple[str, int]]:
        """(entry_id, seq) for every accepted entry in a PERMANENT thread,
        in GLOBAL acceptance order.

        Drives main translog rebuild (§9 integrity rule). SQLite's implicit
        rowid is monotonic in insertion order, so ORDER BY rowid IS the
        acceptance sequence — even across threads, even with thread-local
        seq resetting to 0 each thread.

        v0.4.37: ephemeral-thread entries are excluded. Those live in the
        per-thread ephemeral translog (see iter_ephemeral_entries) and
        must not tangle the main tree — cross-tree substitution is
        exactly what the per-thread STH binding is defending against.
        """
        rows = self._conn.execute(
            "SELECT id, seq FROM entries"
            " WHERE thread NOT IN (SELECT thread FROM ephemeral_threads)"
            " ORDER BY rowid"
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
    # v0.4.27: audience field is conditionally present in canonical
    # content. Older entries don't have the key at all (Entry.content
    # strips it when None); newer entries with kind='audience' carry
    # an {pubkeys: [...]} dict.
    audience_dict = content.pop("audience", None)
    audience = Audience(**audience_dict) if audience_dict is not None else None
    # v0.6.0: ballot + vote fields carry the same
    # byte-identical-when-absent rule. Rehydrate to dataclasses so
    # pipeline.accept()'s ballot/vote validation (which reads
    # `.options`, `.ballot_id`, etc.) doesn't crash with
    # AttributeError on 'dict' when validating a vote that references
    # a ballot fetched via store.get(). Skipped for pre-v0.6.0 entries
    # (they never have these keys).
    ballot_dict = content.pop("ballot", None)
    ballot = Ballot(**ballot_dict) if ballot_dict is not None else None
    vote_dict = content.pop("vote", None)
    vote = Vote(**vote_dict) if vote_dict is not None else None
    ev = Entry(blobs=blobs, receipt=receipt, audience=audience,
               ballot=ballot, vote=vote, **content)
    ev.id = entry_id
    ev.sig = sig
    return ev
