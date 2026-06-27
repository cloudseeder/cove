"""End-to-end test for scripts/attest_member.py.

Stands up a real hub (via the conftest hub fixture), starts it on a
loopback port via uvicorn-style async, runs the script against it,
and asserts the directory now resolves the new pubkey.

The script's design — fetch /directory, sign new manifest, POST
/admin/attest — is intentionally NOT mocked here: the entire point
is to pin that the wire dance works against the production code path.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from cove import crypto


@contextlib.contextmanager
def _serve(app, port: int):
    """Spin up uvicorn in a background thread on the given port; yield
    the base URL. Stops on exit. Uses Server.should_exit signal so it
    actually stops cleanly between tests."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    # Wait for the port to actually accept connections — uvicorn boot is
    # async and threading.Thread.start doesn't block until ready.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz",
                                   timeout=0.5).read()
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn never came up")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        t.join(timeout=5)


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_attest_member_script_attests_a_pubkey(hub, tmp_path):
    # Persist root.priv to a temp file the script can read.
    root_key_file = tmp_path / "root.priv"
    root_key_file.write_text(hub["root_priv"])

    new_priv, new_pub = crypto.generate_keypair()

    # The new member is NOT yet in the directory (sanity).
    assert hub["directory"].resolve(new_pub) is None

    port = _free_port()
    with _serve(hub["app"], port) as base:
        result = subprocess.run(
            [
                sys.executable, "scripts/attest_member.py",
                "--hub", base,
                "--root-key", str(root_key_file),
                "--pubkey", new_pub,
                "--name", "Carol Pilot",
                "--affiliation", "Lot 42",
                "--role", "member",
                "--title", "",
                "-y",
            ],
            capture_output=True, text=True,
        )

    assert result.returncode == 0, \
        f"script failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    # The directory inside the running app is the SAME object the test
    # holds a reference to — the hub fixture shares it.
    att = hub["directory"].resolve(new_pub)
    assert att is not None
    assert att.display_name == "Carol Pilot"
    assert att.affiliation == "Lot 42"
    assert att.role == "member"


def test_attest_member_script_refuses_wrong_root_key(hub, tmp_path):
    """If the root.priv on disk doesn't derive to the hub's org pubkey,
    the script must refuse BEFORE signing anything — otherwise it
    builds a manifest the hub will reject anyway, with a less
    diagnostic error message."""
    wrong_priv, _ = crypto.generate_keypair()
    root_key_file = tmp_path / "wrong.priv"
    root_key_file.write_text(wrong_priv)

    new_priv, new_pub = crypto.generate_keypair()

    port = _free_port()
    with _serve(hub["app"], port) as base:
        result = subprocess.run(
            [
                sys.executable, "scripts/attest_member.py",
                "--hub", base, "--root-key", str(root_key_file),
                "--pubkey", new_pub, "--name", "Carol",
                "--affiliation", "U", "--role", "member", "-y",
            ],
            capture_output=True, text=True,
        )
    assert result.returncode != 0
    assert "root key does not match" in (result.stdout + result.stderr).lower()


def test_attest_member_script_refuses_duplicate_pubkey(hub, tmp_path):
    """Re-attesting a pubkey that's already in the directory should
    fail early — the hub validates this, but the local check gives
    a clearer error AND avoids needless network round-trips."""
    root_key_file = tmp_path / "root.priv"
    root_key_file.write_text(hub["root_priv"])

    port = _free_port()
    with _serve(hub["app"], port) as base:
        result = subprocess.run(
            [
                sys.executable, "scripts/attest_member.py",
                "--hub", base, "--root-key", str(root_key_file),
                "--pubkey", hub["member_pub"],   # already attested
                "--name", "Alice", "--affiliation", "U",
                "--role", "member", "-y",
            ],
            capture_output=True, text=True,
        )
    assert result.returncode != 0
    assert "already in the directory" in (result.stdout + result.stderr).lower()
