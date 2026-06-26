"""Entry acceptance pipeline. Spec: server-hub-spec.md §7.1.

Orchestrates the 10 ordered steps. This is the convergence point of ordering,
tamper-evidence, and throttle — build it test-first and review carefully.
"""
from __future__ import annotations

from typing import Optional

from .blobs import BlobStore
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
                 overview: Overview, ledger: Ledger, throttler: Throttler,
                 blobs: Optional[BlobStore] = None) -> None:
        self.store = store
        self.directory = directory
        self.translog = translog
        self.overview = overview
        self.ledger = ledger
        self.throttler = throttler
        # Optional in tests where the entry never references blobs; required
        # in production for the strict step-7 blob-presence check.
        self.blobs = blobs

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

        # 7. References must exist: parents in the entry store, blobs in the
        # blob store. The blob-first ordering is deliberate (client-spec §3):
        # 'whatever the code happens to do' is the worst answer — readers
        # would receive verified entries pointing at 404s, and client authors
        # would each discover the gap differently. Upload bytes, then post
        # the entry that references them.
        for p in ev.parents:
            if not self.store.exists(p):
                raise AcceptanceError(f"dangling parent {p}")
        if self.blobs is not None:
            for b in ev.blobs:
                if not self.blobs.has(b.hash):
                    raise AcceptanceError(f"unstored blob reference {b.hash}")
        # kind=receipt structural check: the cumulative-ack payload must be
        # present, otherwise the receipt has nothing to feed the ledger and
        # is just an opaque marker — refuse it loudly here rather than
        # accept-and-silently-no-op in step 9.
        if ev.kind == "receipt" and ev.receipt is None:
            raise AcceptanceError("receipt entry missing receipt payload")

        # kind=branch structural check: must name a non-empty sub-thread
        # that isn't the current thread (no self-loops). The branch entry
        # is the link between parent and child — without branch_thread it
        # is just an empty post mislabelled.
        if ev.kind == "branch":
            if not ev.branch_thread:
                raise AcceptanceError("branch entry missing branch_thread")
            if ev.branch_thread == ev.thread:
                raise AcceptanceError(
                    f"branch_thread {ev.branch_thread!r} cannot equal thread")

        # 8. Assign per-thread seq, persist, extend translog, materialize the new STH.
        # Store-before-log because the entry store is source of truth (§9); the log
        # leaf commits to (id, per-thread seq) so the hub cannot later equivocate
        # about either the entry or the seq it assigned (§6.4.1).
        # append_atomic gives us (seq, persist) as one operation per §6 — a failed
        # persist cannot burn a seq number. A translog failure AFTER persist is
        # recoverable via TamperEvidentLog.rebuild from store.iter_global.
        seq = self.store.append_atomic(ev)
        self.translog.append(ev.id, seq)
        sth = self.translog.current_sth()
        # Record blob references AFTER the entry is durably in the store
        # (so the ref's entry_id is real). This is the recording layer a
        # future GC will key off — refcount checks, not log-scan archaeology.
        if self.blobs is not None and ev.blobs:
            self.blobs.record_references(ev.id, ev.blobs)

        # 9. Overview index + ledger (receipts only).
        self.overview.add(ev.thread, ev.id, ev.parents, seq,
                          branch_thread=ev.branch_thread)
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
    """Feed a kind='receipt' entry into the ledger. Spec §8.

      - recipient = ev.author (the receipt is authored by who's acking).
      - thread    = ev.thread (cumulative ack is per-thread).
      - high_water_seq = ev.receipt.high_water_seq (TCP-cumulative-ack style).
      - observed_sth   = the (tree_size, root_hash) the recipient SAW when
                         they sent this receipt — not the hub's current head.
                         §6.4.3 equivocation evidence: different roots
                         observed at the same tree_size across recipients
                         is cryptographic proof of split-view, surfaced by
                         Ledger.equivocation_signals().

    The pipeline has already validated that ev.receipt is not None
    (acceptance step above). `sth` is the post-acceptance head; we don't
    use it here — the recipient's OBSERVED sth is the evidentially
    relevant one and lives in ev.receipt.
    """
    r = ev.receipt
    assert r is not None  # acceptance step guarantees this
    ledger.apply_receipt(
        recipient=ev.author,
        thread=ev.thread,
        high_water_seq=r.high_water_seq,
        observed_sth=(r.observed_sth_size, r.observed_sth_root),
    )
