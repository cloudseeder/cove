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
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from cove import crypto                                                # noqa: E402
from cove.identity import (                                            # noqa: E402
    issue_attestation, issue_directory, manifest_to_dict,
)


_VALID_ROLES = {"member", "officer", "board"}


def _slug(name: str) -> str:
    """Turn 'Kevin Smith' into 'kevin-smith' for the keyfile name. Stable,
    case-insensitive, no surprises. Operators can override per row with
    a key_name column if they want shorter names or disambiguation."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "member"


def _load_roster(path: Path) -> list[dict]:
    """Parse a CSV roster.

    Required columns: display_name, affiliation, role.
    Optional columns: title, key_name (defaults to slugified display_name).

    Role must be one of member|officer|board (matches HubConfig tier
    table in cove/config.py). Anything else is rejected loudly rather
    than silently demoted to 'member', because role affects throttle
    tier and quota — a typo silently downgrading the board would be
    a quietly broken attestation.

    Affiliation is a freeform org sub-grouping (lot/dept/team/chapter/
    class). Empty string is fine. Title is the human-readable role
    title ('President', 'VP Engineering') and is independent of `role`
    (which is the protocol trust tier).
    """
    if not path.exists():
        raise SystemExit(f"roster file not found: {path}")
    rows: list[dict] = []
    seen_slugs: set[str] = set()
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        required = {"display_name", "affiliation", "role"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(
                f"roster missing required columns: {sorted(missing)} "
                f"(saw {reader.fieldnames})")
        for i, raw in enumerate(reader, start=2):  # row 1 = header
            display_name = (raw.get("display_name") or "").strip()
            affiliation = (raw.get("affiliation") or "").strip()
            role = (raw.get("role") or "").strip().lower()
            title = (raw.get("title") or "").strip() or None
            key_name = (raw.get("key_name") or "").strip() or _slug(display_name)
            if not display_name:
                raise SystemExit(f"roster row {i}: display_name is required")
            if role not in _VALID_ROLES:
                raise SystemExit(
                    f"roster row {i}: role {role!r} not in {sorted(_VALID_ROLES)}")
            if key_name in seen_slugs:
                raise SystemExit(
                    f"roster row {i}: key_name {key_name!r} is duplicated "
                    "(add a key_name column to disambiguate)")
            seen_slugs.add(key_name)
            rows.append({
                "display_name": display_name,
                "affiliation": affiliation,
                "role": role,
                "title": title,
                "key_name": key_name,
            })
    if not rows:
        raise SystemExit(f"roster {path} has no member rows")
    return rows


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
    group = p.add_mutually_exclusive_group()
    group.add_argument("--members", default=None,
                       help="comma-separated member names — quick path for "
                            "test fixtures. Real names live in a roster file.")
    group.add_argument("--roster", type=Path, default=None,
                       help="CSV roster. Columns: display_name, unit, role "
                            "(member|officer|board), key_name (optional, "
                            "defaults to slugified display_name). Each row "
                            "becomes a root-signed attestation with the real "
                            "name the UI renders.")
    p.add_argument("--default-thread", default=None,
                   help="soft hint clients use to land new members on a "
                        "specific thread after attestation (e.g. "
                        "'announcements'). Optional — omit to let clients "
                        "fall back to their own default ('general').")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing state dir (DESTRUCTIVE)")
    args = p.parse_args()
    if args.roster is None and args.members is None:
        args.members = "alice"  # back-compat default for the test path

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
    if args.roster is not None:
        roster = _load_roster(args.roster)
    else:
        # Legacy --members path: every member gets role='member' and the
        # org name as their affiliation (no real per-member info to put).
        roster = [
            {"display_name": n.strip(), "affiliation": args.org_name,
             "role": "member", "title": None, "key_name": _slug(n.strip())}
            for n in args.members.split(",") if n.strip()
        ]
    attestations = []
    for r in roster:
        m_priv, m_pub = crypto.generate_keypair()
        key_name = r["key_name"]
        _write(members_dir / f"{key_name}.priv", m_priv + "\n", mode=0o600)
        _write(members_dir / f"{key_name}.pub", m_pub + "\n", mode=0o644)
        att = issue_attestation(
            root_priv, member_pubkey=m_pub,
            display_name=r["display_name"],
            affiliation=r["affiliation"], role=r["role"],
            title=r["title"],
            issuer_pubkey=root_pub, issued_at=issued_at,
        )
        attestations.append(att)

    # 4. Genesis directory manifest — root-signed wire form the hub serves.
    manifest = issue_directory(
        root_priv, org=root_pub,
        attestations=attestations, revocations=[],
        updated_at=issued_at,
        default_thread=args.default_thread,
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
    print(f" Members         :")
    for r in roster:
        title_str = f" ({r['title']})" if r['title'] else ""
        print(f"   - {r['display_name']}{title_str}")
        print(f"       affiliation={r['affiliation']!r:<18} "
              f"role={r['role']:<8} key={r['key_name']}")
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
