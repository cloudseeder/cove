#!/usr/bin/env python3
"""mint_invite — mint a single-use invite code against a running hub.

Companion to attest_member.py and rerole_member.py. The hub gates
/pending on a valid unused invite (v0.4.33). Codes are root-signed
because minting one is a governance act: the keymaster is authorizing
a new pubkey to join the queue. This script builds the signed payload
off-host, POSTs to /admin/invites, and prints the code + expiry so the
keymaster can deliver it out-of-band (text, in person, on paper).

Use this when the keymaster device can't reach the AdminPanel — e.g.
the PWA-only Mac has no root-key custody, or the Tauri desktop app
isn't installed on this machine. Same signing model as the AdminPanel's
mint-invite flow; same wire endpoint; same UA-header dodge for
Cloudflare's default WAF (see [[admin-cli-gotchas]]).

Workflow:

  python scripts/mint_invite.py \\
      --hub https://lwccoa-hub.oap.dev \\
      --root-key ~/cove-root.priv \\
      --ttl-seconds 86400 \\
      --name-hint "Amy Brooks"

Prints:
  [mint] code=ab12cd34…  expires_at=<monotonic-secs>  expires_in=86400s

Give the code to the invitee out-of-band. On the client, they paste it
into the OnboardingPanel's "Invite code" field. The invite is atomic
single-use — first successful /pending consumes it.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from cove import crypto
from cove.identity import manifest_from_dict


# Cloudflare's default WAF rules 1010/1015 block urllib's default UA
# ("Python-urllib/3.x") as a bot signature. Sending anything vaguely
# browser-shaped bypasses the check — the actual auth is on the payload
# sig (root-signed), not the transport, so this is cosmetic.
_UA = "cove-admin-cli/0.4.79"


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


def main() -> int:
    p = argparse.ArgumentParser(
        description="Mint a single-use invite code against a running hub.")
    p.add_argument("--hub", required=True,
                   help="e.g. https://lwccoa-hub.oap.dev")
    p.add_argument("--root-key", required=True, type=Path,
                   help="path to the org root private key (64-char hex)")
    p.add_argument("--ttl-seconds", type=int, default=86400,
                   help="how long the code is valid, in seconds "
                        "(default: 86400 = 24h). Common choices: 3600 "
                        "(1h — hand it out immediately), 86400 (24h — "
                        "text now, join tomorrow), 604800 (7d — mailing "
                        "list style).")
    p.add_argument("--name-hint", default=None,
                   help="optional label the keymaster attaches to the "
                        "invite so it's obvious in the /admin/invites "
                        "list who the code was minted for. Freeform.")
    args = p.parse_args()

    if args.ttl_seconds <= 0:
        raise SystemExit("--ttl-seconds must be > 0")

    root_priv = _load_root_priv(args.root_key)
    root_pub = crypto.derive_pubkey(root_priv)

    # Sanity: the root key must match the hub's advertised org, otherwise
    # the hub will reject the signature. Fetching /directory tells us the
    # advertised org pubkey.
    print(f"[mint] verifying root key against {args.hub}/directory…")
    current = manifest_from_dict(_http_get(f"{args.hub.rstrip('/')}/directory"))
    if current.org != root_pub:
        raise SystemExit(
            f"root key does not match the hub's org pubkey.\n"
            f"  expected (hub): {current.org}\n"
            f"  derived (you):  {root_pub}\n"
            "If you're sure this is the right hub, verify your root key file."
        )

    # Build + sign the payload. The hub verifies crypto.verify(root_pub,
    # sig, canonicalize(payload)) — the payload dict must round-trip
    # exactly through JCS on both sides, so we do NOT include extra
    # fields the hub won't accept (see cove/invites.py InviteRegistry.mint
    # for the accepted shape).
    payload: dict = {"ttl_seconds": args.ttl_seconds}
    if args.name_hint:
        payload["name_hint"] = args.name_hint
    sig = crypto.sign(root_priv, crypto.canonicalize(payload))

    print(f"[mint] posting to {args.hub}/admin/invites…")
    resp = _http_post(f"{args.hub.rstrip('/')}/admin/invites",
                      {"payload": payload, "sig": sig})

    code = resp.get("code", "?")
    expires_in = resp.get("expires_in_seconds", "?")
    name_hint = resp.get("name_hint") or "<none>"
    print()
    print(f"  code:       {code}")
    print(f"  expires_in: {expires_in}s")
    print(f"  name_hint:  {name_hint}")
    print()
    print("Deliver the code out-of-band (text, in person, on paper).")
    print("The invitee pastes it into OnboardingPanel's 'Invite code' field.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
