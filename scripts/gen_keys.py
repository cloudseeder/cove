"""Generate root + hub operational keypairs for a deployment.

  python scripts/gen_keys.py

Prints keys; writes nothing by default. The ROOT private key must be kept OFFLINE
with the board and NEVER placed on the hub (CLAUDE.md §1). The HUB private key
lives on the hub and signs only seq/STH claims. Keys are gitignored; treat the
printed private keys as secrets.
"""
import sys
sys.path.insert(0, "src")
from cove import crypto


def main() -> None:
    root_priv, root_pub = crypto.generate_keypair()
    hub_priv, hub_pub = crypto.generate_keypair()
    print("# ROOT key — keep OFFLINE with the board; never on the hub")
    print(f"ROOT_PRIVATE={root_priv}")
    print(f"ROOT_PUBLIC={root_pub}")
    print()
    print("# HUB operational key — lives on the hub; signs seq/STH only")
    print(f"HUB_PRIVATE={hub_priv}")
    print(f"HUB_PUBLIC={hub_pub}")


if __name__ == "__main__":
    main()
