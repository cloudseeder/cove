"""Overview index + delivery ledger. Spec: server-hub-spec.md §6, §8.

DERIVED and rebuildable from the entry store (§6 integrity rule). The simulation
showed naive thread reconstruction grows linearly with total log size while an
index keeps it sub-millisecond — so the index is mandatory, but disposable.
"""
from __future__ import annotations

from typing import Iterable, Optional


class Overview:
    """Per-thread child map + seq order for fast flat/threaded rendering. §6.

    Storage shape:
      - _threads[thread][entry_id] = (seq, parents)   # per-thread roll
      - _children[parent_id]       = [child_id, ...]  # global child map
                                                       (entry_ids are globally unique
                                                        content-addresses, so no
                                                        thread keying needed here)
    """

    def __init__(self) -> None:
        self._threads: dict[str, dict[str, tuple[int, list[str]]]] = {}
        self._children: dict[str, list[str]] = {}

    def add(self, thread: str, entry_id: str, parents: list[str], seq: int) -> None:
        self._threads.setdefault(thread, {})[entry_id] = (seq, list(parents))
        for p in parents:
            self._children.setdefault(p, []).append(entry_id)

    def children(self, entry_id: str) -> list[str]:
        return list(self._children.get(entry_id, []))

    def thread_entries(self, thread: str) -> list[tuple[str, int, list[str]]]:
        """All entries in this thread as (entry_id, seq, parents), in seq order.

        Insertion order is NOT trusted (rebuilds may feed entries out of order);
        we sort on read. At pilot scale this is sub-millisecond per thread.
        """
        entries = self._threads.get(thread, {})
        rows = [(eid, seq, list(parents)) for eid, (seq, parents) in entries.items()]
        rows.sort(key=lambda r: r[1])
        return rows

    def rebuild(self, entries: Iterable[tuple[str, str, list[str], int]]) -> None:
        """Rebuild from raw entries (the integrity escape hatch). §6.

        Accepts (thread, entry_id, parents, seq) tuples — what `add` takes,
        batched. Clears prior state so a stale cache cannot contaminate the
        rebuild.
        """
        self._threads.clear()
        self._children.clear()
        for thread, entry_id, parents, seq in entries:
            self.add(thread, entry_id, parents, seq)


class Ledger:
    """Delivery ledger derived from receipt entries. Spec §8.

    Cumulative high-water receipts (§8): a receipt acks 'thread C through seq N'.
    Surface BOTH who has acked and who has NOT (the actionable non-delivery
    list — email's silent void becomes a list).

    Also retains observed STH heads carried on receipts (§6.4.3) so the same
    tree_size showing different root_hashes across recipients is detectable as
    equivocation evidence.
    """

    def __init__(self) -> None:
        # (recipient, thread) -> highest acked seq (-1 sentinel for 'never')
        self._high_water: dict[tuple[str, str], int] = {}
        # (recipient, thread) -> set of observed (tree_size, root_hash) pairs
        self._observed_sths: dict[tuple[str, str], set[tuple[int, str]]] = {}

    def apply_receipt(self, recipient: str, thread: str, high_water_seq: int,
                      observed_sth: tuple[int, str]) -> None:
        """Apply a (recipient, thread, high-water) receipt.

        observed_sth=(tree_size, root_hash) is retained for equivocation
        detection (§6.4.3). Stale receipts (lower seq than the current
        high-water) DO NOT decrement — cumulative-ack semantics, like
        TCP/NNTP. We still record the observed STH from the stale receipt:
        the STH is evidence about hub history regardless of how far the
        recipient had caught up at that moment.
        """
        key = (recipient, thread)
        existing = self._high_water.get(key, -1)
        if high_water_seq > existing:
            self._high_water[key] = high_water_seq
        self._observed_sths.setdefault(key, set()).add(observed_sth)

    def high_water(self, recipient: str, thread: str) -> int:
        """Highest seq the recipient has acked in this thread; -1 if none."""
        return self._high_water.get((recipient, thread), -1)

    def status(self, thread: str, *, required_seq: int,
               members: Iterable[str]) -> dict:
        """{'acked': [...], 'not_acked': [...]} for a given broadcast position. §8.

        A member is 'acked' iff their high-water in `thread` is at or past
        `required_seq`. The not_acked list is the actionable one — that's the
        feature email structurally cannot offer.
        """
        acked, not_acked = [], []
        for m in members:
            (acked if self.high_water(m, thread) >= required_seq else not_acked).append(m)
        return {"acked": acked, "not_acked": not_acked}

    def observed_sths(self, recipient: str, thread: str) -> set[tuple[int, str]]:
        """All (tree_size, root_hash) heads this recipient has reported observing."""
        return set(self._observed_sths.get((recipient, thread), set()))

    def equivocation_signals(self) -> dict[int, set[str]]:
        """{tree_size: {root_hash, ...}} for every tree_size where >1 root has
        been observed across all recipients/threads.

        Per §6.4.3: 'Two valid STHs at the same tree_size with different
        root_hash are cryptographic proof the hub equivocated.' This surfaces
        the candidate cases; cryptographic confirmation (verifying both STH
        signatures against the pinned hub key) is the caller's job.
        """
        by_size: dict[int, set[str]] = {}
        for sths in self._observed_sths.values():
            for size, root in sths:
                by_size.setdefault(size, set()).add(root)
        return {size: roots for size, roots in by_size.items() if len(roots) > 1}
