"""Per-identity throttle & quota. Spec: server-hub-spec.md §7.2.

The protocol-level replacement for bandwidth scarcity. Bounds blast radius; does
NOT judge. Real enforcement against a bad actor is accountability + revocation by
the board (§7.2.4) — never auto-revoke here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import HubConfig, DEFAULT
from .entry import Entry


class ThrottleError(Exception):
    """Carries the structured throttle response. Spec §7.2.3."""
    def __init__(self, scope: str, limit, retry_after_s: Optional[int], detail: str):
        super().__init__(detail)
        self.scope = scope            # "rate" | "volume" | "storage" | "structural"
        self.limit = limit
        self.retry_after_s = retry_after_s
        self.detail = detail


def check_structural(ev: Entry, cfg: HubConfig = DEFAULT) -> None:
    """Pipeline step 1 (pre-auth). Raise ThrottleError(scope='structural') on violation. §7.2.1."""
    raise NotImplementedError


class Throttler:
    """Token buckets + rolling volume + storage quota, per identity, role-differentiated. §7.2.2."""

    def __init__(self, cfg: HubConfig = DEFAULT) -> None:
        self._cfg = cfg
        # TODO: per-identity bucket state, daily volume counters, storage usage.

    def check_and_consume(self, author: str, role: str, entry_bytes: int,
                          new_blob_bytes: int = 0) -> None:
        """Pipeline step 5 (post-auth). Raise ThrottleError(rate|volume|storage) or return. §7.2.2."""
        raise NotImplementedError

    def note_violation(self, author: str) -> bool:
        """Track sustained violations; return True when alert threshold crossed. §7.2.4."""
        raise NotImplementedError
