"""Entry data model and integrity. Spec: server-hub-spec.md §3.

An entry is the atomic unit of a thread DAG. `id` is the content address
of the entry minus {id, sig}; `sig` is the author's Ed25519 signature over the
same canonical content. This module implements id/signature correctly because
everything downstream (store, translog, pipeline) trusts it; the heavier logic
lives elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from . import crypto

# Reserved set of entry kinds (sign-only v1; no encryption kinds).
# 'branch' (v0.2) — declares that a sub-thread spawned off this thread.
# Carries the new sub-thread name in `branch_thread`. The branch entry
# itself is a regular signed log entry and lives in the PARENT thread;
# the sub-thread is open-namespace and materializes on first post like
# any other thread does.
#
# 'archive' / 'reopen' (v0.4.25) — governance acts that toggle a
# thread's visibility state. Body carries the human rationale. Like
# every other entry, they're signed and live in the log; client logic
# (not the hub) decides which entries actually mean "this thread is
# archived" — currently: the latest board-authored archive|reopen
# entry per thread wins. Non-board archive entries land in the log
# but are ignored by the visibility-state computation.
KINDS = {"notice", "post", "reply", "supersede", "membership", "receipt",
         "revoke", "branch", "archive", "reopen", "audience"}

# Fields excluded from the content that id/sig commit to.
_NON_CONTENT = {"id", "sig"}


@dataclass
class BlobRef:
    hash: str          # "sha256:" + hex of the blob bytes
    media_type: str
    size: int
    name: str


@dataclass
class Audience:
    """v0.4.27: per-thread audience scope (kind='audience').

    `pubkeys` is the closed list of attested members allowed to /sync,
    /threads-list, /inbox-list, and receive /stream pushes for this
    thread. An audience-less thread (no audience entry ever posted)
    behaves as before: public to every authed member.

    Update rule (computed by store.thread_audience): walk audience
    entries forward in seq order; the FIRST one establishes the
    audience by any author; subsequent ones are honored only if the
    author was in the audience at the time. Latest accepted entry
    wins (replaces — not unions).
    """
    pubkeys: list[str] = field(default_factory=list)


@dataclass
class Receipt:
    """Cumulative receipt payload (kind='receipt'). Spec §8.

    The recipient (entry.author) is acking 'thread C through seq N', where C
    is entry.thread and N is high_water_seq. observed_sth_(size,root) is the
    Signed Tree Head this recipient saw when they sent the ack — the
    receipt-carried evidence §6.4.3 uses to detect hub equivocation
    (same tree_size, different root_hash across recipients).
    """
    high_water_seq: int
    observed_sth_size: int
    observed_sth_root: str


@dataclass
class Entry:
    thread: str                      # thread root id; root: thread == id
    author: str                    # author public key (hex)
    kind: str                      # one of KINDS
    created_at: str                # rfc3339; advisory only, causal order from `parents`
    parents: list[str] = field(default_factory=list)
    body: str = ""
    blobs: list[BlobRef] = field(default_factory=list)
    supersedes: Optional[str] = None
    receipt: Optional[Receipt] = None   # set for kind='receipt' (§8)
    branch_thread: Optional[str] = None # set for kind='branch' (§3.x) —
                                        # names the spawned sub-thread.
                                        # Part of canonical content, so
                                        # signature covers the link.
    audience: Optional[Audience] = None # set for kind='audience' (v0.4.27);
                                        # conditionally omitted from
                                        # canonical content so adding the
                                        # field doesn't invalidate every
                                        # pre-v0.4.27 entry's signature.
    id: Optional[str] = None       # set by compute_id
    sig: Optional[str] = None      # set by sign

    def content(self) -> dict[str, Any]:
        """The canonical content that id and sig commit to (everything but id/sig)."""
        d = asdict(self)
        for k in _NON_CONTENT:
            d.pop(k, None)
        # v0.4.27: byte-identical-when-absent rule for the audience
        # field, mirroring DirectoryManifest.default_thread /
        # capabilities_by_role. asdict() always emits {'audience':
        # None} for pre-v0.4.27 entries; strip it so their signatures
        # still verify against the canonical form they signed.
        if d.get("audience") is None:
            d.pop("audience", None)
        return d


def compute_id(ev: Entry) -> str:
    return crypto.content_id(ev.content())


def sign_entry(ev: Entry, author_private_hex: str) -> Entry:
    """Compute id and sign. The author signs on THEIR device; the hub never does."""
    ev.id = compute_id(ev)
    ev.sig = crypto.sign(author_private_hex, crypto.canonicalize(ev.content()))
    return ev


def verify_entry(ev: Entry) -> bool:
    """Recompute id (must match) and verify sig against author. Spec §3.1.

    NOTE: this checks intrinsic integrity only. Whether `author` is a currently
    attested, non-revoked identity is the directory's job (identity.py), and is
    enforced in the acceptance pipeline (pipeline.py).
    """
    if ev.id is None or ev.sig is None:
        return False
    if ev.kind not in KINDS:
        return False
    if compute_id(ev) != ev.id:
        return False
    return crypto.verify(ev.author, ev.sig, crypto.canonicalize(ev.content()))
