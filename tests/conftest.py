"""Shared fixtures."""
import pytest
from fastapi.testclient import TestClient

from cove import crypto
from cove.api import create_app
from cove.auth import AuthService
from cove.blobs import BlobStore
from cove.identity import (
    Directory, Revocation, issue_attestation, issue_directory,
)
from cove.index import Ledger, Overview
from cove.pipeline import Pipeline
from cove.store import EventStore
from cove.throttle import Throttler
from cove.translog import TamperEvidentLog


@pytest.fixture
def keypair():
    priv, pub = crypto.generate_keypair()
    return priv, pub


@pytest.fixture
def hub_keypair():
    """The hub operational key used to sign STHs (translog)."""
    priv, pub = crypto.generate_keypair()
    return priv, pub


@pytest.fixture
def root_keypair():
    """The org root key. Lives offline; signs attestations and directory manifests. §2."""
    priv, pub = crypto.generate_keypair()
    return priv, pub


@pytest.fixture
def hub(tmp_path, root_keypair, hub_keypair, keypair):
    """Fully-wired hub with one attested member + one revoked + auth'd TestClient.

    Lives in conftest so any test module can reach it; concrete wire tests
    live in test_api.py, but pending/admin tests (and future slices) reuse
    the same scaffolding.
    """
    root_priv, root_pub = root_keypair
    hub_priv, hub_pub = hub_keypair
    member_priv, member_pub = keypair

    att_member = issue_attestation(
        root_priv, member_pubkey=member_pub, display_name="Alice",
        affiliation="U-1", role="member", issuer_pubkey=root_pub,
        issued_at="2026-01-01T00:00:00+00:00",
    )
    revoked_priv, revoked_pub = crypto.generate_keypair()
    att_revoked = issue_attestation(
        root_priv, member_pubkey=revoked_pub, display_name="Bob (left)",
        affiliation="U-2", role="member", issuer_pubkey=root_pub,
        issued_at="2026-01-01T00:00:00+00:00",
    )
    rev = Revocation(pubkey=revoked_pub,
                     revoked_at="2026-02-01T00:00:00+00:00", reason="left")
    manifest = issue_directory(root_priv, org=root_pub,
                               attestations=[att_member, att_revoked],
                               revocations=[rev],
                               updated_at="2026-06-01T00:00:00+00:00")

    store = EventStore(str(tmp_path / "hub.db"))
    translog = TamperEvidentLog(hub_priv, hub_pub)
    overview = Overview()
    ledger = Ledger()
    directory = Directory.from_manifest(manifest)
    throttler = Throttler()
    pipeline = Pipeline(store=store, directory=directory, translog=translog,
                        overview=overview, ledger=ledger, throttler=throttler)
    auth = AuthService(directory=directory)
    blobs = BlobStore(str(tmp_path / "blobs"))

    from cove.invites import InviteRegistry
    invites = InviteRegistry()
    app = create_app(pipeline=pipeline, store=store, translog=translog,
                     overview=overview, ledger=ledger,
                     directory=directory, directory_manifest=manifest,
                     auth=auth, blobs=blobs, invites=invites)

    client = TestClient(app)
    ch = client.post("/auth/challenge").json()
    sig = crypto.sign(member_priv, ch["nonce"].encode())
    sess = client.post("/auth/verify", json={
        "pubkey": member_pub, "nonce": ch["nonce"], "sig": sig,
    }).json()
    client.headers["Authorization"] = f"Bearer {sess['token']}"

    return {
        "client": client,
        "app": app,
        "member_priv": member_priv, "member_pub": member_pub,
        "root_priv": root_priv, "root_pub": root_pub,
        "revoked_pub": revoked_pub,
        "hub_pub": hub_pub,
        "store": store, "translog": translog,
        "overview": overview, "ledger": ledger,
        "auth": auth, "directory": directory,
        "throttler": throttler,
        "blobs": blobs,
        "att_member": att_member,
        "session_token": sess["token"],
        "invites": invites,
    }
