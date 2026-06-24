"""Overview index + delivery ledger contract. Spec §6, §8.

The overview index is DERIVED and rebuildable from the entry store (§6 integrity
rule): if it's lost or suspected stale, it can be reconstructed from the raw
signed entries. Don't bake state into the index that isn't recoverable.

The ledger is the cumulative high-water ack derivation per §8. It must:
  - be monotonic per (recipient, thread) — a late, stale receipt cannot
    'un-ack' earlier work;
  - surface both who has acked and who has NOT (the actionable list);
  - retain the observed STHs receipts carry, so equivocation across recipients
    (same tree_size, different root_hash) is detectable per §6.4.3.
"""
from __future__ import annotations

import pytest

from cove.index import Ledger, Overview


# ---- Overview -----------------------------------------------------------

@pytest.fixture
def overview():
    return Overview()


def test_children_unknown_returns_empty(overview):
    assert overview.children("never-seen") == []


def test_add_then_children(overview):
    overview.add("t1", "a", parents=[], seq=0)
    overview.add("t1", "b", parents=["a"], seq=1)
    overview.add("t1", "c", parents=["a"], seq=2)
    assert overview.children("a") == ["b", "c"]
    assert overview.children("b") == []


def test_threads_are_isolated(overview):
    overview.add("tA", "a", parents=[], seq=0)
    overview.add("tB", "x", parents=[], seq=0)
    assert [e for e, _, _ in overview.thread_entries("tA")] == ["a"]
    assert [e for e, _, _ in overview.thread_entries("tB")] == ["x"]


def test_thread_entries_in_seq_order_regardless_of_insertion_order(overview):
    """Entries can arrive out of order during a rebuild; the seq order is canon."""
    overview.add("t1", "b", parents=["a"], seq=1)
    overview.add("t1", "c", parents=["a"], seq=2)
    overview.add("t1", "a", parents=[],    seq=0)
    got = overview.thread_entries("t1")
    assert [eid for eid, _, _ in got] == ["a", "b", "c"]
    assert [seq for _, seq, _ in got] == [0, 1, 2]


def test_dag_supports_multiple_parents(overview):
    """A merge node has more than one parent — the child map records both edges."""
    overview.add("t1", "a", parents=[],       seq=0)
    overview.add("t1", "b", parents=[],       seq=1)
    overview.add("t1", "m", parents=["a","b"], seq=2)
    assert overview.children("a") == ["m"]
    assert overview.children("b") == ["m"]


def test_rebuild_reconstructs_the_index(overview):
    """§6 integrity rule: the overview must be rebuildable from raw entries."""
    overview.add("t1", "a", [],    0)
    overview.add("t1", "b", ["a"], 1)
    overview.add("t1", "c", ["a"], 2)

    rebuilt = Overview()
    rebuilt.rebuild([
        ("t1", "a", [],    0),
        ("t1", "b", ["a"], 1),
        ("t1", "c", ["a"], 2),
    ])
    assert rebuilt.children("a") == overview.children("a")
    assert rebuilt.thread_entries("t1") == overview.thread_entries("t1")


def test_rebuild_clears_prior_state(overview):
    """Rebuild starts from a clean slate — stale entries from before are gone."""
    overview.add("t1", "old", [], 0)
    overview.rebuild([("t1", "new", [], 0)])
    assert overview.children("old") == []
    assert [eid for eid, _, _ in overview.thread_entries("t1")] == ["new"]


# ---- Ledger -------------------------------------------------------------

@pytest.fixture
def ledger():
    return Ledger()


def test_high_water_unknown_returns_minus_one(ledger):
    assert ledger.high_water("nobody", "t1") == -1


def test_apply_receipt_records_high_water(ledger):
    ledger.apply_receipt("alice", "t1", 5, (10, "root_hex"))
    assert ledger.high_water("alice", "t1") == 5


def test_high_water_is_monotonic_per_recipient_thread(ledger):
    """A stale receipt arriving late cannot 'un-ack' a higher seq — cumulative ack semantics."""
    ledger.apply_receipt("alice", "t1", 5, (10, "r1"))
    ledger.apply_receipt("alice", "t1", 3, (8,  "r0"))    # stale
    assert ledger.high_water("alice", "t1") == 5
    # A new, higher receipt advances it.
    ledger.apply_receipt("alice", "t1", 7, (12, "r2"))
    assert ledger.high_water("alice", "t1") == 7


def test_high_water_independent_per_thread(ledger):
    """A receipt against thread A does not advance the ack on thread B."""
    ledger.apply_receipt("alice", "tA", 9, (20, "r"))
    assert ledger.high_water("alice", "tB") == -1


def test_status_partitions_members_into_acked_and_not(ledger):
    """The actionable non-delivery list (§8) — the email-style 'silent void' becomes a list."""
    ledger.apply_receipt("alice",  "t1", 10, (20, "r"))
    ledger.apply_receipt("bob",    "t1",  5, (20, "r"))    # behind the required seq
    # carol never ack'd anything.
    s = ledger.status("t1", required_seq=10, members=["alice", "bob", "carol"])
    assert sorted(s["acked"])    == ["alice"]
    assert sorted(s["not_acked"]) == ["bob", "carol"]


def test_status_when_nobody_has_acked(ledger):
    s = ledger.status("t1", required_seq=0, members=["alice", "bob"])
    assert s["acked"] == []
    assert sorted(s["not_acked"]) == ["alice", "bob"]


# ---- §6.4.3 equivocation detection via receipt-carried STH --------------

def test_observed_sths_collects_distinct_heads(ledger):
    ledger.apply_receipt("alice", "t1", 5, (10, "rootA"))
    ledger.apply_receipt("alice", "t1", 7, (12, "rootB"))
    ledger.apply_receipt("alice", "t1", 7, (12, "rootB"))   # duplicate; dedupe
    got = ledger.observed_sths("alice", "t1")
    assert got == {(10, "rootA"), (12, "rootB")}


def test_equivocation_signal_when_same_size_different_roots(ledger):
    """Two valid STHs at the same tree_size with different root_hash is the
    cryptographic equivocation signal of §6.4.3."""
    ledger.apply_receipt("alice", "t1", 5, (10, "rootA"))
    ledger.apply_receipt("bob",   "t2", 3, (10, "rootB"))   # same size, different root!
    signals = ledger.equivocation_signals()
    assert 10 in signals
    assert signals[10] == {"rootA", "rootB"}


def test_no_equivocation_when_all_recipients_see_same_root(ledger):
    ledger.apply_receipt("alice", "t1", 5, (10, "root"))
    ledger.apply_receipt("bob",   "t2", 3, (10, "root"))
    ledger.apply_receipt("carol", "t1", 8, (15, "root2"))   # different size — fine
    assert ledger.equivocation_signals() == {}
