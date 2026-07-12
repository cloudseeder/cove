"""Audience-change authorization — the write-side gate for kind='audience'.

Shared between the acceptance pipeline (structured 403 on rejection) and the
read-side defense-in-depth filter in store.thread_audience(). Both callsites
MUST route through this function so a hub bug can't silently smuggle an
unauthorized audience change past the read layer that the pipeline let land.

Spec: server-hub-spec.md §3 (audience-declaration entries).
"""
from __future__ import annotations

from typing import Callable, Optional


MANAGE_AUDIENCE_CAP = "manage_audience"


def authorize_audience_change(
    old: Optional[list[str]],
    new: list[str],
    author: str,
    caller_has_manage_audience: Callable[[str], bool],
) -> Optional[str]:
    """Return None if the change is authorized, else a structured reason string.

    Rules (spec §3.x):
      1. Bootstrap — no prior audience → accept unconditionally.
      2. Author-in-audience — author must be in `old`. Else 'not_in_audience'.
      3. Diff-gate — if `removed − {author}` is non-empty, author must hold the
         manage_audience capability. Else 'removal_requires_manage_audience'.
      4/5. Additive and self-leave — subsumed by rules 2+3.

    The caller-capability lookup is a callback so this module stays free of a
    Directory dependency; the pipeline passes `self.directory.caller_capabilities`
    and store.thread_audience() passes a closure over the live directory.
    """
    if old is None:
        return None
    if author not in old:
        return "not_in_audience"
    removed = set(old) - set(new)
    removed_others = removed - {author}
    if removed_others and not caller_has_manage_audience(author):
        return "removal_requires_manage_audience"
    return None
