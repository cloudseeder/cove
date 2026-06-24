"""Generate JSON fixtures the TS verification suite cross-checks against.

The TS port of the verification math has to produce byte-identical results
to the Python reference. The way we pin that is fixtures: build entries +
manifests + STHs + proofs in Python, write them to disk, load them in the
TS tests, and assert verify*() returns the same answer.

If the wire shape ever drifts, the TS fixtures regen and the diff makes
it visible immediately.

Output: clients/web/src/lib/cove/fixtures.json
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from cove import crypto
from cove.entry import Entry, sign_entry
from cove.identity import (
    Revocation, hash_manifest, issue_attestation, issue_directory,
)
from cove.translog import TamperEvidentLog


def main() -> None:
    # Deterministic-ish keypairs — we sign live and stash the keys too so
    # TS tests can verify the same signatures and also verify NEGATIVE cases.
    root_priv, root_pub = crypto.generate_keypair()
    hub_priv, hub_pub = crypto.generate_keypair()
    board_priv, board_pub = crypto.generate_keypair()
    alice_priv, alice_pub = crypto.generate_keypair()

    # Attestations + manifest
    att_board = issue_attestation(
        root_priv, member_pubkey=board_pub, display_name="Board",
        unit="B-1", role="board", issuer_pubkey=root_pub,
        issued_at="2026-01-01T00:00:00+00:00",
    )
    att_alice = issue_attestation(
        root_priv, member_pubkey=alice_pub, display_name="Alice",
        unit="U-1", role="member", issuer_pubkey=root_pub,
        issued_at="2026-01-01T00:00:00+00:00",
    )
    manifest = issue_directory(
        root_priv, org=root_pub,
        attestations=[att_board, att_alice],
        revocations=[Revocation(pubkey="00" * 32,
                                revoked_at="2026-02-01T00:00:00+00:00",
                                reason="never attested, in fixture only")],
        updated_at="2026-06-01T00:00:00+00:00",
    )

    # Translog with three entries from board.
    translog = TamperEvidentLog(hub_priv, hub_pub)
    entries = []
    for i, body in enumerate(["first notice", "second post", "third reply"]):
        ev = sign_entry(Entry(
            thread="annual-meeting", author=board_pub, kind="notice",
            created_at=f"2026-06-15T18:0{i}:00Z", body=body,
        ), board_priv)
        translog.append(ev.id, i)
        entries.append((ev, i))

    sth = translog.current_sth()
    proofs = [translog.inclusion_proof(ev.id) for ev, _ in entries]

    out = {
        "keypairs": {
            "root":  {"priv": root_priv,  "pub": root_pub},
            "hub":   {"priv": hub_priv,   "pub": hub_pub},
            "board": {"priv": board_priv, "pub": board_pub},
            "alice": {"priv": alice_priv, "pub": alice_pub},
        },
        "manifest": _manifest_dict(manifest),
        "manifest_hash": hash_manifest(manifest),
        "entries": [
            {"entry": asdict(ev), "seq": seq, "proof": asdict(p)}
            for (ev, seq), p in zip(entries, proofs)
        ],
        "sth": asdict(sth),
    }

    fixtures_path = (
        Path(__file__).resolve().parent.parent
        / "clients" / "web" / "src" / "lib" / "cove" / "fixtures.json"
    )
    fixtures_path.parent.mkdir(parents=True, exist_ok=True)
    fixtures_path.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {fixtures_path}")


def _manifest_dict(m) -> dict:
    return {
        "org": m.org,
        "attestations": [asdict(a) for a in m.attestations],
        "revocations": [asdict(r) for r in m.revocations],
        "updated_at": m.updated_at,
        "prev_manifest_hash": m.prev_manifest_hash,
        "sig": m.sig,
    }


if __name__ == "__main__":
    main()
