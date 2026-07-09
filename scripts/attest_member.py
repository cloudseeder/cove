#!/usr/bin/env python3
"""attest_member — issue a single attestation against a running hub.

v0.4.0 admin tool. The keymaster scans a pairing QR (or accepts a
cove://pair?… link out-of-band), confirms the fingerprint matches what
the requesting device shows, then runs this script to issue the
attestation. The hub holds NO root key (CLAUDE.md non-negotiable #1),
so the manifest is built + root-signed entirely here, then POSTed to
/admin/attest where the hub verifies the chain + sig and swaps its
in-memory directory.

Workflow:
  1. Member generates keypair on-device → QR / pairing link
  2. Keymaster verifies channel (in-person scan, verified Signal, etc.)
  3. Keymaster runs:

       python scripts/attest_member.py \\
           --hub https://lwccoa-hub.oap.dev \\
           --root-key ~/cove-root.priv \\
           --pubkey <hex from QR> \\
           --name "Jane Doe" \\
           --affiliation "Lot 27" \\
           --role member \\
           --title ""

  4. Hub's WS /pending/watch fires; member's app auto-unlocks.

For multiple pending requests at once, just re-run the script per
member — each call chains off the prior manifest, so concurrent
admin actions are caught with a 409 stale_manifest (the spec's
stale-update guard).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Local import — assumes script is run from repo root with the cove
# package installed in editable mode (pip install -e ".[dev]").
from cove import crypto
from cove.identity import (
    DirectoryManifest, hash_manifest, issue_attestation, issue_directory,
    manifest_from_dict, manifest_to_dict,
)


# Cloudflare's default WAF rules block urllib's default UA as a bot
# signature — send anything vaguely non-Python-urllib and CF passes it
# through. Auth is on the signed manifest, not the transport, so the UA
# value doesn't matter beyond dodging the WAF.
_UA = "cove-admin-cli/0.4.85"


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
    sys.stdout.write(prompt)
    sys.stdout.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hub", required=True,
                   help="Hub base URL, e.g. https://lwccoa-hub.oap.dev")
    p.add_argument("--root-key", required=True, type=Path,
                   help="Path to root.priv (64-char hex on a single line)")
    p.add_argument("--pubkey", required=True,
                   help="Member's pubkey hex (from the QR / pairing link)")
    p.add_argument("--name", required=True,
                   help="Member's display name (root signs this binding)")
    p.add_argument("--affiliation", required=True,
                   help="Org sub-group: lot number / team / school / etc.")
    p.add_argument("--role", required=True,
                   choices=["member", "officer", "board"],
                   help="Trust tier (drives throttle/quota)")
    p.add_argument("--title", default="",
                   help="Human-readable title (President, Treasurer, …). "
                        "Optional; pass empty string for none.")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Skip the confirmation prompt")
    args = p.parse_args(argv)

    if len(args.pubkey) != 64 or not all(c in "0123456789abcdef" for c in args.pubkey):
        raise SystemExit("--pubkey must be 64-char hex")

    root_priv = _load_root_priv(args.root_key)
    root_pub = crypto.derive_pubkey(root_priv)

    print(f"[attest] fetching current directory from {args.hub}…")
    current_dict = _http_get(f"{args.hub.rstrip('/')}/directory")
    try:
        current = manifest_from_dict(current_dict)
    except Exception as e:
        raise SystemExit(f"hub returned an unparseable directory: {e}")

    if current.org != root_pub:
        raise SystemExit(
            f"root key does not match the hub's org pubkey.\n"
            f"  expected (hub): {current.org}\n"
            f"  derived (you):  {root_pub}\n"
            "If you are sure this is the right hub, verify your root key file."
        )
    # Refuse to re-attest if the pubkey is already in the directory —
    # otherwise the resulting manifest would have a duplicate row and
    # the hub validates that.
    if any(a.member_pubkey == args.pubkey for a in current.attestations):
        raise SystemExit(
            f"pubkey {args.pubkey} is already in the directory. "
            "Use --revoke flow to rotate, not attest."
        )

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[attest] preparing attestation:")
    print(f"  pubkey:      {args.pubkey}")
    print(f"  name:        {args.name}")
    print(f"  affiliation: {args.affiliation}")
    print(f"  role:        {args.role}")
    print(f"  title:       {args.title or '<none>'}")
    print(f"  issued_at:   {now}")
    if not args.yes and not _confirm(
            "[attest] proceed? this writes to the live directory (y/N): "):
        print("[attest] aborted")
        return 1

    new_att = issue_attestation(
        root_priv,
        member_pubkey=args.pubkey,
        display_name=args.name,
        affiliation=args.affiliation,
        role=args.role,
        title=args.title or None,
        issuer_pubkey=root_pub,
        issued_at=now,
    )

    new_manifest = issue_directory(
        root_priv, org=root_pub,
        attestations=list(current.attestations) + [new_att],
        revocations=list(current.revocations),
        updated_at=now,
        prev_manifest_hash=hash_manifest(current),
    )

    print(f"[attest] posting new manifest to {args.hub}/admin/attest…")
    resp = _http_post(f"{args.hub.rstrip('/')}/admin/attest",
                      {"manifest": manifest_to_dict(new_manifest)})
    print(f"[attest] hub accepted. manifest_hash={resp.get('manifest_hash')}")
    print(f"[attest] {args.name} is now in the directory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
