"""Throttle + quota contract. Spec §7.2.

This is the protocol-level replacement for bandwidth scarcity (§7.2 intro).
It does not judge — it caps blast radius and emits an admin alert when
violations sustain (§7.2.4). Accountability + revocation are the response,
not auto-revoke.

Structured rejection (§7.2.3): every rejection carries (scope, limit,
retry_after_s, detail) so the client can back off intelligently — no silent
drops (CLAUDE.md non-negotiable #5).
"""
from __future__ import annotations

import pytest

from cove.config import DEFAULT, HubConfig, StructuralBounds, TIERS
from cove.entry import BlobRef, Entry
from cove.throttle import ThrottleError, Throttler, check_structural


def _post(*, body: str = "hi", parents=None, blobs=None) -> Entry:
    return Entry(
        thread="t1", author="anyone", kind="post",
        created_at="2026-01-01T00:00:00Z", body=body,
        parents=parents or [], blobs=blobs or [],
    )


# ---- §7.2.1 structural bounds ------------------------------------------

def test_structural_pass_for_normal_entry():
    check_structural(_post())   # no raise


def test_structural_rejects_oversized_entry():
    # 256 KB default; pad the body well past that.
    big = _post(body="x" * (DEFAULT.bounds.max_entry_bytes + 10))
    with pytest.raises(ThrottleError) as ei:
        check_structural(big)
    assert ei.value.scope == "structural"
    assert ei.value.limit == DEFAULT.bounds.max_entry_bytes
    assert ei.value.retry_after_s is None    # structural is permanent


def test_structural_rejects_too_many_parents():
    too_many = ["sha256:" + "00" * 32] * (DEFAULT.bounds.max_parents + 1)
    with pytest.raises(ThrottleError) as ei:
        check_structural(_post(parents=too_many))
    assert ei.value.scope == "structural"
    assert ei.value.limit == DEFAULT.bounds.max_parents


def test_structural_rejects_too_many_blobs():
    blobs = [BlobRef(hash="sha256:" + "aa" * 32, media_type="x/y", size=10, name=f"b{i}")
             for i in range(DEFAULT.bounds.max_blobs_per_event + 1)]
    with pytest.raises(ThrottleError) as ei:
        check_structural(_post(blobs=blobs))
    assert ei.value.scope == "structural"
    assert ei.value.limit == DEFAULT.bounds.max_blobs_per_event


def test_structural_rejects_oversized_single_blob():
    blob = BlobRef(hash="sha256:" + "aa" * 32, media_type="x/y",
                   size=DEFAULT.bounds.max_blob_bytes + 1, name="huge")
    with pytest.raises(ThrottleError) as ei:
        check_structural(_post(blobs=[blob]))
    assert ei.value.scope == "structural"
    assert ei.value.limit == DEFAULT.bounds.max_blob_bytes


# ---- thread-name canonicalization (server-side enforcement) -------------
#
# The client sanitizes thread names to lowercase ASCII alphanumeric
# segments hyphen-separated before submitting (sanitizeThreadName in
# clients/web/src/lib/cove/threadname.ts). The hub rejects anything
# else with ThrottleError(scope='structural') so a hand-rolled API
# caller or older client can't slip a non-canonical name through and
# create a sibling-thread of the same intent.

@pytest.mark.parametrize("name", [
    "annual-meeting", "budget-2026", "t1", "t1-sub", "root", "a",
    "annual-board-meeting-2026-q4",
])
def test_structural_accepts_canonical_thread_names(name):
    check_structural(_post(parents=[]) if False else
                     Entry(thread=name, author="anyone", kind="post",
                           created_at="2026-01-01T00:00:00Z", body="hi"))


@pytest.mark.parametrize("bad,reason", [
    ("Annual-Meeting", "uppercase"),
    ("annual meeting", "whitespace"),
    ("annual_meeting", "underscore"),
    ("annual--meeting", "double hyphen"),
    ("-annual-meeting", "leading hyphen"),
    ("annual-meeting-", "trailing hyphen"),
    ("", "empty"),
    ("café-talk", "non-ascii"),
    ("a" * 65, "over length cap"),
])
def test_structural_rejects_non_canonical_thread_names(bad, reason):
    ev = Entry(thread=bad, author="anyone", kind="post",
               created_at="2026-01-01T00:00:00Z", body="hi")
    with pytest.raises(ThrottleError) as ei:
        check_structural(ev)
    assert ei.value.scope == "structural", reason


def test_structural_rejects_non_canonical_branch_thread():
    # branch_thread carries the SAME canonical-form contract — a malformed
    # one would otherwise create a non-addressable sub-thread when the
    # parent entry is accepted.
    ev = Entry(thread="parent", author="anyone", kind="branch",
               created_at="2026-01-01T00:00:00Z", body="branching off",
               branch_thread="Sub Thread")
    with pytest.raises(ThrottleError) as ei:
        check_structural(ev)
    assert ei.value.scope == "structural"
    assert "branch_thread" in ei.value.detail


# ---- §7.2.2 token bucket (rate + burst) --------------------------------

class _Clock:
    def __init__(self, t=0.0): self.t = t
    def now(self): return self.t
    def advance(self, dt): self.t += dt


@pytest.fixture
def clock():
    return _Clock()


def test_burst_allowance_consumes_then_rate_kicks_in(clock):
    """First `burst` entries fit; the (burst+1)th raises rate."""
    th = Throttler(time_fn=clock.now)
    tier = TIERS["member"]
    for _ in range(tier.burst):
        th.check_and_consume("alice", "member", entry_bytes=100)
    with pytest.raises(ThrottleError) as ei:
        th.check_and_consume("alice", "member", entry_bytes=100)
    assert ei.value.scope == "rate"
    assert ei.value.retry_after_s is not None and ei.value.retry_after_s > 0


def test_rate_rejection_does_not_drain_tokens(clock):
    """A rejected request must NOT consume — otherwise repeated failed
    attempts during throttling would also push out the legitimate retry."""
    th = Throttler(time_fn=clock.now)
    tier = TIERS["member"]
    for _ in range(tier.burst):
        th.check_and_consume("alice", "member", entry_bytes=100)
    # Several rejected attempts back-to-back.
    for _ in range(5):
        with pytest.raises(ThrottleError):
            th.check_and_consume("alice", "member", entry_bytes=100)
    # Advance enough wall time to refill exactly one token.
    refill_per_sec = tier.entries_per_min / 60.0
    clock.advance(1.0 / refill_per_sec + 0.01)
    th.check_and_consume("alice", "member", entry_bytes=100)   # one slot now


def test_separate_identities_have_separate_buckets(clock):
    th = Throttler(time_fn=clock.now)
    tier = TIERS["member"]
    for _ in range(tier.burst):
        th.check_and_consume("alice", "member", entry_bytes=100)
    # Bob is unaffected.
    th.check_and_consume("bob", "member", entry_bytes=100)


def test_role_differentiated_buckets(clock):
    """A board identity's burst is strictly larger than a member's (§7.2.2)."""
    th = Throttler(time_fn=clock.now)
    member_burst = TIERS["member"].burst
    board_burst = TIERS["board"].burst
    assert board_burst > member_burst
    # The board identity can spend past a member's burst limit without throttling.
    for _ in range(member_burst + 1):
        th.check_and_consume("board-key", "board", entry_bytes=100)


# ---- §7.2.2 rolling volume cap -----------------------------------------

def test_volume_cap_blocks_slow_drip_within_rate(clock):
    """Stay under the rate limit but exceed bytes/day -> rate doesn't catch
    this; the volume scope does. The whole point of the volume cap."""
    th = Throttler(time_fn=clock.now)
    tier = TIERS["member"]
    # Spread sends out so the rate bucket never empties; only volume matters.
    interval = 60.0 / tier.entries_per_min + 0.01
    chunk = tier.bytes_per_day // 4
    # 4 chunks fit exactly; the 5th must exceed.
    for _ in range(4):
        th.check_and_consume("alice", "member", entry_bytes=chunk)
        clock.advance(interval)
    with pytest.raises(ThrottleError) as ei:
        th.check_and_consume("alice", "member", entry_bytes=chunk)
    assert ei.value.scope == "volume"
    assert ei.value.limit == tier.bytes_per_day
    assert ei.value.retry_after_s is not None


def test_volume_window_slides_after_24h(clock):
    th = Throttler(time_fn=clock.now)
    tier = TIERS["member"]
    # Burn the whole daily budget at t=0.
    th.check_and_consume("alice", "member", entry_bytes=tier.bytes_per_day)
    # Slightly later, rate is fine but volume must reject.
    clock.advance(10.0)
    with pytest.raises(ThrottleError) as ei:
        th.check_and_consume("alice", "member", entry_bytes=1)
    assert ei.value.scope == "volume"
    # 24h later, the old window is gone — volume opens up again.
    clock.advance(86400 + 1)
    th.check_and_consume("alice", "member", entry_bytes=1)


# ---- §7.2.2 storage quota ----------------------------------------------

def test_storage_quota_blocks_blob_overflow(clock):
    th = Throttler(time_fn=clock.now)
    tier = TIERS["member"]
    # Fill the storage quota with a single big blob.
    th.check_and_consume("alice", "member",
                         entry_bytes=100, new_blob_bytes=tier.storage_quota_bytes)
    with pytest.raises(ThrottleError) as ei:
        th.check_and_consume("alice", "member",
                             entry_bytes=100, new_blob_bytes=1)
    assert ei.value.scope == "storage"
    assert ei.value.retry_after_s is None     # persistent until freed/raised
    assert ei.value.limit == tier.storage_quota_bytes


def test_zero_blob_bytes_does_not_affect_storage(clock):
    """No-blob entries never touch the storage counter (regression guard)."""
    th = Throttler(time_fn=clock.now)
    tier = TIERS["member"]
    # Many no-blob entries; storage_used must stay at 0.
    for i in range(10):
        clock.advance(60.0)  # avoid rate limit
        th.check_and_consume("alice", "member", entry_bytes=100)
    # A blob the size of the full quota still fits because nothing was used.
    th.check_and_consume("alice", "member", entry_bytes=100,
                         new_blob_bytes=tier.storage_quota_bytes)


# ---- §7.2.4 admin alert (note_violation) -------------------------------

def test_note_violation_alerts_at_threshold_then_quiets():
    cfg = HubConfig(violation_alert_threshold=3)
    th = Throttler(cfg=cfg)
    assert th.note_violation("alice") is False    # 1
    assert th.note_violation("alice") is False    # 2
    assert th.note_violation("alice") is True     # 3 — crosses threshold
    # Already alerted; subsequent calls don't re-fire (one alert per crossing).
    assert th.note_violation("alice") is False
    assert th.note_violation("alice") is False


def test_note_violation_per_identity():
    cfg = HubConfig(violation_alert_threshold=2)
    th = Throttler(cfg=cfg)
    assert th.note_violation("alice") is False
    assert th.note_violation("bob")   is False
    assert th.note_violation("alice") is True   # alice crosses; bob doesn't
    assert th.note_violation("bob")   is True   # bob crosses now
