/**
 * Cove identity vault — hub-stored, multi-recipient encrypted.
 *
 * v0.4.76. This is DISTINCT from `vault.ts` (the passphrase-encrypted
 * single-device IndexedDB vault shipped in v0.4.34). Naming:
 *
 *   vault.ts       — LOCAL, single-device, per-browser IndexedDB. Being
 *                    phased out as part of the fresh-start migration.
 *   vault-blob.ts  — HUB-STORED, multi-device, multi-unlock-method. The
 *                    v0.4.76+ home for identity portability.
 *
 * How it works:
 *   1. Canonical Ed25519 priv is encrypted ONCE under a random Content-
 *      Encryption Key (CEK) — the `content_ciphertext` in the wire schema.
 *   2. Each unlock method (passphrase, Passkey PRF, later YubiKey /
 *      recovery-code) has its own AES-GCM-wrapped copy of the CEK sitting
 *      in `method_slots[].wrap_ciphertext`.
 *   3. Adding or rotating a method rewrites only that slot's wrap — the
 *      content ciphertext is minted once at vault creation and never
 *      changes (so removing a method doesn't invalidate the priv on
 *      devices that already unlocked it).
 *   4. The record is signed with the vault-owner's Ed25519 priv. The hub
 *      verifies the sig covers `canonicalize(record - 'sig')` — same JCS
 *      shape the Python side (rfc8785) produces.
 *   5. Chain: every write includes `prev_vault_hash`; the hub CASes it
 *      against the current head (409 on stale, response carries head_hash
 *      for one-round-trip retry).
 *
 * Non-negotiable #1: this module NEVER surrenders plaintext key material
 * across a network boundary. All decryption happens locally; the hub sees
 * only ciphertext. The wire schema (`vaults.py::validate_shape`) enforces
 * that the outer record carries no priv-shaped field.
 *
 * Reuse rules:
 *   - Passkey vault-KEK salt (`cove-vault-kek-v1`) is DIFFERENT from the
 *     passkey.ts identity-seed salt (`cove-passkey-prf-v1`). A leaked KEK
 *     cannot be walked back to the priv. Compile-adjacent assertion in
 *     the test suite.
 *   - Hand-rolled JCS matches Python rfc8785 output byte-for-byte. A
 *     paste-in golden vector in the test suite catches drift.
 *   - PBKDF2 params (600k, SHA-256) match vault.ts:38 — the OWASP 2023
 *     minimum. Recorded per-slot so future bumps are transparent.
 */

import { ed25519 } from '@noble/curves/ed25519';
import { hkdf } from '@noble/hashes/hkdf';
import { sha256 } from '@noble/hashes/sha256';
import { bytesToHex, hexToBytes } from '@noble/hashes/utils';

// ---- Wire schema types -------------------------------------------------

/** A vault record as it appears on the wire and in the SQLite `vaults`
 *  table. JCS-canonicalized before signing/hashing. */
export interface VaultRecord {
  pubkey: string;                    // 64-hex Ed25519 pubkey of the owner
  version: 1;
  prev_vault_hash: string;           // "sha256:<hex>" — GENESIS_PREV on first record
  content_algo: 'AES-GCM-256-v1';
  content_iv: string;                // b64url of 12 bytes
  content_ciphertext: string;        // b64url of AES-GCM(CEK, IV, plaintext)
  method_slots: MethodSlot[];
  updated_at: string;                // ISO 8601 UTC
  sig: string;                       // 128-hex Ed25519 over canonicalize(record - 'sig')
}

export interface PassphraseSlot {
  id: string;                        // 16-hex random
  type: 'passphrase';
  algo: 'PBKDF2-SHA256-AES-GCM-256-v1';
  kdf_salt: string;                  // b64url of 16 bytes
  kdf_iterations: number;
  wrap_iv: string;                   // b64url of 12 bytes
  wrap_ciphertext: string;           // b64url of AES-GCM(KEK, IV, CEK)
  label: string;
  created_at: string;
}

export interface PasskeySlot {
  id: string;
  type: 'passkey';
  algo: 'PRF-HKDF-AES-GCM-256-v1';
  credential_id: string;             // b64url of WebAuthn credentialId
  rp_id: string;
  prf_salt_tag: string;              // sha256(this) is the PRF `first` input
  hkdf_info: string;
  wrap_iv: string;
  wrap_ciphertext: string;
  label: string;
  created_at: string;
}

export type MethodSlot = PassphraseSlot | PasskeySlot;

// ---- Constants ---------------------------------------------------------

export const GENESIS_PREV = 'sha256:' + '0'.repeat(64);

/** OWASP 2023 min for PBKDF2-SHA256. Matches vault.ts:38. */
export const PBKDF2_ITERATIONS = 600_000;

/** Vault-KEK salt tag for the Passkey PRF ceremony. MUST differ from
 *  passkey.ts's `cove-passkey-prf-v1` so a leaked KEK can't be walked
 *  back to the identity priv. Compile-adjacent assertion in the tests. */
export const VAULT_PRF_SALT_TAG = 'cove-vault-kek-v1';
export const VAULT_HKDF_INFO = 'cove-vault-kek-v1';

const CONTENT_ALGO: 'AES-GCM-256-v1' = 'AES-GCM-256-v1';
const PASSPHRASE_ALGO: 'PBKDF2-SHA256-AES-GCM-256-v1' = 'PBKDF2-SHA256-AES-GCM-256-v1';
const PASSKEY_ALGO: 'PRF-HKDF-AES-GCM-256-v1' = 'PRF-HKDF-AES-GCM-256-v1';

// ---- JCS canonicalizer (RFC 8785) -------------------------------------

/** Deterministic RFC 8785 serialization. Matches Python `rfc8785.dumps`
 *  byte-for-byte. Rules:
 *    - Object keys sorted lex on UTF-16 code units (JS String.prototype
 *      comparison already does this).
 *    - Array order preserved.
 *    - No whitespace.
 *    - Strings escaped per RFC 8785 §3.2.2.2 (JSON-standard escapes only).
 *    - Numbers per §3.2.2.3 — integers → decimal digits, floats via ES
 *      Number.prototype.toString (matches ECMAScript). We only pass
 *      integers on the vault path (version, iterations), so the float
 *      corner cases are moot but tested for completeness.
 *
 *  Hand-rolled because there's no zero-dep npm JCS impl I trust to stay
 *  drift-free with the Python side. A single test vector against Python
 *  rfc8785 catches drift immediately. */
export function canonicalize(obj: unknown): Uint8Array {
  return new TextEncoder().encode(canonicalJSON(obj));
}

function canonicalJSON(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('JCS: non-finite number');
    // ECMAScript Number.toString matches JCS §3.2.2.3 for typical values.
    return String(value);
  }
  if (typeof value === 'string') return escapeString(value);
  if (Array.isArray(value)) {
    return '[' + value.map(canonicalJSON).join(',') + ']';
  }
  if (typeof value === 'object') {
    const keys = Object.keys(value as Record<string, unknown>);
    // JS string comparison is on UTF-16 code units, which is what JCS
    // §3.2.3 requires. Any surrogate-pair-heavy key edge cases would
    // need a codepoint sort — none appear on our vault path.
    keys.sort();
    const parts: string[] = [];
    for (const k of keys) {
      const v = (value as Record<string, unknown>)[k];
      if (v === undefined) continue;  // JSON: undefined-valued keys drop
      parts.push(escapeString(k) + ':' + canonicalJSON(v));
    }
    return '{' + parts.join(',') + '}';
  }
  throw new Error(`JCS: unsupported value type: ${typeof value}`);
}

/** RFC 8785 §3.2.2.2. Only the JSON-standard escapes; everything else is
 *  copied verbatim as UTF-8. */
function escapeString(s: string): string {
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c === 0x22) out += '\\"';
    else if (c === 0x5c) out += '\\\\';
    else if (c === 0x08) out += '\\b';
    else if (c === 0x0c) out += '\\f';
    else if (c === 0x0a) out += '\\n';
    else if (c === 0x0d) out += '\\r';
    else if (c === 0x09) out += '\\t';
    else if (c < 0x20) out += '\\u' + c.toString(16).padStart(4, '0');
    else out += s[i];
  }
  return out + '"';
}

// ---- Base64URL (no padding) -------------------------------------------

function b64urlEncode(bytes: Uint8Array): string {
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function b64urlDecode(s: string): Uint8Array {
  const padded = s.replace(/-/g, '+').replace(/_/g, '/')
    + '='.repeat((4 - (s.length % 4)) % 4);
  const bin = atob(padded);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function randomBytes(n: number): Uint8Array {
  return crypto.getRandomValues(new Uint8Array(n));
}

function randomId(): string {
  return bytesToHex(randomBytes(8));   // 16 hex chars = 64 bits — plenty for slot IDs
}

function nowIso(): string {
  return new Date().toISOString();
}

// ---- Hashing + signing helpers ----------------------------------------

/** Hash a vault record. Covers the FULL record including `sig` — so the
 *  next record's `prev_vault_hash` chains against a value that includes
 *  the signature, preventing sig-swap attacks. Mirrors Python
 *  `vaults.hash_vault` at src/cove/vaults.py. */
export async function hashVault(v: VaultRecord): Promise<string> {
  const bytes = canonicalize(v);
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes as BufferSource));
  return 'sha256:' + bytesToHex(digest);
}

/** Sign an unsigned vault record. `sig` field is ignored/replaced. Returns
 *  a new frozen record with the sig set — does not mutate the input. */
export async function signVault(
  unsigned: Omit<VaultRecord, 'sig'>,
  ownerPriv: string,
): Promise<VaultRecord> {
  const withoutSig = { ...unsigned } as Record<string, unknown>;
  const bytes = canonicalize(withoutSig);
  const sig = bytesToHex(ed25519.sign(bytes, hexToBytes(ownerPriv)));
  return { ...unsigned, sig } as VaultRecord;
}

// ---- KEK derivation ---------------------------------------------------

/** Derive an AES-GCM CryptoKey from a passphrase + PBKDF2 params. */
async function passphraseKek(
  passphrase: string,
  salt: Uint8Array,
  iterations: number,
): Promise<CryptoKey> {
  const baseKey = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(passphrase) as BufferSource,
    { name: 'PBKDF2' },
    false,
    ['deriveKey'],
  );
  return crypto.subtle.deriveKey(
    { name: 'PBKDF2', hash: 'SHA-256', salt: salt as BufferSource, iterations },
    baseKey,
    { name: 'AES-GCM', length: 256 },
    true,   // extractable — we don't export, but tests occasionally need it
    ['encrypt', 'decrypt'],
  );
}

/** Derive an AES-GCM CryptoKey from a raw HKDF-produced 32-byte seed. */
async function importAesGcmKey(rawKey: Uint8Array): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    'raw',
    rawKey as BufferSource,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt', 'decrypt'],
  );
}

// ---- Passkey PRF ceremony (KEK) ---------------------------------------

interface PrfResults { first?: ArrayBuffer }
interface PrfExtensionOutput { enabled?: boolean; results?: PrfResults }

function extractPrfOutput(cred: PublicKeyCredential): ArrayBuffer {
  const extensions = (cred as unknown as {
    getClientExtensionResults(): { prf?: PrfExtensionOutput };
  }).getClientExtensionResults();
  const first = extensions?.prf?.results?.first;
  if (!first) {
    throw new Error(
      'This browser or authenticator did not return the PRF output. '
      + 'Passkey vault-unlock needs the PRF extension. Try Chrome / '
      + 'Safari 17+ / a recent Android build, or use the passphrase '
      + 'unlock instead.',
    );
  }
  return first;
}

function vaultPrfSalt(): Uint8Array {
  return sha256(new TextEncoder().encode(VAULT_PRF_SALT_TAG));
}

function rpIdForVault(): string {
  if (typeof window === 'undefined') return 'cove.oap.dev';
  const host = window.location.hostname;
  if (host === 'localhost' || host === '127.0.0.1') return host;
  const parts = host.split('.');
  return parts.length >= 3 ? parts.slice(1).join('.') : host;
}

/** Register a NEW Passkey ceremony for the vault KEK. Returns the
 *  credential ID + a 32-byte AES-GCM key derived from the PRF output.
 *
 *  `label` becomes the WebAuthn user.name and user.displayName — that's
 *  what the OS Passkey picker shows at unlock time. Without this, every
 *  Cove vault Passkey registered as "cove-vault" and the picker
 *  couldn't distinguish between them (v0.4.76 bug: two Passkeys, same
 *  displayed name, guesswork at unlock). */
async function registerPasskeyKek(label: string): Promise<{
  credentialId: string;
  kek: CryptoKey;
}> {
  const challenge = randomBytes(32);
  const userId = randomBytes(16);
  const salt = vaultPrfSalt();
  // WebAuthn user fields shown by the OS Passkey picker:
  //   displayName = human name (large in the picker)
  //   name        = username-ish (smaller, below the displayName)
  // Convention on Apple: both fields are shown; using the label for
  // both is the clearest UX. Prefix the picker label with "Cove vault"
  // so if the OS ever collapses names across sites, it's obvious this
  // Passkey belongs to Cove.
  const displayName = `Cove vault: ${label}`;
  const userName = label;
  const cred = await navigator.credentials.create({
    publicKey: {
      rp: { id: rpIdForVault(), name: 'Cove' },
      user: { id: userId as BufferSource, name: userName, displayName },
      challenge: challenge as BufferSource,
      pubKeyCredParams: [
        { type: 'public-key', alg: -8 },
        { type: 'public-key', alg: -7 },
      ],
      authenticatorSelection: {
        userVerification: 'required',
        residentKey: 'required',
      },
      extensions: {
        prf: { eval: { first: salt as BufferSource } },
      } as AuthenticationExtensionsClientInputs,
    },
  }) as PublicKeyCredential | null;
  if (!cred) throw new Error('Passkey creation was cancelled.');
  const prfOutput = extractPrfOutput(cred);
  const rawKek = hkdf(sha256, new Uint8Array(prfOutput), undefined,
                     new TextEncoder().encode(VAULT_HKDF_INFO), 32);
  const kek = await importAesGcmKey(rawKek);
  return {
    credentialId: b64urlEncode(new Uint8Array(cred.rawId)),
    kek,
  };
}

/** Unlock via EXISTING Passkey ceremonies. Iterates the vault's Passkey
 *  slots, offers all their credential IDs to the OS picker in one ceremony,
 *  then tries each slot's wrap against the derived KEK until AES-GCM
 *  succeeds. */
async function unlockPasskeyKek(slots: PasskeySlot[]): Promise<{
  matchedSlot: PasskeySlot;
  kek: CryptoKey;
}> {
  if (slots.length === 0) throw new Error('no Passkey slots on this vault');
  const challenge = randomBytes(32);
  const salt = vaultPrfSalt();
  const cred = await navigator.credentials.get({
    publicKey: {
      rpId: rpIdForVault(),
      challenge: challenge as BufferSource,
      allowCredentials: slots.map((s) => ({
        type: 'public-key' as const,
        id: b64urlDecode(s.credential_id) as BufferSource,
        transports: ['internal', 'hybrid'] as AuthenticatorTransport[],
      })),
      userVerification: 'required',
      extensions: {
        prf: { eval: { first: salt as BufferSource } },
      } as AuthenticationExtensionsClientInputs,
    },
  }) as PublicKeyCredential | null;
  if (!cred) throw new Error('Passkey sign-in was cancelled.');
  const prfOutput = extractPrfOutput(cred);
  const rawKek = hkdf(sha256, new Uint8Array(prfOutput), undefined,
                     new TextEncoder().encode(VAULT_HKDF_INFO), 32);
  const kek = await importAesGcmKey(rawKek);

  // Which slot did the user's Passkey correspond to? Match by credential ID.
  const usedId = b64urlEncode(new Uint8Array(cred.rawId));
  const matched = slots.find((s) => s.credential_id === usedId);
  if (!matched) {
    throw new Error(
      'the Passkey the user selected is not one of this vault\'s slots — '
      + 'the credential ID did not match',
    );
  }
  return { matchedSlot: matched, kek };
}

// ---- Content encryption (CEK ↔ priv material) -------------------------

/** v0.4.84: `meta.hubs` is a synced list of hub URLs the user is a
 *  member of. Every device that unlocks the vault reads this on unlock
 *  and merges into local `cove.hubs` — so adding a hub on one device
 *  propagates to every other device automatically. Additive-merge
 *  semantics: local additions win over vault absence, and CAS on
 *  the vault push handles concurrent edits. */
export interface VaultContent {
  priv: string;
  pub: string;
  created_at: string;
  meta: { note?: string; hubs?: string[] };
}

async function encryptContent(
  cek: Uint8Array,
  content: VaultContent,
): Promise<{ content_iv: string; content_ciphertext: string }> {
  const cekKey = await importAesGcmKey(cek);
  const iv = randomBytes(12);
  const plaintext = canonicalize(content as unknown);
  const ct = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: iv as BufferSource },
    cekKey,
    plaintext as BufferSource,
  );
  return {
    content_iv: b64urlEncode(iv),
    content_ciphertext: b64urlEncode(new Uint8Array(ct)),
  };
}

async function decryptContent(
  cek: Uint8Array,
  content_iv: string,
  content_ciphertext: string,
): Promise<VaultContent> {
  const cekKey = await importAesGcmKey(cek);
  const pt = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: b64urlDecode(content_iv) as BufferSource },
    cekKey,
    b64urlDecode(content_ciphertext) as BufferSource,
  );
  const text = new TextDecoder().decode(pt);
  return JSON.parse(text) as VaultContent;
}

/** v0.4.84: expose decrypt for AppState so it can read the synced hub
 *  list after unlock. Kept out of the main unlock functions because
 *  those return only the priv (backward compat); this reads the
 *  content object explicitly. */
export async function readVaultContent(
  cek: Uint8Array,
  vault: VaultRecord,
): Promise<VaultContent> {
  return decryptContent(cek, vault.content_iv, vault.content_ciphertext);
}

/** v0.4.84: rebuild the vault with an updated hub list in
 *  meta.hubs. The CEK is preserved (content is re-encrypted with a
 *  fresh IV under the same CEK). Bumps prev_vault_hash to the prior
 *  record's hash and re-signs with owner priv. */
export async function updateVaultHubs(opts: {
  vault: VaultRecord;
  cek: Uint8Array;
  ownerPriv: string;
  hubs: string[];
}): Promise<VaultRecord> {
  const priorContent = await decryptContent(
    opts.cek, opts.vault.content_iv, opts.vault.content_ciphertext,
  );
  const nextContent: VaultContent = {
    priv: priorContent.priv,
    pub: priorContent.pub,
    created_at: priorContent.created_at,
    meta: { ...priorContent.meta, hubs: [...opts.hubs] },
  };
  const { content_iv, content_ciphertext } = await encryptContent(
    opts.cek, nextContent,
  );
  const priorHash = await hashVault(opts.vault);
  const unsigned: Omit<VaultRecord, 'sig'> = {
    pubkey: opts.vault.pubkey,
    version: 1,
    prev_vault_hash: priorHash,
    content_algo: opts.vault.content_algo,
    content_iv,
    content_ciphertext,
    method_slots: opts.vault.method_slots,
    updated_at: nowIso(),
  };
  return signVault(unsigned, opts.ownerPriv);
}

// ---- CEK wrap/unwrap per slot -----------------------------------------

async function wrapCekWithKek(cek: Uint8Array, kek: CryptoKey): Promise<{
  wrap_iv: string;
  wrap_ciphertext: string;
}> {
  const iv = randomBytes(12);
  const ct = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: iv as BufferSource },
    kek,
    cek as BufferSource,
  );
  return {
    wrap_iv: b64urlEncode(iv),
    wrap_ciphertext: b64urlEncode(new Uint8Array(ct)),
  };
}

async function unwrapCekWithKek(
  kek: CryptoKey,
  wrap_iv: string,
  wrap_ciphertext: string,
): Promise<Uint8Array> {
  const pt = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: b64urlDecode(wrap_iv) as BufferSource },
    kek,
    b64urlDecode(wrap_ciphertext) as BufferSource,
  );
  return new Uint8Array(pt);
}

// ---- Slot builders ----------------------------------------------------

async function buildPassphraseSlot(
  cek: Uint8Array,
  passphrase: string,
  label: string,
): Promise<PassphraseSlot> {
  if (passphrase.length < 12) {
    throw new Error('passphrase must be at least 12 characters');
  }
  const salt = randomBytes(16);
  const kek = await passphraseKek(passphrase, salt, PBKDF2_ITERATIONS);
  const { wrap_iv, wrap_ciphertext } = await wrapCekWithKek(cek, kek);
  return {
    id: randomId(),
    type: 'passphrase',
    algo: PASSPHRASE_ALGO,
    kdf_salt: b64urlEncode(salt),
    kdf_iterations: PBKDF2_ITERATIONS,
    wrap_iv,
    wrap_ciphertext,
    label,
    created_at: nowIso(),
  };
}

async function buildPasskeySlot(
  cek: Uint8Array,
  label: string,
): Promise<PasskeySlot> {
  const { credentialId, kek } = await registerPasskeyKek(label);
  const { wrap_iv, wrap_ciphertext } = await wrapCekWithKek(cek, kek);
  return {
    id: randomId(),
    type: 'passkey',
    algo: PASSKEY_ALGO,
    credential_id: credentialId,
    rp_id: rpIdForVault(),
    prf_salt_tag: VAULT_PRF_SALT_TAG,
    hkdf_info: VAULT_HKDF_INFO,
    wrap_iv,
    wrap_ciphertext,
    label,
    created_at: nowIso(),
  };
}

// ---- Public API: create + mutate + unlock -----------------------------

export type FirstUnlockChoice =
  | { kind: 'passphrase'; passphrase: string; label?: string }
  | { kind: 'passkey'; label?: string };

/** Mint a brand-new vault around an existing priv/pub pair. `firstUnlock`
 *  determines the sole initial slot; add more via addPassphraseSlot /
 *  addPasskeySlot after the first save. */
export async function createVault(opts: {
  priv: string;
  pub: string;
  firstUnlock: FirstUnlockChoice;
  note?: string;
  /** v0.4.84: hub URLs to seed the synced hub list with. Every device
   *  that unlocks the vault reads meta.hubs and merges into local
   *  cove.hubs, so a new device signing in inherits the user's hub
   *  membership. Undefined = no hubs baked in (single-hub scenarios
   *  or fresh onboards). */
  hubs?: string[];
}): Promise<VaultRecord> {
  const cek = randomBytes(32);
  const meta: VaultContent['meta'] = {};
  if (opts.note !== undefined) meta.note = opts.note;
  if (opts.hubs !== undefined && opts.hubs.length > 0) meta.hubs = [...opts.hubs];
  const content: VaultContent = {
    priv: opts.priv,
    pub: opts.pub,
    created_at: nowIso(),
    meta,
  };
  const { content_iv, content_ciphertext } = await encryptContent(cek, content);
  const slot: MethodSlot = opts.firstUnlock.kind === 'passphrase'
    ? await buildPassphraseSlot(cek, opts.firstUnlock.passphrase,
                                opts.firstUnlock.label ?? 'Passphrase')
    : await buildPasskeySlot(cek, opts.firstUnlock.label ?? 'Passkey');
  const unsigned: Omit<VaultRecord, 'sig'> = {
    pubkey: opts.pub,
    version: 1,
    prev_vault_hash: GENESIS_PREV,
    content_algo: CONTENT_ALGO,
    content_iv,
    content_ciphertext,
    method_slots: [slot],
    updated_at: nowIso(),
  };
  return signVault(unsigned, opts.priv);
}

/** Add a passphrase slot to an existing vault. Caller must have already
 *  unlocked the vault to obtain the CEK — that's what proves they have
 *  the right to add a new unlock method. */
export async function addPassphraseSlot(opts: {
  vault: VaultRecord;
  cek: Uint8Array;
  ownerPriv: string;
  passphrase: string;
  label: string;
}): Promise<VaultRecord> {
  const slot = await buildPassphraseSlot(opts.cek, opts.passphrase, opts.label);
  return chainNextVault(opts.vault, opts.ownerPriv, {
    method_slots: [...opts.vault.method_slots, slot],
  });
}

/** Add a Passkey slot to an existing vault. Runs a WebAuthn ceremony to
 *  obtain a fresh credential + PRF-derived KEK. */
export async function addPasskeySlot(opts: {
  vault: VaultRecord;
  cek: Uint8Array;
  ownerPriv: string;
  label: string;
}): Promise<VaultRecord> {
  const slot = await buildPasskeySlot(opts.cek, opts.label);
  return chainNextVault(opts.vault, opts.ownerPriv, {
    method_slots: [...opts.vault.method_slots, slot],
  });
}

/** Remove a slot by id. Refuses to drop the last slot — that would
 *  brick the vault. */
export async function removeSlot(opts: {
  vault: VaultRecord;
  slotId: string;
  ownerPriv: string;
}): Promise<VaultRecord> {
  const remaining = opts.vault.method_slots.filter((s) => s.id !== opts.slotId);
  if (remaining.length === opts.vault.method_slots.length) {
    throw new Error(`slot ${opts.slotId} not found`);
  }
  if (remaining.length === 0) {
    throw new Error(
      'refusing to remove the last unlock method — you would lock '
      + 'yourself out. Add another method first, then remove this one.',
    );
  }
  return chainNextVault(opts.vault, opts.ownerPriv, {
    method_slots: remaining,
  });
}

/** Rewrite the vault with a delta, chaining prev_vault_hash to the prior
 *  record's hash. Bumps updated_at. Content_ciphertext + content_iv are
 *  preserved (CEK doesn't change on slot-list edits). Signs with the
 *  owner priv. */
async function chainNextVault(
  prior: VaultRecord,
  ownerPriv: string,
  delta: Partial<Omit<VaultRecord, 'pubkey' | 'version' | 'sig'>>,
): Promise<VaultRecord> {
  const priorHash = await hashVault(prior);
  const unsigned: Omit<VaultRecord, 'sig'> = {
    pubkey: prior.pubkey,
    version: 1,
    prev_vault_hash: priorHash,
    content_algo: prior.content_algo,
    content_iv: prior.content_iv,
    content_ciphertext: prior.content_ciphertext,
    method_slots: delta.method_slots ?? prior.method_slots,
    updated_at: nowIso(),
  };
  return signVault(unsigned, ownerPriv);
}

/** Try to unlock a vault with a passphrase. Iterates every passphrase
 *  slot until one AES-GCM decrypts successfully. Throws on complete
 *  miss. Returns the derived priv/pub AND the CEK — the CEK is what the
 *  caller needs to add / remove / rotate additional slots. */
export async function unlockWithPassphrase(opts: {
  vault: VaultRecord;
  passphrase: string;
}): Promise<{ priv: string; pub: string; cek: Uint8Array }> {
  const passphraseSlots = opts.vault.method_slots.filter(
    (s): s is PassphraseSlot => s.type === 'passphrase',
  );
  if (passphraseSlots.length === 0) {
    throw new Error('this vault has no passphrase slots — use Passkey unlock');
  }
  for (const slot of passphraseSlots) {
    try {
      const kek = await passphraseKek(
        opts.passphrase,
        b64urlDecode(slot.kdf_salt),
        slot.kdf_iterations,
      );
      const cek = await unwrapCekWithKek(kek, slot.wrap_iv, slot.wrap_ciphertext);
      const content = await decryptContent(
        cek, opts.vault.content_iv, opts.vault.content_ciphertext,
      );
      if (content.pub !== opts.vault.pubkey) {
        throw new Error('vault content pub mismatch — record tampered');
      }
      return { priv: content.priv, pub: content.pub, cek };
    } catch {
      // AES-GCM tag failure — wrong passphrase for this slot. Try the next.
    }
  }
  throw new Error('none of the passphrase slots accepted this passphrase');
}

/** Try to unlock a vault via a Passkey ceremony. Offers every Passkey
 *  slot's credentialId to the OS picker; whichever the user selects
 *  determines which slot's wrap gets unwrapped. */
export async function unlockWithPasskey(opts: {
  vault: VaultRecord;
}): Promise<{ priv: string; pub: string; cek: Uint8Array }> {
  const passkeySlots = opts.vault.method_slots.filter(
    (s): s is PasskeySlot => s.type === 'passkey',
  );
  const { matchedSlot, kek } = await unlockPasskeyKek(passkeySlots);
  const cek = await unwrapCekWithKek(
    kek, matchedSlot.wrap_iv, matchedSlot.wrap_ciphertext,
  );
  const content = await decryptContent(
    cek, opts.vault.content_iv, opts.vault.content_ciphertext,
  );
  if (content.pub !== opts.vault.pubkey) {
    throw new Error('vault content pub mismatch — record tampered');
  }
  return { priv: content.priv, pub: content.pub, cek };
}
