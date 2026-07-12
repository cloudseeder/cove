"""Entry acceptance pipeline. Spec: server-hub-spec.md §7.1.

Orchestrates the 10 ordered steps. This is the convergence point of ordering,
tamper-evidence, and throttle — build it test-first and review carefully.
"""
from __future__ import annotations

from typing import Optional

from datetime import datetime, timezone

from .audience import MANAGE_AUDIENCE_CAP, authorize_audience_change
from .blobs import BlobStore
from .entry import Entry, verify_entry


def _parse_rfc3339_utc(ts: str) -> datetime:
    """v0.6.0: strict-ish RFC3339 parse for ballot closes_at. Accepts
    the trailing 'Z' Cove clients emit (new Date().toISOString()) plus
    explicit `+00:00`. Returns an aware UTC datetime.

    Kept local to pipeline.py rather than in crypto/util to avoid
    surfacing a general 'ISO parse' seam that would tempt callers to
    rely on this loose acceptance shape. Ballot timestamps are the
    only place we need it."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed")
    return dt.astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
from .identity import Directory
from .index import Overview, Ledger
from .store import EventStore
from .throttle import Throttler, check_structural
from .translog import STH, TamperEvidentLog
from .translog_ephemeral import EphemeralSTH, EphemeralTransLog


class AcceptanceError(Exception):
    """Pipeline rejected an entry before persistence. Carries a short reason.

    ThrottleError (raised by check_structural / throttler) is distinct and
    propagates with its own structured response (§7.2.3) — do not wrap it.
    """


# v0.4.37: entry kinds allowed inside an ephemeral thread. Everything not
# in this set is a governance/permanent-shape kind and gets rejected
# structurally when directed at an ephemeral thread.
#
# v0.4.48: `audience` moved into the allowed set. Audience is per-thread
# routing (who receives pushes / who /sync surfaces the thread to), not
# governance. In an ephemeral thread the audience entry lives in the
# per-thread ephemeral log and dies with the thread; nothing about it
# leaks or persists after tombstone. The prior blanket rejection made
# "group ephemeral threads" structurally impossible — a real gap for the
# recital-with-just-3-people use case.
_EPHEMERAL_ALLOWED_KINDS = frozenset({"post", "reply", "receipt", "audience"})


class Pipeline:
    def __init__(self, store: EventStore, directory: Directory, translog: TamperEvidentLog,
                 overview: Overview, ledger: Ledger, throttler: Throttler,
                 blobs: Optional[BlobStore] = None,
                 ephemeral_translog: Optional[EphemeralTransLog] = None) -> None:
        self.store = store
        self.directory = directory
        self.translog = translog
        self.overview = overview
        self.ledger = ledger
        self.throttler = throttler
        # Optional in tests where the entry never references blobs; required
        # in production for the strict step-7 blob-presence check.
        self.blobs = blobs
        # v0.4.37: ephemeral log. When None the pipeline still refuses
        # entries directed at ephemeral threads (structural rejection is
        # store-driven), so a caller that forgot to wire this up gets
        # loud errors rather than silent single-tree accumulation.
        self.ephemeral_translog = ephemeral_translog

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

        # v0.5.0: audience-change write-side gate. Prior to v0.5.0 the pipeline
        # accepted every signed audience entry and the "author must be in
        # current audience" rule was enforced silently at read time
        # (store.thread_audience filtered ineligible entries). That violated
        # non-negotiable #5 (no silent failures) — the client got HTTP 200
        # for a mutation the hub was going to ignore. Now the diff-gate lives
        # here and returns a structured reason on rejection. Spec §3.x.
        if ev.kind == "audience":
            if ev.audience is None:
                raise AcceptanceError("audience entry missing audience payload")
            current = self.store.thread_audience(ev.thread)
            old = current.pubkeys if current is not None else None
            reason = authorize_audience_change(
                old=old,
                new=ev.audience.pubkeys,
                author=ev.author,
                caller_has_manage_audience=lambda pk: (
                    MANAGE_AUDIENCE_CAP in self.directory.caller_capabilities(pk)
                ),
            )
            if reason is not None:
                raise AcceptanceError(reason)

        # v0.4.49: refuse writes to a tombstoned thread. Without this,
        # posts to a sealed thread name would silently land in the main
        # log next to the tombstone entry as if the name had never been
        # used — betraying the "sealed" promise the user made when they
        # deleted the thread. Reject BEFORE seq allocation so a rejected
        # attempt cannot burn a seq number.
        if self.store.is_tombstoned(ev.thread):
            raise AcceptanceError(
                f"thread {ev.thread!r} is tombstoned — no further writes accepted",
            )

        # v0.4.37: ephemeral-thread routing decision + structural kind gate.
        # Consulted before the translog write so the gate is store-driven
        # and can't be bypassed by a pipeline caller that forgot to wire
        # the ephemeral translog. Reject BEFORE seq allocation so a
        # governance-kind attempt doesn't burn a seq number.
        is_ephemeral = self.store.is_ephemeral(ev.thread)
        if is_ephemeral and ev.kind not in _EPHEMERAL_ALLOWED_KINDS:
            raise AcceptanceError(
                f"kind {ev.kind!r} not permitted in ephemeral thread {ev.thread!r}",
            )
        if is_ephemeral and self.ephemeral_translog is None:
            # Loud: the store says the thread is ephemeral but the
            # pipeline was constructed without the ephemeral log wired
            # up. Refuse rather than accepting into a mystery void.
            raise AcceptanceError(
                f"ephemeral thread {ev.thread!r} has no ephemeral translog wired",
            )

        # v0.5.3: reject empty-content posts. A post/reply/notice/supersede
        # with no body AND no blobs is unrenderable — it appears in the
        # feed as a blank row (Brooks hit one in `flood-recovery`).
        # Attachment-only entries are legitimate (a PDF share with no
        # commentary), so the rule is "body OR blobs, not neither."
        # v0.6.0: `ballot` joins the list — the question lives in body,
        # so an empty-body ballot is unrenderable.
        if ev.kind in ("post", "reply", "notice", "supersede", "ballot"):
            if not ev.body.strip() and not ev.blobs:
                raise AcceptanceError("empty_body")

        # v0.5.3: supersede (edit) authorization. A supersede entry MUST
        # (a) name a real prior entry via `supersedes`, (b) live in the
        # same thread as its target, and (c) share the same author.
        # Without (c) anyone could rewrite anyone else's posts —
        # accountability is what makes the log useful. Same-thread (b)
        # prevents a supersede-across-threads that would confuse the
        # "walk the thread to find the latest body" client rule.
        if ev.kind == "supersede":
            if not ev.supersedes:
                raise AcceptanceError("supersede_missing_target")
            target = self.store.get(ev.supersedes)
            if target is None:
                raise AcceptanceError("supersede_target_unknown")
            if target.thread != ev.thread:
                raise AcceptanceError("supersede_wrong_thread")
            if target.author != ev.author:
                raise AcceptanceError("supersede_wrong_author")

        # v0.6.0: ballot validation. Options must be non-empty + unique
        # (voters reference options by index; duplicate labels confuse
        # the tally UI). closes_at must parse as RFC3339 UTC and be in
        # the future. The question lives in ev.body — the empty-body
        # gate already covers "no question."
        if ev.kind == "ballot":
            if ev.ballot is None:
                raise AcceptanceError("ballot_missing_payload")
            opts = ev.ballot.options
            if not opts or any(not o.strip() for o in opts):
                raise AcceptanceError("ballot_options_empty")
            if len(set(opts)) != len(opts):
                raise AcceptanceError("ballot_options_duplicate")
            if not ev.ballot.closes_at:
                raise AcceptanceError("ballot_missing_closes_at")
            try:
                closes = _parse_rfc3339_utc(ev.ballot.closes_at)
            except ValueError:
                raise AcceptanceError("ballot_bad_closes_at") from None
            now = _now_utc()
            if closes <= now:
                raise AcceptanceError("ballot_closes_in_past")

        # v0.6.0: vote validation. Must reference a real ballot in the
        # same thread, name an in-range option, and land before the
        # ballot closes. Voter must be in the ballot's thread audience
        # at accept time — the general audience gate (via /sync) is
        # sufficient because non-audience members can't see the ballot
        # to vote on it in the first place; the pipeline check makes
        # sure a stale-cached UI can't sneak a vote in.
        if ev.kind == "vote":
            if ev.vote is None:
                raise AcceptanceError("vote_missing_payload")
            ballot_entry = self.store.get(ev.vote.ballot_id)
            if ballot_entry is None or ballot_entry.kind != "ballot":
                raise AcceptanceError("vote_ballot_unknown")
            if ballot_entry.thread != ev.thread:
                raise AcceptanceError("vote_wrong_thread")
            if ballot_entry.ballot is None:
                raise AcceptanceError("vote_ballot_malformed")
            idx = ev.vote.option_index
            if not isinstance(idx, int) or idx < 0 or idx >= len(ballot_entry.ballot.options):
                raise AcceptanceError("vote_option_out_of_range")
            try:
                closes = _parse_rfc3339_utc(ballot_entry.ballot.closes_at)
            except ValueError:
                raise AcceptanceError("vote_ballot_malformed") from None
            if _now_utc() > closes:
                raise AcceptanceError("vote_ballot_closed")
            # Audience gate — silent-empty if the caller isn't allowed
            # to see the thread would defeat the whole point.
            current = self.store.thread_audience(ev.thread)
            if current is not None and ev.author not in current.pubkeys:
                raise AcceptanceError("vote_not_in_audience")

        # 8. Assign per-thread seq, persist, extend translog, materialize the new STH.
        # Store-before-log because the entry store is source of truth (§9); the log
        # leaf commits to (id, per-thread seq) so the hub cannot later equivocate
        # about either the entry or the seq it assigned (§6.4.1).
        # append_atomic gives us (seq, persist) as one operation per §6 — a failed
        # persist cannot burn a seq number. A translog failure AFTER persist is
        # recoverable via TamperEvidentLog.rebuild from store.iter_global (or from
        # store.iter_ephemeral_entries for ephemeral threads).
        seq = self.store.append_atomic(ev)
        if is_ephemeral:
            self.ephemeral_translog.append(  # type: ignore[union-attr]
                thread=ev.thread, entry_id=ev.id, seq=seq,
            )
            sth = self.ephemeral_translog.current_sth(ev.thread)  # type: ignore[union-attr]
        else:
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


def _apply_receipt(ledger: Ledger, ev: Entry, sth: STH | EphemeralSTH) -> None:
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
