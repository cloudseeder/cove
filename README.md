# Cove

Verifiable, accountable group messaging for a closed institutional directory. Pilot: LWCCOA board + members.

Cove is the network; **VNTP** (Verifiable Notice Transfer Protocol) is the protocol underneath it. NNTP/Usenet structure, git-style content-addressed integrity, made verifiable and accountable.

Every participant is a keypair. Every entry is signed. Delivery is provable. History is tamper-evident. There is no spam filter and no central trust authority deciding what is allowed — origin is cryptographically proven or rejected, and abuse is bounded by per-identity throttles and ended by the organization revoking an attestation.

This is **not** an email replacement and **not** a chat app. It is the smallest complete instance of accountable messaging inside a known directory. See `docs/server-hub-spec.md` (authoritative) and `docs/client-spec.md` (wire contract).

## Stack (a choice, not a constraint)

- **Python 3.11+ / FastAPI** — request/response + WebSocket fan-out.
- **PyNaCl** — Ed25519 signing/verification.
- **rfc8785** — JSON Canonicalization Scheme (deterministic serialization for content-addressing and signatures).
- **SQLite** (stdlib) — append-only entry store; no daemon, durable, fits a pilot.
- **pytest** — test-first, especially for the crypto-and-ordering core.

Swap any of these if there's a reason; update this section and `CLAUDE.md` if you do.

## Layout

```
docs/        the specs — server-hub-spec.md is ground truth
src/cove/     the server modules (see CLAUDE.md architecture map)
tests/       test-first; test_translog.py encodes the proof contract
scripts/     gen_keys.py — generate root + hub keypairs (kept out of the repo)
```

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Scope discipline

v1 is single-organization. Inter-org communication, federation, encryption, and toll/reputation are **deliberately deferred** (`server-hub-spec.md` §11) — designed-for via clean seams, but not built. Read `CLAUDE.md` before adding anything that reaches past one organization.
