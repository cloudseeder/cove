"""Content-addressed blob store. Spec: server-hub-spec.md §4.

The log is tiny (<1% of total storage in the simulation); the heavy bytes
live here. The key is the sha256 of the bytes — clients re-hash on
download to detect tampering (the hub cannot substitute content
undetected), and dedup within the organization is automatic because
identical bytes map to identical paths.

Filesystem-backed for the pilot: one file per blob, sharded by the first
two hex chars of the hash so any single directory stays small (relevant
on tmpfs / ext4 / etc. at scale).

What's NOT here in v1: encryption (sign-only), tiering hot→cold (the
spec calls for hooks but no policy yet — bytes stay forever until that
lands), and the entry referencing the blob (entries live in the entry
store; this module only stores bytes).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional


_HEX = set("0123456789abcdef")


class BlobStore:
    def __init__(self, root: str = "data/blobs") -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes) -> str:
        """Store `content` under its sha256. Returns the content-address
        ('sha256:HEX'). Idempotent — re-storing the same bytes is a no-op
        and returns the existing address."""
        h = hashlib.sha256(content).hexdigest()
        p = self._path_for(h)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: temp file then rename, so a torn write (process
            # kill mid-flush) never leaves a half-bytes file at the final
            # path — a later re-upload of the same bytes recovers cleanly.
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_bytes(content)
            os.replace(tmp, p)
        return f"sha256:{h}"

    def get(self, blob_id: str) -> Optional[bytes]:
        """Return the bytes for a content-address, or None if absent."""
        h = self._normalize(blob_id)
        if h is None:
            return None
        p = self._path_for(h)
        return p.read_bytes() if p.exists() else None

    def has(self, blob_id: str) -> bool:
        h = self._normalize(blob_id)
        if h is None:
            return False
        return self._path_for(h).exists()

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
