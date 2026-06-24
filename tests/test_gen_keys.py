"""Key-generation operator tool. Pins the safety guarantees that matter when
the operator runs this against a real deployment:

  - default to stdout (no surprise file writes);
  - file mode 0600 on private key files (CLAUDE.md non-negotiable #1: the
    private key custody is the security model);
  - refuse to overwrite existing key files unless --force;
  - the bytes written actually round-trip through crypto.sign/verify.
"""
from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

import pytest

from cove import crypto


SCRIPT = Path(__file__).parent.parent / "scripts" / "gen_keys.py"


@pytest.fixture(scope="module")
def gen_keys():
    spec = importlib.util.spec_from_file_location("gen_keys", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- default stdout behavior ------------------------------------------

def test_default_emits_all_three_kinds_to_stdout(gen_keys, capsys):
    rc = gen_keys.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ROOT_PRIVATE="   in out
    assert "ROOT_PUBLIC="    in out
    assert "HUB_PRIVATE="    in out
    assert "HUB_PUBLIC="     in out
    assert "MEMBER_PRIVATE=" in out
    assert "MEMBER_PUBLIC="  in out


def test_default_stdout_flags_root_as_offline(gen_keys, capsys):
    """Operator-facing safety hint — the printed root section must call out
    its custody requirement, since this is the security model."""
    gen_keys.main([])
    out = capsys.readouterr().out
    root_block = out.split("ROOT_PRIVATE=")[0]
    assert "OFFLINE" in root_block.upper()


def test_kind_root_emits_only_root(gen_keys, capsys):
    rc = gen_keys.main(["--kind", "root"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ROOT_PRIVATE=" in out
    assert "HUB_PRIVATE="     not in out
    assert "MEMBER_PRIVATE="  not in out


# ---- file output: contents + permissions ------------------------------

def test_out_writes_keypair_files(gen_keys, tmp_path):
    rc = gen_keys.main(["--kind", "hub", "--out", str(tmp_path)])
    assert rc == 0
    priv = (tmp_path / "hub.priv").read_text().strip()
    pub = (tmp_path / "hub.pub").read_text().strip()
    assert len(priv) == 64    # ed25519 private as hex
    assert len(pub) == 64
    # The keypair must actually work.
    sig = crypto.sign(priv, b"smoke test")
    assert crypto.verify(pub, sig, b"smoke test") is True


def test_private_key_file_is_chmod_600(gen_keys, tmp_path):
    """Private key files MUST be 0o600. CLAUDE.md non-negotiable #1: custody
    is the security model — group/world-readable defeats it."""
    gen_keys.main(["--kind", "root", "--out", str(tmp_path)])
    priv_mode = stat.S_IMODE((tmp_path / "root.priv").stat().st_mode)
    pub_mode = stat.S_IMODE((tmp_path / "root.pub").stat().st_mode)
    assert priv_mode == 0o600, oct(priv_mode)
    assert pub_mode == 0o644, oct(pub_mode)


def test_kind_all_writes_all_three_pairs(gen_keys, tmp_path):
    rc = gen_keys.main(["--out", str(tmp_path)])
    assert rc == 0
    for name in ("root", "hub", "member"):
        assert (tmp_path / f"{name}.priv").exists()
        assert (tmp_path / f"{name}.pub").exists()


def test_name_overrides_basename(gen_keys, tmp_path):
    rc = gen_keys.main(["--kind", "member", "--name", "alice", "--out", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "alice.priv").exists()
    assert (tmp_path / "alice.pub").exists()
    assert not (tmp_path / "member.priv").exists()


# ---- safety: no silent overwrite --------------------------------------

def test_refuses_to_overwrite_by_default(gen_keys, tmp_path, capsys):
    """A second run against the same dir MUST fail rather than silently
    overwriting a private key (which would be catastrophic — old key is gone)."""
    rc = gen_keys.main(["--kind", "hub", "--out", str(tmp_path)])
    assert rc == 0
    first_priv = (tmp_path / "hub.priv").read_text()

    rc2 = gen_keys.main(["--kind", "hub", "--out", str(tmp_path)])
    assert rc2 != 0
    err = capsys.readouterr().err.lower()
    assert "exist" in err or "overwrite" in err or "refuse" in err
    # Original file untouched.
    assert (tmp_path / "hub.priv").read_text() == first_priv


def test_force_overwrites_existing(gen_keys, tmp_path):
    gen_keys.main(["--kind", "hub", "--out", str(tmp_path)])
    first = (tmp_path / "hub.priv").read_text()
    rc = gen_keys.main(["--kind", "hub", "--out", str(tmp_path), "--force"])
    assert rc == 0
    assert (tmp_path / "hub.priv").read_text() != first   # actually new
