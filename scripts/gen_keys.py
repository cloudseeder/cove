"""Generate root + hub operational + member keypairs for a deployment.

Examples:

    python scripts/gen_keys.py                          # print all to stdout
    python scripts/gen_keys.py --kind hub               # just the hub keypair
    python scripts/gen_keys.py --kind root --out keys/  # write root.priv (0600) + root.pub (0644)
    python scripts/gen_keys.py --kind member --name alice --out keys/

Custody (CLAUDE.md non-negotiable #1 — the security model lives here):

  - **ROOT private** must be kept OFFLINE with the board; NEVER on the hub.
    The hub holds no member or root private keys; it cannot forge content.
  - **HUB private** lives on the hub and signs only seq/STH claims.
  - **MEMBER private** lives on the member's device and signs their entries.

Private key files are written `0o600` so a group/world-readable filesystem
mistake cannot leak them. The script refuses to overwrite an existing file
unless `--force`; silently clobbering a private key is catastrophic — the
old key is unrecoverable.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, Sequence


# Allow `python scripts/gen_keys.py` before the package is installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from cove import crypto                                            # noqa: E402


_KINDS = ("root", "hub", "member")
_CUSTODY = {
    "root": "keep OFFLINE with the board; NEVER on the hub",
    "hub": "lives on the hub; signs seq/STH only",
    "member": "lives on the member's device; signs their entries",
}


def _print_block(kind: str, priv: str, pub: str) -> None:
    upper = kind.upper()
    print(f"# {upper} key — {_CUSTODY[kind]}")
    print(f"{upper}_PRIVATE={priv}")
    print(f"{upper}_PUBLIC={pub}")
    print()


def _write_keypair(out_dir: Path, name: str, priv: str, pub: str,
                   *, force: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    priv_path = out_dir / f"{name}.priv"
    pub_path = out_dir / f"{name}.pub"
    if not force:
        for p in (priv_path, pub_path):
            if p.exists():
                raise FileExistsError(
                    f"{p} already exists — refusing to overwrite (use --force)")
    priv_path.write_text(priv + "\n")
    pub_path.write_text(pub + "\n")
    os.chmod(priv_path, 0o600)
    os.chmod(pub_path, 0o644)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Generate Ed25519 keypairs for a Cove deployment.",
    )
    p.add_argument("--kind", choices=(*_KINDS, "all"), default="all",
                   help="which keypair(s) to generate (default: all)")
    p.add_argument("--out", type=Path, default=None,
                   help="write keypair files into this directory "
                        "(private 0600, public 0644); default: stdout only")
    p.add_argument("--name", default=None,
                   help="override the file basename (default: the --kind value); "
                        "useful for distinct member keys, e.g. --name alice")
    p.add_argument("--force", action="store_true",
                   help="overwrite existing key files (default: refuse)")
    args = p.parse_args(argv)

    kinds = list(_KINDS) if args.kind == "all" else [args.kind]
    if args.name is not None and args.kind == "all":
        print("error: --name requires a single --kind (not 'all')", file=sys.stderr)
        return 2

    for kind in kinds:
        priv, pub = crypto.generate_keypair()
        name = args.name or kind
        if args.out is not None:
            try:
                _write_keypair(args.out, name, priv, pub, force=args.force)
            except FileExistsError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
        else:
            _print_block(kind, priv, pub)
    return 0


if __name__ == "__main__":
    sys.exit(main())
