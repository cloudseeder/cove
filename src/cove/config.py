"""Org-configurable limits. Spec: server-hub-spec.md §7.2, §10.

Concrete defaults from the spec. These are operational config, not protocol
constants — an organization may ratify different values (§10.7). They are
gathered here so the throttle layer and the acceptance pipeline read one source.
"""
from __future__ import annotations

from dataclasses import dataclass


# ---- §7.2.1 hard structural bounds (apply to EVERY entry, all identities) ----
@dataclass(frozen=True)
class StructuralBounds:
    max_entry_bytes: int = 256 * 1024     # entry record, excluding referenced blobs
    max_parents: int = 32                 # bounds fan-in / DAG traversal cost
    max_blobs_per_event: int = 16
    max_blob_bytes: int = 100 * 1024 * 1024


# ---- §7.2.2 per-identity rate / volume / storage, by role -------------------
@dataclass(frozen=True)
class Tier:
    entries_per_min: int          # sustained token-bucket refill
    burst: int                   # bucket capacity
    bytes_per_day: int           # rolling volume cap
    storage_quota_bytes: int     # total blob storage for this identity


TIERS: dict[str, Tier] = {
    "member":  Tier(entries_per_min=20,  burst=60,  bytes_per_day=50  * 1024**2, storage_quota_bytes=2  * 1024**3),
    "officer": Tier(entries_per_min=60,  burst=120, bytes_per_day=200 * 1024**2, storage_quota_bytes=10 * 1024**3),
    "board":   Tier(entries_per_min=120, burst=300, bytes_per_day=1   * 1024**3, storage_quota_bytes=50 * 1024**3),
}


@dataclass(frozen=True)
class HubConfig:
    bounds: StructuralBounds = StructuralBounds()

    # §7.2.4 admin-alert threshold: sustained violations before flagging for board review.
    violation_alert_threshold: int = 5
    escalating_auto_throttle: bool = False   # never auto-revoke; revocation is a governance act

    # §6.4 / §10.8 signed-tree-head cadence: publish at least every N entries and every M seconds.
    # NOTE (v0.4.31 investigation): these fields are currently NOT
    # consulted by translog.current_sth() — it recomputes from the
    # current tree state on every call. The constants remain as spec
    # documentation; if we ever wire actual batching, the real fix for
    # the "size error" race is in /proof/inclusion's atomic bundling
    # (see api.py), NOT this cadence.
    sth_every_n_events: int = 64
    sth_every_seconds: int = 300

    # §5 session token TTL (seconds)
    session_ttl_seconds: int = 3600

    # v0.4.76 identity vault: max wire size of a single vault record.
    # Realistic vaults with 8 slots + a small content ciphertext land
    # around 4 KiB. 64 KiB is 16× headroom, which comfortably covers
    # future YubiKey slots (per-slot ephemeral pubkey wrappers) and label
    # sprawl without letting a hostile client burn megabytes of storage
    # under one pubkey.
    max_vault_body_bytes: int = 64 * 1024

    # v0.4.37 ephemeral threads: upper bound on the TTL a creator may
    # request at POST /threads/ephemeral. Prevents a member from opening
    # a "delete in 100 years" thread that quietly evades the ephemeral
    # tier's whole point. Default 1 year; the creation UI's presets are
    # 7d/30d/90d, so this only bites free-form values.
    ephemeral_ttl_max_seconds: int = 365 * 24 * 3600
    ephemeral_ttl_min_seconds: int = 1 * 24 * 3600
    # v0.4.38: how often the auto-seal task walks the live ephemeral
    # threads looking for expired TTLs. 0 disables the loop (tests
    # drive the seal via the manual endpoint or the internal helper).
    # Default 60s: cheap in production, granular enough that a thread
    # never lives more than a minute past its declared expiry.
    ephemeral_seal_check_seconds: int = 60

    def tier_for_role(self, role: str) -> Tier:
        # board and broadcast identities share the "board" tier; unknown roles default to member.
        return TIERS.get(role, TIERS["member"])


DEFAULT = HubConfig()
