"""Entry acceptance pipeline. Spec: server-hub-spec.md §7.1.

Orchestrates the 10 ordered steps. This is the convergence point of ordering,
tamper-evidence, and throttle — build it test-first and review carefully.
"""
from __future__ import annotations

from .entry import Entry, verify_entry
from .identity import Directory
from .index import Overview, Ledger
from .store import EventStore
from .throttle import Throttler, check_structural
from .translog import STH, TamperEvidentLog


class AcceptanceError(Exception):
    """Pipeline rejected an entry before persistence. Carries a short reason.

    ThrottleError (raised by check_structural / throttler) is distinct and
    propagates with its own structured response (§7.2.3) — do not wrap it.
    """


class Pipeline:
    def __init__(self, store: EventStore, directory: Directory, translog: TamperEvidentLog,
                 overview: Overview, ledger: Ledger, throttler: Throttler) -> None:
        self.store = store
        self.directory = directory
        self.translog = translog
        self.overview = overview
        self.ledger = ledger
        self.throttler = throttler

    def accept(self, ev: Entry) -> int:
        """Run the §7.1 pipeline. Return assigned per-thread seq, or raise.

        Steps map 1:1 to server-hub-spec.md §7.1. Order matters: throttle is
        post-auth so it cannot be exhausted by unauthenticated spam (step 5
        after step 4); store.append is before translog.append because the
        store is source of truth and the log is derived from it (step 8).
        """
        # 1. Structural bounds (pre-auth). ThrottleError(scope=structural) on violation.
        check_structural(ev)

        # 2. Resolve author; reject if unknown or revoked.
        att = self.directory.resolve(ev.author)
        if att is None:
            raise AcceptanceError(f"unknown author {ev.author}")
        if self.directory.is_revoked(ev.author):
            raise AcceptanceError(f"revoked author {ev.author}")

        # 3 + 4. Recompute id (must match) and verify sig against author.
        if not verify_entry(ev):
            raise AcceptanceError("id mismatch or signature invalid")

        # 5. Per-identity throttle / quota — author is now known and proven.
        self.throttler.check_and_consume(
            ev.author, att.role, entry_bytes=_entry_bytes(ev)
        )

        # 6. ACL: author may post to thread. Membership entries land in a later slice;
        # for now this is a structural placeholder (a no-op deny would block all posts).
        # TODO(membership-acl): consult overview/membership entries to gate non-members.

        # 7. Parents must exist (or be in flight in the same batch — not modeled yet).
        for p in ev.parents:
            if not self.store.exists(p):
                raise AcceptanceError(f"dangling parent {p}")

        # 8. Assign per-thread seq, persist, extend translog, materialize the new STH.
        # Store-before-log because the entry store is source of truth (§9); the log
        # leaf commits to (id, per-thread seq) so the hub cannot later equivocate
        # about either the entry or the seq it assigned (§6.4.1).
        seq = self.store.next_seq(ev.thread)
        self.store.append(ev, seq)
        self.translog.append(ev.id, seq)
        sth = self.translog.current_sth()

        # 9. Overview index + ledger (receipts only).
        self.overview.add(ev.thread, ev.id, ev.parents, seq)
        if ev.kind == "receipt":
            _apply_receipt(self.ledger, ev, sth)

        # 10. Fan-out is the WebSocket layer's job (api.py); not part of the
        # acceptance contract — return as soon as the entry is committed.
        return seq


def _entry_bytes(ev: Entry) -> int:
    """Approximate on-the-wire entry size for throttle accounting. Blob bytes
    are accounted separately via Throttler.check_and_consume(new_blob_bytes=...)."""
    from . import crypto
    return len(crypto.canonicalize(ev.content()))


def _apply_receipt(ledger: Ledger, ev: Entry, sth: STH) -> None:
    """Receipt body shape lands in a later slice; isolated here so step 9 is clean."""
    # TODO(receipts): parse (recipient, thread, high_water_seq, observed_sth) from ev
    # per §8, then ledger.apply_receipt(...). Until then this is a no-op so a receipt
    # entry can still be accepted into the log.
    return None
