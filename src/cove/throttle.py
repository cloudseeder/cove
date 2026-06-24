"""Per-identity throttle & quota. Spec: server-hub-spec.md §7.2.

The protocol-level replacement for bandwidth scarcity. Bounds blast radius; does
NOT judge. Real enforcement against a bad actor is accountability + revocation by
the board (§7.2.4) — never auto-revoke here.

State is in-memory (§9 'transient counters'): per-identity token bucket, rolling
24h volume window, cumulative blob storage, and a violation counter. A real
deployment can persist these later; the rebuilds-from-entries integrity rule
does not apply to throttle state (it's operational, not authoritative).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Optional

from . import crypto
from .config import DEFAULT, HubConfig, Tier
from .entry import Entry


_VOLUME_WINDOW_S = 86400.0     # rolling 24h, per §7.2.2 "bytes/day"


class ThrottleError(Exception):
    """Carries the structured throttle response. Spec §7.2.3.

    `scope` distinguishes the four cases so the client can back off
    appropriately: rate/volume → transient (queue + retry after
    `retry_after_s`); storage → persistent until space is freed or quota
    raised; structural → permanent (the entry is malformed).
    """

    def __init__(self, scope: str, limit, retry_after_s: Optional[int], detail: str):
        super().__init__(detail)
        self.scope = scope            # "rate" | "volume" | "storage" | "structural"
        self.limit = limit
        self.retry_after_s = retry_after_s
        self.detail = detail


# ---- §7.2.1 pre-auth structural bounds ---------------------------------
def check_structural(ev: Entry, cfg: HubConfig = DEFAULT) -> None:
    """Pipeline step 1 (pre-auth). Raise ThrottleError(scope='structural') on violation.

    Cheap, content-agnostic, runs before any crypto or directory lookup.
    Bounds the worst case before we spend on authenticating it.
    """
    b = cfg.bounds
    entry_bytes = len(crypto.canonicalize(ev.content()))
    if entry_bytes > b.max_entry_bytes:
        raise ThrottleError("structural", b.max_entry_bytes, None,
                            f"entry size {entry_bytes} > max {b.max_entry_bytes}")
    if len(ev.parents) > b.max_parents:
        raise ThrottleError("structural", b.max_parents, None,
                            f"parents {len(ev.parents)} > max {b.max_parents}")
    if len(ev.blobs) > b.max_blobs_per_event:
        raise ThrottleError("structural", b.max_blobs_per_event, None,
                            f"blobs {len(ev.blobs)} > max {b.max_blobs_per_event}")
    for blob in ev.blobs:
        if blob.size > b.max_blob_bytes:
            raise ThrottleError("structural", b.max_blob_bytes, None,
                                f"blob {blob.name} size {blob.size} > max {b.max_blob_bytes}")


# ---- §7.2.2 per-identity state ----------------------------------------
@dataclass
class _IdentityState:
    tokens: float = 0.0                                  # current bucket level
    last_refill: float = 0.0                             # wall time of last refill calc
    volume_log: Deque[tuple[float, int]] = field(default_factory=deque)  # (t, bytes) — sliding 24h
    storage_used: int = 0
    violations: int = 0
    alerted: bool = False
    bucket_initialized: bool = False                     # first-touch flag for burst init


class Throttler:
    """Token buckets + rolling volume + storage quota, per identity, role-differentiated. §7.2.2.

    `time_fn` is injectable (defaults to time.monotonic) so tests can advance
    a virtual clock deterministically across the rate / volume window cases
    without sleeping.
    """

    def __init__(self, cfg: HubConfig = DEFAULT,
                 time_fn: Callable[[], float] = time.monotonic) -> None:
        self._cfg = cfg
        self._now = time_fn
        self._lock = threading.Lock()
        self._state: dict[str, _IdentityState] = {}
        # Per-identity tier overrides set via POST /admin/limits (§7.2.2).
        # Overrides are role-INDEPENDENT: once set, the throttler uses the
        # override regardless of what role the directory says the author has.
        self._overrides: dict[str, Tier] = {}

    def check_and_consume(self, author: str, role: str, entry_bytes: int,
                          new_blob_bytes: int = 0) -> None:
        """Pipeline step 5 (post-auth). Raise ThrottleError(rate|volume|storage) or return. §7.2.2.

        On any rejection, NO state is mutated — repeated failed attempts
        cannot themselves drain the bucket or the daily volume.
        """
        tier = self._overrides.get(author) or self._cfg.tier_for_role(role)
        with self._lock:
            st = self._state.setdefault(author, _IdentityState())
            now = self._now()

            self._refill_bucket(st, tier, now)
            self._trim_volume_window(st, now)

            # 1. Storage quota — permanent until freed. Check before mutating anything.
            if st.storage_used + new_blob_bytes > tier.storage_quota_bytes:
                raise ThrottleError("storage", tier.storage_quota_bytes, None,
                                    f"storage {st.storage_used + new_blob_bytes} > "
                                    f"quota {tier.storage_quota_bytes}")

            # 2. Rate — token bucket.
            if st.tokens < 1.0:
                refill_per_s = tier.entries_per_min / 60.0
                deficit = 1.0 - st.tokens
                retry = max(1, int(deficit / refill_per_s) + 1)
                raise ThrottleError("rate", tier.entries_per_min, retry,
                                    f"rate exceeded; refill {refill_per_s:.2f}/s")

            # 3. Volume — rolling 24h sum.
            window_used = sum(b for _, b in st.volume_log)
            if window_used + entry_bytes > tier.bytes_per_day:
                oldest_t = st.volume_log[0][0] if st.volume_log else now
                retry = max(1, int(_VOLUME_WINDOW_S - (now - oldest_t)) + 1)
                raise ThrottleError("volume", tier.bytes_per_day, retry,
                                    f"volume {window_used + entry_bytes} > "
                                    f"day {tier.bytes_per_day}")

            # Accept: deduct token, log volume, charge storage.
            st.tokens -= 1.0
            st.volume_log.append((now, entry_bytes))
            st.storage_used += new_blob_bytes

    def reserve_storage(self, author: str, role: str, new_blob_bytes: int) -> None:
        """Pre-flight quota check for a blob upload. §7.2.2 storage quota.

        Does NOT consume a rate token — blob uploads are byte-driven, not
        request-rate-driven (one POST /blobs is many MB of payload, not
        a 'request' in the rate-bucket sense). Raises ThrottleError(storage)
        on quota exhaustion; on success, charges the storage counter.

        Callers should call this AFTER dedup-check: if the blob already
        exists in the store, no charge is needed (the bytes are already
        paid for by the original uploader).
        """
        tier = self._overrides.get(author) or self._cfg.tier_for_role(role)
        with self._lock:
            st = self._state.setdefault(author, _IdentityState())
            if st.storage_used + new_blob_bytes > tier.storage_quota_bytes:
                raise ThrottleError("storage", tier.storage_quota_bytes, None,
                                    f"storage {st.storage_used + new_blob_bytes} > "
                                    f"quota {tier.storage_quota_bytes}")
            st.storage_used += new_blob_bytes

    def set_tier_override(self, author: str, tier_name: str) -> None:
        """Apply a per-identity tier override (§7.2.2 'overridable per identity
        via POST /admin/limits'). The override replaces the role-derived tier
        for this author from now on. Pass None or a clearing call to remove
        — not implemented yet; first writer wins for the pilot."""
        from .config import TIERS
        tier = TIERS.get(tier_name)
        if tier is None:
            raise ValueError(f"unknown tier {tier_name!r}")
        with self._lock:
            self._overrides[author] = tier

    def note_violation(self, author: str) -> bool:
        """Track sustained violations; return True when alert threshold crossed. §7.2.4.

        Fires exactly once per crossing (the first call that brings the
        identity AT-OR-ABOVE the threshold). Subsequent calls return False —
        the alert was already delivered; the throttle remains as evidence.
        Never auto-revoke; that is a governance act (§7.2.4).
        """
        with self._lock:
            st = self._state.setdefault(author, _IdentityState())
            st.violations += 1
            if st.alerted:
                return False
            if st.violations >= self._cfg.violation_alert_threshold:
                st.alerted = True
                return True
            return False

    # ---- internals -----------------------------------------------------
    def _refill_bucket(self, st: _IdentityState, tier: Tier, now: float) -> None:
        if not st.bucket_initialized:
            st.tokens = float(tier.burst)
            st.last_refill = now
            st.bucket_initialized = True
            return
        refill_per_s = tier.entries_per_min / 60.0
        elapsed = max(0.0, now - st.last_refill)
        st.tokens = min(float(tier.burst), st.tokens + elapsed * refill_per_s)
        st.last_refill = now

    def _trim_volume_window(self, st: _IdentityState, now: float) -> None:
        cutoff = now - _VOLUME_WINDOW_S
        while st.volume_log and st.volume_log[0][0] < cutoff:
            st.volume_log.popleft()
