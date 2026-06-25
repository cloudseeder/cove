"""One-shot deployment bootstrap. Sets up everything a hub needs to run.

This is the *ceremony* — root_priv touches the machine briefly to sign the
genesis directory manifest, then **you move root.priv off the box** before
starting the hub. The runner script (`run_hub.py`) never reads root.priv.

What it does:
    1. Generates root + hub keypairs (Ed25519).
    2. Generates a starter set of member keypairs (--members alice,bob,...).
    3. Issues root-signed attestations for each member.
    4. Issues + signs the genesis DirectoryManifest.
    5. Lays out the state directory the runner reads from.
    6. Prints exactly what you must move off the box.

State layout (under --state-dir, default ~/cove-state):

    keys/
      root.priv          # ←— MOVE OFFLINE IMMEDIATELY
      root.pub
      hub.priv           # stays on the box, 0600
      hub.pub
      members/
        <name>.priv      # hand to the member, then delete here
        <name>.pub
    manifest.json        # signed genesis manifest (wire form)
    manifest.jsonl       # the chain head (= just the genesis here)
    data/
      cove.db            # SQLite store (created lazily by EventStore)
      blobs/             # blob store dir

Usage:
    python scripts/bootstrap_pilot.py \\
        --org-name "LWCCOA" \\
        --members alice,bob,carol \\
        --state-dir ~/cove-state
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from cove import crypto                                                # noqa: E402
from cove.identity import (                                            # noqa: E402
    issue_attestation, issue_directory, manifest_to_dict,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _write(path: Path, content: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, mode)


def main() -> int:
    p = argparse.ArgumentParser(description="Bootstrap a Cove hub deployment.")
    p.add_argument("--state-dir", type=Path,
                   default=Path.home() / "cove-state",
                   help="where the hub keeps its state (default: ~/cove-state)")
    p.add_argument("--org-name", default="Cove pilot",
                   help="display name carried on the root attestation set")
    p.add_argument("--members", default="alice",
                   help="comma-separated member names to bootstrap "
                        "(default: alice)")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing state dir (DESTRUCTIVE)")
    args = p.parse_args()

    state = args.state_dir.expanduser()
    if state.exists() and any(state.iterdir()) and not args.force:
        print(f"error: {state} is not empty — refusing to overwrite "
              f"(use --force if you really mean it)", file=sys.stderr)
        return 1

    keys_dir = state / "keys"
    members_dir = keys_dir / "members"
    data_dir = state / "data"
    blobs_dir = data_dir / "blobs"
    for d in (keys_dir, members_dir, data_dir, blobs_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 1. Root keypair. Used here to sign attestations + the genesis manifest,
    #    then moved off the box. The hub NEVER reads it again.
    root_priv, root_pub = crypto.generate_keypair()
    _write(keys_dir / "root.priv", root_priv + "\n", mode=0o600)
    _write(keys_dir / "root.pub", root_pub + "\n", mode=0o644)

    # 2. Hub operational keypair. Signs seq + STH. Stays on the hub.
    hub_priv, hub_pub = crypto.generate_keypair()
    _write(keys_dir / "hub.priv", hub_priv + "\n", mode=0o600)
    _write(keys_dir / "hub.pub", hub_pub + "\n", mode=0o644)

    # 3. Member keypairs + root-signed attestations.
    issued_at = _now_iso()
    member_names = [n.strip() for n in args.members.split(",") if n.strip()]
    attestations = []
    for name in member_names:
        m_priv, m_pub = crypto.generate_keypair()
        _write(members_dir / f"{name}.priv", m_priv + "\n", mode=0o600)
        _write(members_dir / f"{name}.pub", m_pub + "\n", mode=0o644)
        att = issue_attestation(
            root_priv, member_pubkey=m_pub, display_name=name,
            unit=args.org_name, role="member",
            issuer_pubkey=root_pub, issued_at=issued_at,
        )
        attestations.append(att)

    # 4. Genesis directory manifest — root-signed wire form the hub serves.
    manifest = issue_directory(
        root_priv, org=root_pub,
        attestations=attestations, revocations=[],
        updated_at=issued_at,
    )
    manifest_dict = manifest_to_dict(manifest)
    _write(state / "manifest.json",
           json.dumps(manifest_dict, indent=2, sort_keys=True),
           mode=0o644)
    # Chain head (jsonl) — Directory.load_chain reads this on startup.
    _write(state / "manifest.jsonl",
           json.dumps(manifest_dict, sort_keys=True) + "\n",
           mode=0o644)

    # 5. Tell the operator exactly what to do next. The post-ceremony hygiene
    #    is the whole point — root.priv must leave this machine.
    print()
    print("=" * 72)
    print(" Cove pilot bootstrap complete")
    print("=" * 72)
    print(f" State directory : {state}")
    print(f" Root pubkey     : {root_pub}")
    print(f" Hub pubkey      : {hub_pub}")
    print(f" Members         : {', '.join(member_names)}")
    print()
    print(" Custody non-negotiable (CLAUDE.md #1) — DO THIS NOW:")
    print(f"   1. Move {keys_dir/'root.priv'}")
    print(f"      to OFFLINE storage (USB key, password manager, paper).")
    print(f"      Then delete it from this box:")
    print(f"        shred -u {keys_dir/'root.priv'}")
    print()
    print(f"   2. Hand each member their .priv from {members_dir}/")
    print(f"      and delete the copy on this box once they have it.")
    print()
    print(f"   3. Hub keeps {keys_dir/'hub.priv'} — it stays.")
    print()
    print(" To run the hub:")
    print(f"   COVE_STATE_DIR={state} uvicorn scripts.run_hub:app "
          "--host 127.0.0.1 --port 8000")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
