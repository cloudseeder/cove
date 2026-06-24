"""Overview index + delivery ledger. Spec: server-hub-spec.md §6, §8.

DERIVED and rebuildable from the entry store (§6 integrity rule). The simulation
showed naive thread reconstruction grows linearly with total log size while an
index keeps it sub-millisecond — so the index is mandatory, but disposable.
"""
from __future__ import annotations

from typing import Iterable


class Overview:
    """Per-thread child map + seq order for fast flat/threaded rendering. §6."""

    def add(self, thread: str, entry_id: str, parents: list[str], seq: int) -> None:
        raise NotImplementedError

    def children(self, entry_id: str) -> list[str]:
        raise NotImplementedError

    def rebuild(self, entries: Iterable) -> None:
        """Rebuild from raw entries (the integrity escape hatch). §6."""
        raise NotImplementedError


class Ledger:
    """Delivery ledger derived from receipt entries. Spec §8.

    Cumulative high-water receipts (§8): a receipt acks 'thread C through seq N'.
    Surface BOTH who has acked and who has NOT (the actionable non-delivery list).
    """

    def apply_receipt(self, recipient: str, thread: str, high_water_seq: int,
                      observed_sth: tuple[int, str]) -> None:
        """observed_sth=(tree_size, root_hash) enables equivocation detection (§6.4.3)."""
        raise NotImplementedError

    def status(self, message_event_id: str) -> dict:
        """-> {'acked': [...], 'not_acked': [...]} for a given broadcast/message. §8."""
        raise NotImplementedError
