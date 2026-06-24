# CLAUDE.md — Cove (LWCCOA pilot)

This file is read by Claude Code on every session. It is the operating contract for this repo. Read it fully before acting.

## What this is

**Cove** is a single-organization network for **accountable group messaging**, built on **VNTP** (Verifiable Notice Transfer Protocol): a store-and-forward hub where every participant is a keypair, every entry is signed, delivery is provable, and history is tamper-evident. The pilot is the LWCCOA board + members. This is **not** an email replacement and not a chat app — it is verifiable, accountable group messaging for a closed institutional directory.

**Vocabulary (use these nouns everywhere — code, specs, API, schema):**
- **entry** — the atomic signed unit in the log (an *article*, in Usenet terms). Every kind is an entry: `notice`, `post`, `reply`, `supersede`, `membership`, `receipt`, `revoke`.
- **thread** — the container; an append-only DAG of entries (a *newsgroup*, in Usenet terms).
- **notice** — the headline entry-kind: a board broadcast to all members. It is what the "Notice" in VNTP refers to.
- **hub** — the server role (a Cove deployment runs one hub). The Python package is `cove`.

## Ground truth

- **`docs/server-hub-spec.md` is authoritative.** When code and spec disagree, the spec wins — or the spec is wrong and you stop and flag it, but you do not silently diverge.
- **`docs/client-spec.md` defines the wire contract** the client depends on. Changing a wire-facing behavior means updating both the spec and that contract.
- **Build in the order given in `server-hub-spec.md` §12.** It is sequenced so the broadcast-notice path works earliest and so the expensive-to-retrofit guarantees (tamper-evident log, throttle) go in before the things that depend on them.

## Non-negotiables (these are why this project exists — do not "simplify" them away)

1. **The hub holds NO member or root private keys.** It cannot forge participant content. It has exactly one key of its own — the **hub operational key** — used only to sign `seq` assignments and signed tree heads. The **root key never touches the hub** (it lives with the board, offline). If you find yourself putting a member or root private key on the server, stop.
2. **The tamper-evident log (§6.4) is not optional.** Hash chain + Merkle tree + signed tree heads + inclusion/consistency proofs. This is core v1, not a future nicety. It is what makes the hub *accountable* rather than merely trusted.
3. **The per-identity throttle/quota layer (§7.2) is not optional.** It is the protocol-level replacement for the bandwidth scarcity that used to limit abuse for free. Structural bounds pre-auth; token-bucket rate + volume + storage quota per attested identity, role-differentiated.
4. **No spam scoring. No content moderation.** Origin is binary and proven, or rejected. Throttling is mechanical and identity-keyed, never judgmental. The response to a bad actor is **accountability + revocation by the board**, not a filter.
5. **No silent failures.** Every rejection is a structured response (throttle scope, retry-after, reason). The whole point of this system is that things don't vanish quietly.
6. **Sign-only in v1.** Ed25519 signing everywhere. X25519 encryption fields are *reserved but unused*. Do not implement message/blob encryption in v1.
7. **Build nothing that requires inter-org, but keep the seams clean.** See `server-hub-spec.md` §11. Concretely: attestations carry an `issuer` field — never hardcode a single root in a way that blocks multi-root later. The overview index stays **derived and rebuildable** from the entry store. No federation, no foreign-root resolution, no toll/Sybil/reputation code. These are dormant by design.

## Architecture map (module → responsibility → spec)

| Module | Responsibility | Spec |
|---|---|---|
| `cove/crypto.py` | Ed25519 sign/verify, sha256, JCS (RFC 8785) canonicalization, content-address ids | §3.1 |
| `cove/entry.py` | Entry data model; id computation; signature creation/verification | §3 |
| `cove/identity.py` | Keys, attestations, signed directory manifest, revocation | §2 |
| `cove/store.py` | Append-only entry store (SQLite); source of truth | §9 |
| `cove/translog.py` | Tamper-evident log: hash chain, Merkle tree, STH, inclusion/consistency proofs | §6.4 |
| `cove/index.py` | Overview index (child map, seq order) + delivery ledger derivation | §6, §8 |
| `cove/throttle.py` | Structural bounds + per-identity token buckets + quotas | §7.2 |
| `cove/pipeline.py` | Entry acceptance pipeline — orchestrates the 10 steps | §7.1 |
| `cove/api.py` | FastAPI app: routes + WebSocket fan-out | §7 |
| `cove/config.py` | Org-configurable limits, tiers, bounds, STH cadence | §7.2, §10 |

## Test-first, and where to be careful

- **Write tests before implementation for the crypto-and-ordering core**: `crypto`, `entries`, `translog`, and the `pipeline`. `tests/test_translog.py` already encodes the inclusion/consistency-proof contract — make those pass; do not weaken the tests to make them pass.
- **Do not run unsupervised on `translog.py` or the pipeline ordering logic.** Verifiable history is worthless if the verification is subtly wrong. Small commits, human review on this core. Everything downstream trusts it.
- A slice is **done** when: its spec section is satisfied, its tests pass, no seam from §11 was violated, and the wire behavior matches `client-spec.md`.

## Git discipline

You manage this repo. Conventions:

- **Branch per build-order slice** (e.g. `feat/acceptance-pipeline`, `feat/translog`). Keep `main` working.
- **Small, frequent commits** with clear messages (conventional-commit style: `feat:`, `fix:`, `test:`, `docs:`, `refactor:`). Commit at each green test step.
- **Reversible operations are yours to run freely** — stage, commit, branch, merge locally, write messages.
- **Stop and ask before any irreversible or shared-history operation**: force-push, `reset --hard`, history rewrite/rebase of pushed commits, branch deletion, anything touching a remote's shared history. These don't have the cheap undo that a bad local commit does.
- **Never commit secrets.** Private keys, `.env`, `*.key`, `*.db` are gitignored. Key generation is `scripts/gen_keys.py`; generated keys stay out of the repo. If you ever see a key about to be committed, stop.

## Running

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                      # run tests
uvicorn hub.api:app --reload  # dev server (once api.py exists)
python scripts/gen_keys.py    # generate root + hub keypairs for a deployment
```

## Current focus

First slice = the riskiest core, built test-first: **acceptance pipeline + `seq` + Merkle log together** (build steps 3–5). That is where ordering, tamper-evidence, and throttle converge, and where a subtle bug is most expensive. Start at `tests/test_translog.py`, then `cove/translog.py`, then wire it into `cove/pipeline.py`.
