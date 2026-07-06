"""Production hub runner — what `uvicorn` imports.

State directory comes from COVE_STATE_DIR (default ~/cove-state). The runner
DOES NOT read root.priv — bootstrap_pilot.py used it once to sign the
genesis manifest, and per CLAUDE.md non-negotiable #1 it must have been
moved off the box before the hub starts. The manifest the hub serves
is the root-signed JSON saved at bootstrap; the runner only verifies +
attaches persistence so subsequent admin updates write through to the
chain.

Run:
    COVE_STATE_DIR=~/cove-state uvicorn scripts.run_hub:app \\
        --host 127.0.0.1 --port 8000

Behind a tunnel/reverse-proxy that terminates TLS, bind to 127.0.0.1.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cove.api import create_app                                       # noqa: E402
from cove.auth import AuthService                                     # noqa: E402
from cove.blobs import BlobStore                                      # noqa: E402
from cove.identity import Directory                                   # noqa: E402
from cove.index import Ledger, Overview                               # noqa: E402
from cove.pipeline import Pipeline                                    # noqa: E402
from cove.store import EventStore                                     # noqa: E402
from cove.throttle import Throttler                                   # noqa: E402
from cove.translog import TamperEvidentLog                            # noqa: E402
from cove.translog_ephemeral import EphemeralTransLog                 # noqa: E402
from cove.vaults import VaultStore                                    # noqa: E402


def _read(path: Path) -> str:
    return path.read_text().strip()


def _build_app():
    state = Path(os.environ.get("COVE_STATE_DIR",
                                str(Path.home() / "cove-state"))).expanduser()
    keys = state / "keys"
    data = state / "data"
    manifest_chain = state / "manifest.jsonl"

    hub_priv = _read(keys / "hub.priv")
    hub_pub = _read(keys / "hub.pub")

    # Loud refusal if root.priv is still here. The whole architecture
    # assumes it's gone; silently tolerating it would normalize the
    # exact mistake the security model exists to prevent.
    root_priv_path = keys / "root.priv"
    if root_priv_path.exists():
        raise RuntimeError(
            f"REFUSING TO START: {root_priv_path} is still on this machine. "
            "Per CLAUDE.md non-negotiable #1 the root private key must NEVER "
            "live on the hub. Move it offline (USB / paper / password "
            "manager) and shred the on-disk copy, then start the hub again."
        )

    # On-disk state — durable across restarts. Translog + Overview are
    # rebuilt from the store by create_app's lifespan; nothing to do here.
    store = EventStore(str(data / "cove.db"))
    blobs = BlobStore(str(data / "blobs"))
    # v0.4.76: vaults live in the same SQLite file as EventStore (distinct
    # table). Pass an explicit VaultStore so create_app doesn't fall back
    # to the "data/hub.db" default (which lands at /app/data in the
    # container — unwritable by uid 1000).
    vaults = VaultStore(str(data / "cove.db"))
    translog = TamperEvidentLog(hub_priv, hub_pub)
    ephemeral_translog = EphemeralTransLog(hub_priv, hub_pub)
    overview = Overview()
    ledger = Ledger()

    # Load the signed manifest chain from disk. Directory.load_chain returns
    # an empty Directory if the file is missing — fail loud instead so a
    # missing manifest isn't silently served as "no members".
    if not manifest_chain.exists():
        raise RuntimeError(
            f"REFUSING TO START: no signed manifest at {manifest_chain}. "
            "Run scripts/bootstrap_pilot.py first."
        )
    directory = Directory.load_chain(manifest_chain)
    directory.attach_persistence(manifest_chain)

    throttler = Throttler()
    pipeline = Pipeline(
        store=store, directory=directory, translog=translog,
        overview=overview, ledger=ledger, throttler=throttler, blobs=blobs,
        ephemeral_translog=ephemeral_translog,
    )
    auth = AuthService(directory=directory)
    return create_app(
        pipeline=pipeline, store=store, translog=translog,
        overview=overview, ledger=ledger, directory=directory,
        directory_manifest=directory.manifest, auth=auth, blobs=blobs,
        ephemeral_translog=ephemeral_translog, vaults=vaults,
    )


# Module-level so uvicorn can `--reload`-import this and find it.
app = _build_app()
