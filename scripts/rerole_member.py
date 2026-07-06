#!/usr/bin/env python3
"""rerole_member — change an existing attestation's role.

Companion to attest_member.py. attest_member refuses to touch a pubkey
that's already in the directory (adding it would duplicate the row);
this script does the opposite — it takes an already-attested pubkey and
issues a NEW attestation with a different role (and optionally a new
title / display_name / affiliation), then bakes a fresh root-signed
manifest that keeps every other row untouched.

Common use: bootstrap_pilot.py's --members mode hard-codes role='member';
after genesis you need to promote yourself (or another admin) to 'board'
so AdminPanel unlocks. Alternatively use --roster with role columns
next time.

Workflow:

  python scripts/rerole_member.py \\
      --hub https://lwccoa-hub.oap.dev \\
      --root-key ~/cove-root.priv \\
      --pubkey <hex> \\
      --role board \\
      --title "Keymaster"

The hub verifies the new manifest's chain + sig and swaps its in-memory
directory. Session tokens issued to the target pubkey stay valid; their
capabilities re-resolve against the new attestation on the next request.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from cove import crypto
from cove.identity import (
    hash_manifest, issue_attestation, issue_directory,
    manifest_from_dict, manifest_to_dict,
)


# Cloudflare's default WAF rules 1010/1015 block urllib's default UA
# ("Python-urllib/3.x") as a bot signature. Sending anything vaguely
# browser-shaped bypasses the check — the actual auth is on the payload
# sig (root-signed manifest), not the transport, so this is cosmetic.
_UA = "cove-admin-cli/0.4.76"


def _http_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"user-agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"GET {url} → {e.code}: {body}") from e


def _http_post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "user-agent": _UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"POST {url} → {e.code}: {body}") from e


def _load_root_priv(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"root key not found: {path}")
    priv_hex = path.read_text().strip()
    if len(priv_hex) != 64 or not all(c in "0123456789abcdef" for c in priv_hex):
        raise SystemExit(f"root key at {path} is not 64-char hex")
    return priv_hex


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() == "y"
    except (KeyboardInterrupt, EOFError):
        return False


def main() -> int:
    p = argparse.ArgumentParser(
        description="Change the role on an existing attestation.")
    p.add_argument("--hub", required=True,
                   help="e.g. https://lwccoa-hub.oap.dev")
    p.add_argument("--root-key", required=True, type=Path,
                   help="path to the org root private key (hex)")
    p.add_argument("--pubkey", required=True,
                   help="64-hex pubkey of the member being re-attested")
    p.add_argument("--role", required=True,
                   choices=["member", "officer", "board"],
                   help="new role for this pubkey")
    p.add_argument("--title", default=None,
                   help="new title (optional, replaces existing)")
    p.add_argument("--name", default=None,
                   help="new display name (optional, else preserve)")
    p.add_argument("--affiliation", default=None,
                   help="new affiliation (optional, else preserve)")
    p.add_argument("-y", "--yes", action="store_true",
                   help="skip confirmation prompt")
    args = p.parse_args()

    if len(args.pubkey) != 64 or not all(c in "0123456789abcdef" for c in args.pubkey):
        raise SystemExit(f"pubkey must be 64-char hex, got {len(args.pubkey)}")

    root_priv = _load_root_priv(args.root_key)
    root_pub = crypto.derive_pubkey(root_priv)

    print(f"[rerole] fetching current directory from {args.hub}…")
    current_dict = _http_get(f"{args.hub.rstrip('/')}/directory")
    current = manifest_from_dict(current_dict)
    if current.org != root_pub:
        raise SystemExit(
            f"root key does not match the hub's org pubkey.\n"
            f"  expected (hub): {current.org}\n"
            f"  derived (you):  {root_pub}\n"
        )

    existing = next(
        (a for a in current.attestations if a.member_pubkey == args.pubkey),
        None,
    )
    if existing is None:
        raise SystemExit(
            f"pubkey {args.pubkey} is not in the directory. "
            "Use attest_member.py to add them fresh.")

    new_name = args.name if args.name is not None else existing.display_name
    new_affiliation = (args.affiliation if args.affiliation is not None
                       else existing.affiliation)
    new_title = args.title if args.title is not None else existing.title

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[rerole] preparing re-attestation:")
    print(f"  pubkey:      {args.pubkey}")
    print(f"  name:        {existing.display_name} → {new_name}")
    print(f"  affiliation: {existing.affiliation} → {new_affiliation}")
    print(f"  role:        {existing.role} → {args.role}")
    print(f"  title:       {existing.title or '<none>'} → {new_title or '<none>'}")
    print(f"  issued_at:   {now}")
    if not args.yes and not _confirm(
            "[rerole] proceed? this writes to the live directory (y/N): "):
        print("[rerole] aborted")
        return 1

    new_att = issue_attestation(
        root_priv,
        member_pubkey=args.pubkey,
        display_name=new_name,
        affiliation=new_affiliation,
        role=args.role,
        title=new_title or None,
        issuer_pubkey=root_pub,
        issued_at=now,
    )

    # Replace the existing attestation for this pubkey; keep every other
    # row exactly as it was.
    other = [a for a in current.attestations if a.member_pubkey != args.pubkey]
    new_manifest = issue_directory(
        root_priv, org=root_pub,
        attestations=other + [new_att],
        revocations=list(current.revocations),
        updated_at=now,
        prev_manifest_hash=hash_manifest(current),
    )

    print(f"[rerole] posting new manifest to {args.hub}/admin/attest…")
    resp = _http_post(f"{args.hub.rstrip('/')}/admin/attest",
                      {"manifest": manifest_to_dict(new_manifest)})
    print(f"[rerole] hub accepted. manifest_hash={resp.get('manifest_hash')}")
    print(f"[rerole] {new_name} is now role={args.role}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
