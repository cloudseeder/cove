"""Unit tests for InviteRegistry — v0.5.1 SQLite persistence in particular.

The end-to-end API-level invite flows live in tests/test_pending_api.py
(they exercise mint/consume via /admin/invites + /pending). These tests
focus on the module-level shape: TTL semantics, cleanup, and the
persistence-across-restart invariant that motivated v0.5.1.
"""
from __future__ import annotations

import time

import pytest

from cove.invites import Invite, InviteRegistry, InviteUnusable


def test_mint_and_get_round_trip(tmp_path):
    reg = InviteRegistry(db_path=str(tmp_path / "hub.db"))
    inv = reg.mint(ttl_seconds=60, name_hint="Amy")
    got = reg.get(inv.code)
    assert got is not None
    assert got.code == inv.code
    assert got.name_hint == "Amy"
    assert got.is_active


def test_consume_marks_used(tmp_path):
    reg = InviteRegistry(db_path=str(tmp_path / "hub.db"))
    inv = reg.mint(ttl_seconds=60)
    consumed = reg.consume(inv.code)
    assert consumed.consumed_at is not None
    with pytest.raises(InviteUnusable, match="already_used"):
        reg.consume(inv.code)


def test_expired_invite_refuses_consume(tmp_path):
    """Rejected with reason='expired', not silently accepted."""
    now = [1_000_000.0]
    reg = InviteRegistry(db_path=str(tmp_path / "hub.db"),
                         time_fn=lambda: now[0])
    inv = reg.mint(ttl_seconds=10)
    now[0] += 20   # jump past expiry
    with pytest.raises(InviteUnusable, match="expired"):
        reg.consume(inv.code)


def test_revoke_active_invite(tmp_path):
    reg = InviteRegistry(db_path=str(tmp_path / "hub.db"))
    inv = reg.mint(ttl_seconds=60)
    reg.revoke(inv.code)
    with pytest.raises(InviteUnusable, match="revoked"):
        reg.consume(inv.code)


# ---- v0.5.1: persistence across registry-restart --------------------------

def test_invites_persist_across_registry_restart(tmp_path):
    """The whole point of v0.5.1. Mint two invites, drop the registry,
    open a fresh one on the same db file, and confirm both survive with
    their metadata intact."""
    db = str(tmp_path / "hub.db")
    reg = InviteRegistry(db_path=db)
    a = reg.mint(ttl_seconds=3600, name_hint="Amy")
    b = reg.mint(ttl_seconds=7200, name_hint=None)
    del reg   # simulate hub restart

    reg2 = InviteRegistry(db_path=db)
    got_a = reg2.get(a.code)
    got_b = reg2.get(b.code)
    assert got_a is not None
    assert got_b is not None
    assert got_a.name_hint == "Amy"
    assert got_a.expires_at == pytest.approx(a.expires_at)
    assert got_b.expires_at == pytest.approx(b.expires_at)
    # Both still consumable on the new registry.
    reg2.consume(a.code)


def test_consume_state_persists_across_registry_restart(tmp_path):
    """A code consumed before the restart is still consumed after."""
    db = str(tmp_path / "hub.db")
    reg = InviteRegistry(db_path=db)
    inv = reg.mint(ttl_seconds=3600)
    reg.consume(inv.code)
    del reg

    reg2 = InviteRegistry(db_path=db)
    with pytest.raises(InviteUnusable, match="already_used"):
        reg2.consume(inv.code)


def test_revoke_state_persists_across_registry_restart(tmp_path):
    """A code revoked before the restart is still revoked after."""
    db = str(tmp_path / "hub.db")
    reg = InviteRegistry(db_path=db)
    inv = reg.mint(ttl_seconds=3600)
    reg.revoke(inv.code)
    del reg

    reg2 = InviteRegistry(db_path=db)
    with pytest.raises(InviteUnusable, match="revoked"):
        reg2.consume(inv.code)


def test_list_active_survives_restart(tmp_path):
    db = str(tmp_path / "hub.db")
    reg = InviteRegistry(db_path=db)
    a = reg.mint(ttl_seconds=3600, name_hint="Amy")
    b = reg.mint(ttl_seconds=3600, name_hint="Bob")
    reg.consume(b.code)   # b is no longer active
    del reg

    reg2 = InviteRegistry(db_path=db)
    active = reg2.list_active()
    assert len(active) == 1
    assert active[0].code == a.code


def test_cleanup_drops_long_expired_rows(tmp_path):
    """Rows expired-or-consumed longer than the cleanup age get dropped
    on the next mint, so a busy hub doesn't need a background sweeper."""
    now = [1_000_000.0]
    db = str(tmp_path / "hub.db")
    reg = InviteRegistry(db_path=db, time_fn=lambda: now[0])
    old = reg.mint(ttl_seconds=60)
    now[0] += 15 * 24 * 3600   # 15 days later — past the 14d cleanup age
    # Next mint should sweep `old` out.
    reg.mint(ttl_seconds=60)
    assert reg.get(old.code) is None
    # And on a fresh registry from the same file, `old` is truly gone
    # (not just missing from in-memory cache).
    del reg
    reg2 = InviteRegistry(db_path=db, time_fn=lambda: now[0])
    assert reg2.get(old.code) is None


def test_in_memory_only_still_works(tmp_path):
    """No db_path → in-memory only. Existing unit-test callers that
    don't need persistence keep working."""
    reg = InviteRegistry()
    inv = reg.mint(ttl_seconds=60)
    assert reg.get(inv.code) is not None
