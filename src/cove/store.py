"""Append-only entry store (SQLite). Spec: server-hub-spec.md §9.

Source of truth. Indexed by (thread, seq) and by id. The overview index and the
tamper-evident log are DERIVED from this and rebuildable. Never mutate an entry
in place; edits/revisions are new entries with `supersedes` (§3.3).
"""
from __future__ import annotations

from typing import Iterable, Optional

from .entry import Entry


class EventStore:
    def __init__(self, path: str = "data/hub.db") -> None:
        self._path = path
        # TODO: open SQLite, create tables (entries, blobs metadata, directory, sth, leaves).

    def append(self, ev: Entry, seq: int) -> None:
        """Persist an accepted entry with its assigned per-thread seq. §7.1 step 8."""
        raise NotImplementedError

    def next_seq(self, thread: str) -> int:
        """Monotonic per-thread sequence. §6. Must be assigned atomically with append."""
        raise NotImplementedError

    def get(self, entry_id: str) -> Optional[Entry]:
        raise NotImplementedError

    def since(self, thread: str, seq: int) -> Iterable[Entry]:
        """Delta-sync: entries in thread after `seq`, in seq order. §7 /sync."""
        raise NotImplementedError

    def exists(self, entry_id: str) -> bool:
        raise NotImplementedError
