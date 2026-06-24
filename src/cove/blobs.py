"""Content-addressed blob store. Spec: server-hub-spec.md §4.

The log is tiny (<1% of total storage in the simulation); the heavy bytes
live here. The key is the sha256 of the bytes — clients re-hash on
download to detect tampering (the hub cannot substitute content
undetected), and dedup within the organization is automatic because
identical bytes map to identical paths.

Filesystem-backed for the pilot: one file per blob, sharded by the first
two hex chars of the hash so any single directory stays small.
Alongside the files, a small SQLite db (`_meta.db` in the blob root)
tracks:

  - blob_meta:  hash, size, first_seen_at — survives byte expiry, so a
                future tiering pass can move cold bytes to cheap storage
                while the row remains as permanent proof-of-existence.
  - blob_refs:  (blob_hash, entry_id) + the entry's claimed media_type /
                size / name. Records the reference relationship at accept
                time so a future GC pass is a refcount check, not a
                full-log scan.

What's NOT here in v1: encryption (sign-only), tiering policy (only the
hooks via the metadata table), and GC of unreferenced blobs (the data
to drive it is recorded; the sweeper itself is deferred).

TODO(multi-tenant): The dedup scope is org-wide. `BlobStore.has` and the
`POST /blobs` response with `dedup:True` together form a PRESENCE
ORACLE: an uploader can learn whether the same bytes already exist in
the store by attempting an upload. In a single-org pilot this is benign
(one trust domain); the moment a second org shares a hub, dedup must
become per-tenant AND the response must not reveal cross-tenant
presence. Don't read 'dedup works today' as 'dedup is safe under
multi-tenancy' — the seam stays open only if this is marked.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


_HEX = set("0123456789abcdef")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS blob_meta (
    hash          TEXT PRIMARY KEY,
    size          INTEGER NOT NULL,
    first_seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS blob_refs (
    blob_hash     TEXT NOT NULL,
    entry_id      TEXT NOT NULL,
    media_type    TEXT,
    size_claimed  INTEGER,
    name          TEXT,
    PRIMARY KEY (blob_hash, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_blob_refs_entry ON blob_refs(entry_id);
"""


class BlobStore:
    def __init__(self, root: str = "data/blobs", *,
                 time_fn: Callable[[], float] = time.time) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._root / "_meta.db"),
                                     check_same_thread=False,
                                     isolation_level=None)
        self._conn.executescript(_SCHEMA)
        self._now = time_fn
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    def put(self, content: bytes) -> str:
        """Store `content` under its sha256. Returns the content-address
        ('sha256:HEX'). Idempotent — re-storing the same bytes is a no-op
        for both the file AND the metadata row, and returns the existing
        address.
        """
        h = "sha256:" + hashlib.sha256(content).hexdigest()
        bare = h[len("sha256:"):]
        p = self._path_for(bare)
        with self._lock:
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                # Atomic write: temp then rename, so a torn write never
                # leaves a half-bytes file at the final path — a later
                # re-upload of the same bytes recovers cleanly.
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_bytes(content)
                os.replace(tmp, p)
            self._conn.execute(
                "INSERT OR IGNORE INTO blob_meta (hash, size, first_seen_at)"
                " VALUES (?, ?, ?)",
                (h, len(content), self._now()),
            )
        return h

    def get(self, blob_id: str) -> Optional[bytes]:
        """Return the bytes for a content-address, or None if absent."""
        h = self._normalize(blob_id)
        if h is None:
            return None
        p = self._path_for(h)
        return p.read_bytes() if p.exists() else None

    def has(self, blob_id: str) -> bool:
        # TODO(multi-tenant): callers that surface has()-based presence
        # information to a client are operating a presence oracle (see
        # module docstring); fine in a single-org pilot, not later.
        h = self._normalize(blob_id)
        if h is None:
            return False
        return self._path_for(h).exists()

    # ---- metadata + references (for future tiering / GC) ---------------
    def metadata(self, blob_id: str) -> Optional[dict]:
        """Permanent metadata row: hash, size, first_seen_at. Survives a
        future byte-expiry / cold-tier pass — the bytes go to cheap
        storage, the row stays as proof-of-existence."""
        row = self._conn.execute(
            "SELECT hash, size, first_seen_at FROM blob_meta WHERE hash=?",
            (blob_id,),
        ).fetchone()
        if row is None:
            return None
        return {"hash": row[0], "size": int(row[1]), "first_seen_at": float(row[2])}

    def record_references(self, entry_id: str, refs: Iterable[Any]) -> None:
        """Record that `entry_id` references each blob in `refs`. Each ref
        may be a BlobRef dataclass or a dict with hash/media_type/size/name.
        Idempotent on (blob_hash, entry_id).

        Called by the pipeline AFTER an entry is committed to the store
        (so the entry_id exists), so this is the recording layer a future
        refcount-based GC will drive off — no log-scan reconstruction
        required.
        """
        with self._lock:
            for ref in refs:
                if hasattr(ref, "hash"):
                    h = ref.hash; mt = ref.media_type
                    sz = ref.size; nm = ref.name
                else:
                    h = ref["hash"]; mt = ref.get("media_type")
                    sz = ref.get("size"); nm = ref.get("name")
                self._conn.execute(
                    "INSERT OR IGNORE INTO blob_refs "
                    "(blob_hash, entry_id, media_type, size_claimed, name)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (h, entry_id, mt, sz, nm),
                )

    def references_for(self, blob_id: str) -> list[str]:
        """Entries that reference this blob — the input a future GC pass
        would scan to decide whether the bytes can be tiered or removed."""
        rows = self._conn.execute(
            "SELECT entry_id FROM blob_refs WHERE blob_hash=?", (blob_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def ref_count(self, blob_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM blob_refs WHERE blob_hash=?", (blob_id,),
        ).fetchone()
        return int(row[0])

    # ---- internals -----------------------------------------------------
    def _path_for(self, h: str) -> Path:
        return self._root / h[:2] / h

    @staticmethod
    def _normalize(blob_id: str) -> Optional[str]:
        """Accept 'sha256:HEX' only. Returns the bare hex, or None on any
        malformed input — rejects path-traversal attempts before they
        touch the filesystem (e.g. 'sha256:../etc/passwd')."""
        if not isinstance(blob_id, str) or not blob_id.startswith("sha256:"):
            return None
        h = blob_id[len("sha256:"):].lower()
        if len(h) != 64 or any(c not in _HEX for c in h):
            return None
        return h
