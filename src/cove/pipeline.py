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
from .translog import TamperEvidentLog


class AcceptanceError(Exception):
    pass


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
        """Run the §7.1 pipeline. Return assigned seq, or raise AcceptanceError/ThrottleError.

        Steps (server-hub-spec.md §7.1) — implement in THIS order:
          1. structural bounds (pre-auth)        -> check_structural(ev)
          2. resolve author in directory; reject if unknown/revoked
          3. recompute id; reject on mismatch
          4. verify sig                          -> verify_entry(ev)
          5. per-identity throttle/quota         -> throttler.check_and_consume(...)
          6. ACL: author may post to thread (membership entries)
          7. verify parents exist (or accepted concurrently)
          8. assign seq; persist; EXTEND TAMPER-EVIDENT LOG; update STH
          9. update overview index; if receipt, update ledger
         10. fan out via /stream; queue for offline
        """
        raise NotImplementedError
