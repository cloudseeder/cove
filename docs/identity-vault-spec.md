# Cove — Identity Vault Specification

**Protocol:** VNTP identity portability layer
**Scope:** cross-platform key custody without ecosystem lock-in
**Status:** Draft 0.1 (implemented in v0.4.76)
**Companions:** `server-hub-spec.md` §2 (identity & directory), `client-spec.md` §1 (key custody)

The identity vault is Cove's answer to the *cross-ecosystem Passkey gap*: iCloud Keychain and Google Password Manager do not sync WebAuthn credentials across each other, so a user with mixed Apple + Android + Windows devices otherwise needs one keypair per ecosystem. The vault lets a user pin **one canonical Ed25519 identity keypair** and unlock it from any device via any of N supported unlock methods (passphrase, Passkey PRF, later YubiKey / recovery codes).

The vault is a **member-owned resource** stored on any Cove hub the user has a session on. It is opaque to the hub — the hub sees only ciphertext plus a signed envelope — and it does not change the identity model of the VNTP protocol at all. Every entry the client emits is still signed with a normal Ed25519 key, verifiable against the directory the way it always was. What changes is only the *storage and portability* of the priv material, not its cryptographic role.

---

## 1. Trust model and non-negotiables preserved

The vault preserves every existing invariant of the hub role (`server-hub-spec.md` §1) and of CLAUDE.md's non-negotiables. Concretely:

- **The hub still holds no member or root private keys** (CLAUDE.md #1). The vault ciphertext is opaque; the hub cannot decrypt any part of it and cannot recover the priv material.
- **The hub still cannot forge participant content** (`server-hub-spec.md` §1). The vault is member-signed; a tampered vault has an invalid Ed25519 signature against the vault-owner pubkey, and every client reader verifies before decrypting.
- **The tamper-evident log continues to be authoritative for entries** (`server-hub-spec.md` §6.4). Vault records are not entries; they do not enter the merkle log, they do not have `seq` numbers, and they do not participate in per-thread ordering. Vaults are stored in a separate table with per-pubkey CAS but no cross-record commitment. Rationale: vaults describe a member's own identity storage; they do not describe hub-visible history, so global ordering does not apply.
- **No new residual power for a malicious hub.** A malicious hub could withhold a vault (refuse GET) or refuse a write (403 all PUT). Both are indistinguishable from availability failure, which is the pre-existing "trusted for availability only" property. A malicious hub cannot serve a *different* vault under the same pubkey — the sig would fail to verify against the owner's pubkey, and the client refuses to unlock.

A new residual acknowledged honestly:

- **A malicious hub can serve a stale vault.** If the vault owner rotates a slot (e.g. removes a compromised passphrase) and the hub serves the pre-rotation record, a client without a fresher local copy will accept it. This is a *rollback* attack. Mitigation: replicate vault writes to every hub the member is on (§5.3 multi-hub replication); the client resolves divergence by preferring the chain-descendant head (§5.4). It does not prevent rollback if *every* hub the user has ever written to colludes; the user's own device holding the newest hash is the ultimate ground truth.

---

## 2. Design premise

A vault holds one canonical Ed25519 identity priv wrapped in a **two-layer encryption**:

- **Content key (CEK)** — a random 32-byte AES-GCM key, minted once at vault creation. It encrypts the priv material (once). The CEK is never persisted in plaintext; it exists only in client memory during an unlock session.
- **N unlock slots** — each slot wraps the CEK under a different KEK derived from a different unlock method. Adding, rotating, or removing a slot rewrites *only that slot's wrap*. The content ciphertext is minted once at vault creation and lives untouched for the vault's lifetime.

This shape is standard multi-recipient encryption (JWE, `age`, AGE-encrypted-file, etc.), applied to a member's own identity priv. Its two consequences:

- Adding a Passkey does not re-encrypt anything the user already had. Adding a passphrase, or removing an old one, does not require the priv to touch cleartext outside the client's memory.
- Different unlock methods on different devices can unlock the **same identity** without any of them holding the others' material. iCloud Keychain and Google Password Manager do not need to talk to each other; they each derive a different KEK from a different Passkey ceremony, both of which unwrap the same CEK, which decrypts the same priv.

---

## 3. Wire schema

A vault record is a JCS-canonical (RFC 8785) JSON object, signed with the vault-owner's Ed25519 priv.

```
vault_record = {
  "pubkey":            <64-hex Ed25519 pubkey>,        // MUST match the URL {pubkey}
  "version":           1,                              // schema version
  "prev_vault_hash":   "sha256:<hex>",                 // GENESIS_SENTINEL on first record
  "content_algo":      "AES-GCM-256-v1",
  "content_iv":        <b64url of 12 bytes>,
  "content_ciphertext":<b64url of AES-GCM(CEK, IV, plaintext)>,
  "method_slots":      [ <MethodSlot>, ... ],          // 1..8 entries
  "updated_at":        <rfc3339 UTC>,
  "sig":               <128-hex Ed25519 over JCS(record minus 'sig')>
}
```

### 3.1 Content plaintext (inside `content_ciphertext`)

Decrypting the outer ciphertext yields a JCS-canonical object:

```
content = {
  "priv":       <64-hex Ed25519 priv>,
  "pub":        <64-hex Ed25519 pub>,                  // sanity check vs. outer pubkey
  "created_at": <rfc3339 UTC>,                         // vault mint time (invariant)
  "meta":       { "note"?: <string ≤128 chars> }
}
```

`meta` is a growth surface: additional fields may land here in a future schema version without a wire break, per the byte-identical-when-absent extensibility rule the manifest uses.

### 3.2 Method slot: passphrase

```
{
  "id":              <16-hex random>,                    // slot identity, stable across rotations
  "type":            "passphrase",
  "algo":            "PBKDF2-SHA256-AES-GCM-256-v1",
  "kdf_salt":        <b64url of 16 bytes>,
  "kdf_iterations":  600000,                             // OWASP 2023 minimum for PBKDF2-SHA256
  "wrap_iv":         <b64url of 12 bytes>,
  "wrap_ciphertext": <b64url of AES-GCM(KEK, IV, CEK)>,
  "label":           <string ≤64 chars>,                 // user-chosen display name
  "created_at":      <rfc3339 UTC>
}
```

`kdf_iterations` is per-record so a future OWASP bump can be adopted transparently (new slots use the new count; old slots keep working until re-added).

### 3.3 Method slot: passkey (WebAuthn PRF)

```
{
  "id":              <16-hex random>,
  "type":            "passkey",
  "algo":            "PRF-HKDF-AES-GCM-256-v1",
  "credential_id":   <b64url of WebAuthn credentialId>,  // allowCredentials hint on unlock
  "rp_id":           <RP identifier used during the ceremony>,
  "prf_salt_tag":    "cove-vault-kek-v1",                // sha256(this string) is the PRF `first` input
  "hkdf_info":       "cove-vault-kek-v1",                // HKDF-SHA256 info string for KEK derivation
  "wrap_iv":         <b64url of 12 bytes>,
  "wrap_ciphertext": <b64url of AES-GCM(KEK, IV, CEK)>,
  "label":           <string ≤64 chars>,
  "created_at":      <rfc3339 UTC>
}
```

### 3.4 Signature semantics

- `sig` covers `JCS(record minus 'sig')`. Signed under the **vault-owner's Ed25519 priv** (NOT the org root). The hub verifies against the pubkey named in `record.pubkey` (which the endpoint also requires to match the URL `{pubkey}` and the caller's session pubkey).
- A tampered slot without a re-sign yields an invalid signature. This is what closes the "hub swaps my Passkey slot" attack surface.

### 3.5 Hash and chain

- `hash_vault(record) = "sha256:" + sha256_hex(JCS(record))` — hashes the full record **including `sig`**. The chain therefore commits to the signature as well as the content, preventing sig-swap attacks.
- The next record's `prev_vault_hash` MUST equal `hash_vault(prior record)`.
- Genesis sentinel for the first record: `prev_vault_hash = "sha256:" + "0" * 64`.
- Mirrors `server-hub-spec.md` §2.3 manifest chaining exactly. Per-pubkey chain, not per-org.

### 3.6 Load-bearing invariants (schema level)

- **Slot IDs are stable across rotations.** A slot rewritten with new wrap material (KDF params tuned, KEK re-derived) keeps its `id`. UI can render "this slot" persistently.
- **The vault-KEK PRF salt (`cove-vault-kek-v1`) MUST differ from the identity-seed PRF salt** used by the v0.4.74 device-Passkey path (`cove-passkey-prf-v1`). A leaked vault KEK must not be walkable back to any deterministically-derived priv. Enforced client-side and in test.
- **Slot AES-GCM wrap authenticates the CEK, not the vault owner.** Wrong passphrase → AES-GCM tag failure → try next slot. There is no distinct "wrong passphrase" error path in the client; failure is uniform across slot types.
- **`method_slots` MUST be non-empty and at most 8 entries.** A vault with zero slots is unrecoverable; hub-side `validate_shape` enforces the invariant. The soft cap of 8 bounds vault size and keeps the UI list human-scannable.

---

## 4. Hub responsibilities

### 4.1 Endpoints

**`GET /vault/{pubkey}` — public, unauthenticated.**

Response envelope:

```
200 OK
{
  "pubkey":     <64-hex>,
  "version":    1,
  "hash":       "sha256:<hex>",
  "updated_at": <rfc3339>,
  "body":       <b64url of the JCS-canonical vault record bytes>
}
```

or:

```
404 { "error": "vault_not_found" }
```

Rationale for public GET: the ciphertext is opaque and reveals nothing beyond "this pubkey has a vault of size N." Attested pubkeys are already public via `GET /directory`, so vault existence adds no privacy surface the directory does not already carry.

**`PUT /vault/{pubkey}` — authenticated.**

Request body: the full vault record dict (including `sig`).

Validation, in order (each rejection returns a discriminated JSON body):

1. **Caller-owns-key**: the session pubkey (from the Bearer token, resolved by `server-hub-spec.md` §5) MUST equal the URL `{pubkey}`. Rejection: `403 { "reason": "pubkey_mismatch" }`.
2. **URL/body coherence**: `body.pubkey` MUST equal the URL `{pubkey}`. Rejection: `400 { "reason": "pubkey_url_mismatch" }`.
3. **Envelope shape**: all top-level fields present + typed, `method_slots` in `1..8`, every slot's `type` in the known set, per-slot required fields present, labels within length cap. Rejection: `400 { "reason": "malformed", "detail": <text> }`.
4. **Membership**: `directory.resolve(pubkey)` MUST return an attestation AND `directory.is_revoked(pubkey)` MUST return false. Rejection: `403 { "reason": "not_a_member" }`. **Enforced BEFORE the sig check** — a non-member with a valid sig on garbage records must not consume CPU on sig verify.
5. **Size**: `len(JCS(body)) ≤ HubConfig.max_vault_body_bytes` (default 64 KiB). Rejection: `413 { "reason": "too_large", "limit": <n>, "size": <n> }`.
6. **Signature**: `crypto.verify(pubkey, body.sig, JCS(body minus 'sig'))` MUST return true. Rejection: `401 { "reason": "invalid_signature" }`. **Verifies against the vault-owner pubkey, NOT the org root** — this is the key protocol difference from `/admin/limits` and other root-signed admin endpoints.
7. **Storage-quota preflight** (read-only): compute `delta = new_size - old_size` (0 if no prior head). If positive, `throttler.check_storage_delta(caller, role, delta)` MUST pass without mutation. Rejection: standard throttle response.
8. **Chain CAS**: `body.prev_vault_hash` MUST equal `hash_vault(current head)` or, if no head exists, `GENESIS_SENTINEL`. Rejection: `409 { "reason": "stale_prev_hash", "head_hash": <current hash> }`. The response includes the current head hash so the client can pull-merge-retry in a single round-trip.
9. **Persist**: `VaultStore.put_atomic(...)` writes the row under a module lock.
10. **Commit quota**: `throttler.commit_storage_delta(caller, delta)` — only after CAS succeeds. Prevents quota leaks on 409.

On success:

```
200 { "pubkey": <hex>, "version": <int>, "hash": "sha256:<hex>", "updated_at": <rfc3339> }
```

The endpoint does not fan out over `/stream`. Multi-device coordination is handled by CAS + retry on the client side (§5.3), not by push. Rationale: vault writes are rare (slot add / rotate / remove) and per-user; the push channel is optimized for cross-member entry delivery.

### 4.2 Storage schema

```sql
CREATE TABLE IF NOT EXISTS vaults (
    pubkey     TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    hash       TEXT NOT NULL,           -- sha256:<hex> of the JCS-canonical body
    body       BLOB NOT NULL,           -- JCS bytes verbatim from the client
    size_bytes INTEGER NOT NULL,        -- mirrored for quota math
    updated_at TEXT NOT NULL            -- ISO 8601 UTC
);
```

- Same SQLite file as `EventStore` (`data/cove.db` in production, `data/hub.db` in tests), distinct table. Isolation of concerns without a second connection.
- WAL mode; `check_same_thread=False`; own `threading.Lock` guarding the head-read + INSERT-OR-REPLACE pair inside `put_atomic`.
- Latest-only: one row per pubkey. History is not retained hub-side. Rationale: rollback attack mitigation (§1) rests on the client's own local memory of the latest hash, not on hub-side history.

### 4.3 Throttler split

The pre-vault throttler's `reserve_storage` was single-phase: check + charge in one call. The vault endpoint requires two-phase accounting because CAS can fail *after* the throttler check has consumed budget, which would leak reservations across 409-heavy sessions. The split:

- `check_storage_delta(author, role, delta_bytes)` — pure check, no mutation. Negative deltas short-circuit to no-op (shrinking cannot exceed quota).
- `commit_storage_delta(author, delta_bytes)` — applies the signed delta. Negative deltas release. Called ONLY after the CAS write succeeds.

This mirrors the entry pipeline's `next_seq + append_atomic` no-burn-on-fail discipline (`server-hub-spec.md` §7.1).

---

## 5. Client responsibilities

### 5.1 Encryption module

The client owns two crypto boundaries: content encryption (CEK ↔ priv material) and slot key derivation (unlock method → KEK → CEK wrap). Both use standard primitives available in Web Crypto natively; no external cryptographic dependencies are required.

- **CEK**: 32 random bytes from `crypto.getRandomValues`. AES-GCM-256, 12-byte IVs per encrypt.
- **Passphrase KEK**: PBKDF2-SHA256, 600k iterations (per-slot `kdf_iterations` in case OWASP tunes), 16-byte salt.
- **Passkey KEK**: WebAuthn PRF extension, `first = sha256("cove-vault-kek-v1")`, HKDF-SHA256 with `info = "cove-vault-kek-v1"` producing 32 output bytes → imported as AES-GCM key.

The client MUST verify the vault record's Ed25519 signature (against `record.pubkey`) **before** attempting to unlock. Sig verify precedes decrypt to prevent oracles and to short-circuit malformed input.

### 5.2 JCS canonicalization

Vault hash and sig are computed over JCS-canonical bytes matching Python's `rfc8785.dumps` output byte-for-byte. The client hand-rolls a JCS canonicalizer (~30 lines) rather than pulling a dependency:

- Object keys sorted lex on UTF-16 code units (matches JS String comparison and the JCS §3.2.3 requirement).
- Strings escaped per JCS §3.2.2.2 (JSON-standard escapes only).
- Integers via ECMAScript Number.toString.
- Undefined-valued keys dropped.
- No whitespace.

Drift is caught by a shared test vector: the client suite includes a paste-in JCS output from Python `rfc8785.dumps` and asserts the client canonicalizer produces the same bytes.

### 5.3 Multi-hub replication

The client stores the vault ciphertext on **every hub the member has an authenticated session with**. Replication is client-side: no hub-to-hub sync, no gossip.

- **On write** (`saveVault`): `Promise.allSettled` push to every authenticated hub. Per-hub CAS failures (`409 stale_prev_hash`) trigger per-hub retry (fetch head → replay delta → re-sign → re-push, capped at 3 attempts). Total failure across every hub throws; partial failure surfaces via `vaultPushFailures` for user notification.
- **On read** (`loadIdentityVault`): try each joined hub in priority order (active hub first). Multiple returning candidates trigger divergence resolution (§5.4).

### 5.4 Divergence resolution

When two hubs return different vault heads for the same pubkey (a plausible outcome after a partial-failure push), the client selects the winning candidate as follows:

1. **Chain-follows-chain**: if any candidate's `prev_vault_hash` equals another candidate's `hash`, the *descendant* is strictly newer regardless of clocks. This is the primary rule.
2. **Fallback: highest `updated_at`**. Client-authored timestamp is untrusted absent chain evidence, but a signed record with a manipulated timestamp still requires the vault owner's priv to forge, so the security floor is priv custody.

After a divergence-resolving read, the next `saveVault` re-normalizes: it pushes the winning head to every hub, which will 409 on the ones that already have it (harmless) and land on the ones that were behind.

### 5.5 Session and pubkey persistence

Post-unlock, the client stores per-hub localStorage:

- `cove.vault.pubkey.<hubUrl>` — the pubkey that successfully unlocked. Drives the "Welcome back to Cove" landing on returning-user relaunch, so the user does not retype their pubkey.

Cleared on logout, on explicit "Use a different key" opt-out, and on hub removal.

Live priv material (`livePriv`), CEK (`liveCek`), and the last-loaded vault record (`liveVault`) are held only in JS heap, `$state`-tracked, and wiped on `logoutAll()`. They are never persisted to disk.

### 5.6 Sign-in flows

- **First-time onboard on device #1**: existing key-generation flow signs in via paste mode or Passkey PRF derivation (v0.4.74 identity Passkey). Immediately after `connect()` succeeds, the client calls `createIdentityVault({ firstUnlock: { kind: 'passphrase', passphrase, label } })`. Best-effort — a failed vault mint does not undo onboard.
- **Same-device relaunch**: localStorage remembers the pubkey. Landing panel shows "Welcome back to Cove"; user unlocks via Passkey (one-tap) or passphrase. Both routes call `unlockFromIdentityVault{Passphrase,Passkey}` which fetches the current vault from the active hub, decrypts, and hands the derived priv to `connect({mode: 'paste', ...})`.
- **Fresh device (no localStorage)**: user manually enters hub URL + pubkey in the AuthPanel cross-device surface, unlocks via Passkey or passphrase against the fetched vault, and is signed in without a fresh invite or re-attestation.

Vault sign-in never re-attests: the same identity (attested pubkey) that was granted on device #1 is used verbatim on device N.

---

## 6. Adding an unlock method

Slot types are discriminated on `type`. Adding a new type (e.g. YubiKey, recovery code) requires:

1. Hub side: add the type to `KNOWN_SLOT_TYPES` and its required-field set to `_SLOT_<TYPE>_REQUIRED` in `vaults.py`. Extend `validate_shape` to accept it.
2. Client side: add a `<Type>Slot` interface to `vault-blob.ts`; extend `unlockWith<Method>` and `add<Method>Slot` functions; wire buttons in AdminPanel.
3. Wire tests: hub-side `test_vault_put_accepts_<type>_slot`; client-side round-trip test.

The outer envelope is not touched. Existing slot types continue to unlock existing vaults with no migration.

For a YubiKey slot, the wrap key derivation would be YubiKey-HMAC-based; for a recovery-code slot, it would be a passphrase slot with a client-generated high-entropy phrase. Both fit the polymorphic `method_slots[]` shape without a schema bump.

---

## 7. Boundaries the vault does NOT change

- **Attestation flow**: `server-hub-spec.md` §2.2 attestation is unchanged. Vault sign-in does not attest; it *uses* an already-attested identity from any device.
- **Directory manifest**: unchanged. Vault records live in a separate table with a separate CAS chain per pubkey. Adding a vault to the hub does not touch the manifest.
- **Entry pipeline**: unchanged. Vault records are not entries; the tamper-evident log (§6.4) does not enumerate vaults.
- **Federation semantics**: a member attested by hub A and hub B (`lwccoa-pilot-state`, `deferred-slices`) writes the same vault to each. Each hub verifies the vault-owner sig independently against its own directory. The two hubs need not agree on anything about the vault.
- **`/stream` fan-out**: vault writes do not push. Multi-device coordination is CAS + retry, not push.
- **Session token issuance**: `server-hub-spec.md` §5 challenge-response is unchanged. Vault unlock happens client-side; the derived priv then flows into the standard challenge-response.

---

## 8. Deferred slot types and future extensions

The polymorphic slot shape accommodates near-term extensions without a schema bump:

- **YubiKey slot** (`type: "yubikey"`): FIDO2 hmac-secret gives us the same PRF-like shape as WebAuthn PRF; HMAC challenge-response is the alternative. Either fits the wrap-slot pattern.
- **Recovery-code slot** (`type: "recovery-code"`): a passphrase slot with a client-generated high-entropy phrase, printable by the user. Reuses the passphrase slot type verbatim; no schema change strictly required.
- **QR-code cross-device onboard**: a client-side flow that transfers a short-lived unwrap key from an existing device via QR + Bluetooth (matching WebAuthn hybrid transport). Would land as a client-only feature; no wire change.
- **Vault content extensions**: the `meta` sub-object inside `content_ciphertext` is a growth surface. Additional per-vault settings (e.g. cross-hub display preferences, invite-code shortcuts) can land there under byte-identical-when-absent rules.

Not deferred: **the vault itself is v0.4.76 core**, shipped and validated end-to-end (see `CHANGELOG.md` v0.4.76 entry).

---

## 9. Cross-references

- `server-hub-spec.md` §1 — trust model preserved
- `server-hub-spec.md` §2 — identity + directory + attestation (unchanged)
- `server-hub-spec.md` §5 — session challenge-response (unchanged; vault unlock feeds this)
- `server-hub-spec.md` §6.4 — tamper-evident log (unchanged; vaults are outside its scope)
- `server-hub-spec.md` §7.2.2 — throttler (extended with `check_storage_delta` + `commit_storage_delta`)
- `client-spec.md` §1 — key custody (extended: priv can now live in a hub-stored vault)
- `client-spec.md` §2.1 — onboarding flow (extended: vault mint at end of onboard)
- CLAUDE.md non-negotiables #1, #5 (preserved)
- `docs/dependency-risks.md` — no new dependencies introduced by the vault (Web Crypto + `@noble/curves` + `@noble/hashes` already in-tree)
