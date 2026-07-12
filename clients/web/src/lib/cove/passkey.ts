/**
 * WebAuthn Passkey → deterministic Ed25519 keypair for the PWA.
 *
 * v0.4.74. The federation slice (v0.4.68–v0.4.73) shipped
 * "one keypair, N hubs." This module ships "one keypair per PERSON,
 * synced across their devices" — the piece of portability that was
 * missing when a person had a phone PWA + laptop PWA that each
 * generated their own random keypair.
 *
 * How it works:
 *   1. Passkey is registered against `cove.oap.dev` (parent RP ID
 *      so it covers all `*.cove.oap.dev` origins).
 *   2. On both create() and get(), we enable the WebAuthn PRF
 *      extension with a fixed 32-byte salt — the platform authenticator
 *      returns 32 bytes of stable pseudo-random output.
 *   3. HKDF-SHA256 turns that into an Ed25519 seed.
 *   4. `@noble/curves/ed25519` derives the priv+pub from the seed.
 *   5. Same Passkey (synced via iCloud Keychain / Google Password
 *      Manager) → same PRF output → same priv on every device.
 *
 * Crucially: Cove's protocol layer sees a normal Ed25519 sig over
 * canonical bytes, exactly like today. Verifiers on the Python hub +
 * TS client don't change at all. Only the SOURCE of the priv material
 * changes.
 *
 * Threat model:
 *   - Priv lives briefly in JS heap during signing (same window as
 *     today's paste mode). The wall between "web page can see priv"
 *     and "web page can't" is the Passkey ceremony itself — user has
 *     to biometric/PIN through every unlock.
 *   - Passkey is bound to `cove.oap.dev`; a phishing origin can't
 *     get a challenge signed against it.
 *   - PRF salt is a Cove-specific constant; a hypothetical other RP
 *     using the same Passkey with a different salt gets a different
 *     PRF output (WebAuthn spec guarantees this).
 *   - Tauri desktop stays on OS-keychain custody (separate origin;
 *     Passkey doesn't apply there). See docs/plan file for why.
 *
 * Kept as a separate module (not folded into vault.ts) so the two
 * paths are cleanly independent — different IndexedDB database,
 * different unlock ceremony, different threat model.
 */

import { hkdf } from '@noble/hashes/hkdf';
import { sha256 } from '@noble/hashes/sha256';
import { ed25519 } from '@noble/curves/ed25519';
import { bytesToHex } from '@noble/hashes/utils';

const DB_NAME = 'cove-passkey';
const STORE_NAME = 'credentials';
const DB_VERSION = 1;

/** Cove-specific constant. Passed as the `first` PRF eval argument on
 *  every ceremony so the authenticator's PRF output is stable across
 *  create/get and across devices. Different apps using the same
 *  Passkey with a different salt would get a different PRF output
 *  (WebAuthn spec §11.10). */
const PRF_SALT = sha256(new TextEncoder().encode('cove-passkey-prf-v1'));

/** HKDF info string. Tags the derivation so a future version can
 *  rotate cleanly (bump to v2). */
const HKDF_INFO = new TextEncoder().encode('cove-ed25519-seed-v1');

/** WebAuthn RP identifier. Parent domain so a Passkey created via
 *  `app.cove.oap.dev` is usable from any hub origin the user connects
 *  to (`lwccoa-hub.oap.dev`, `brooks-hub.oap.dev`, etc.) — WebAuthn
 *  allows the rp.id to be a suffix of the origin. Value is
 *  configurable via URL for local dev; falls back to the parent of
 *  the current origin. */
function rpId(): string {
  if (typeof window === 'undefined') return 'cove.oap.dev';
  const host = window.location.hostname;
  if (host === 'localhost' || host === '127.0.0.1') return host;
  // Strip the leftmost label so `app.cove.oap.dev` → `cove.oap.dev`,
  // `lwccoa-hub.oap.dev` → `oap.dev` (which won't validate, so the
  // browser will refuse the ceremony and we'll surface the error).
  // Users can pin the RP ID by hosting the PWA at `cove.oap.dev`.
  const parts = host.split('.');
  return parts.length >= 3 ? parts.slice(1).join('.') : host;
}

const ALGO_TAG = 'PRF-HKDF-Ed25519-v1';

export interface PasskeyStatus {
  /** Whether the client has a Passkey record registered on this device. */
  exists: boolean;
  /** Derived Ed25519 pubkey. Surfaced in the UI so the user sees
   *  "Welcome back, abc12345…" before biometric prompt. */
  pubkey?: string;
  /** WebAuthn credential ID (base64url). Handed to
   *  navigator.credentials.get() as an allowCredentials hint on
   *  subsequent unlocks. */
  credentialId?: string;
  /** ISO 8601. */
  created_at?: string;
}

interface PasskeyRecord {
  pubkey: string;
  credentialId: string;
  algo: string;
  created_at: string;
}

async function openDB(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: 'pubkey' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error('IDB open failed'));
  });
}

async function withStore<T>(
  mode: IDBTransactionMode,
  fn: (store: IDBObjectStore) => T | Promise<T>,
): Promise<T> {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, mode);
    const store = tx.objectStore(STORE_NAME);
    let result: T;
    Promise.resolve(fn(store)).then((v) => { result = v; }, reject);
    tx.oncomplete = () => resolve(result);
    tx.onerror = () => reject(tx.error ?? new Error('IDB transaction error'));
    tx.onabort = () => reject(tx.error ?? new Error('IDB transaction aborted'));
  });
}

async function readOne(): Promise<PasskeyRecord | null> {
  return withStore('readonly', (store) =>
    new Promise<PasskeyRecord | null>((resolve, reject) => {
      const req = store.getAll();
      req.onsuccess = () => {
        const records = req.result as PasskeyRecord[];
        resolve(records.length > 0 ? records[0] : null);
      };
      req.onerror = () => reject(req.error);
    }),
  );
}

/** Feature detect. Optimistic-by-default: returns true unless we can
 *  positively determine that Passkey WON'T work here. We deliberately do
 *  NOT gate on `getClientCapabilities` reporting `extension:prf` — early
 *  cut of that API (Chrome 133 / Safari 18) doesn't consistently list
 *  PRF even on browsers that support it, and my v0.4.74 strict-check
 *  produced false negatives that silently hid the Passkey chooser on
 *  otherwise-capable Macs.
 *
 *  Definitive negatives (return false):
 *    - No `PublicKeyCredential` interface at all
 *    - `isUserVerifyingPlatformAuthenticatorAvailable()` explicitly false
 *
 *  Everything else returns true. If PRF genuinely isn't there,
 *  `registerPasskey()` throws with a clear message at ceremony time —
 *  better UX than silently hiding the whole affordance.
 */
export async function passkeySupported(): Promise<boolean> {
  if (typeof window === 'undefined') return false;
  if (!('credentials' in navigator)) return false;
  if (!('PublicKeyCredential' in window)) return false;
  const PKC = (window as unknown as {
    PublicKeyCredential: {
      isUserVerifyingPlatformAuthenticatorAvailable?: () => Promise<boolean>;
    };
  }).PublicKeyCredential;
  try {
    const uv = await PKC.isUserVerifyingPlatformAuthenticatorAvailable?.();
    if (uv === false) return false;
  } catch { /* older browsers throw; carry on optimistically */ }
  return true;
}

export async function passkeyStatus(): Promise<PasskeyStatus> {
  try {
    const record = await readOne();
    if (!record) return { exists: false };
    return {
      exists: true,
      pubkey: record.pubkey,
      credentialId: record.credentialId,
      created_at: record.created_at,
    };
  } catch {
    // IndexedDB unavailable — no Passkey registration to speak of.
    return { exists: false };
  }
}

/** Base64URL encode/decode without padding. WebAuthn credential IDs
 *  round-trip through this shape. */
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

interface PrfResults {
  first?: ArrayBuffer;
}
interface PrfExtensionOutput {
  enabled?: boolean;
  results?: PrfResults;
}

/** Turn the raw PRF output into an Ed25519 keypair. Deterministic. */
function deriveKeypair(prfOutput: ArrayBuffer): { priv: string; pub: string } {
  const ikm = new Uint8Array(prfOutput);
  const seed = hkdf(sha256, ikm, undefined, HKDF_INFO, 32);
  const pubBytes = ed25519.getPublicKey(seed);
  return { priv: bytesToHex(seed), pub: bytesToHex(pubBytes) };
}

/** Coerce the mock/browser response into the PRF output we need. */
function extractPrfOutput(cred: PublicKeyCredential): ArrayBuffer {
  const extensions = (cred as unknown as {
    getClientExtensionResults(): { prf?: PrfExtensionOutput };
  }).getClientExtensionResults();
  const first = extensions?.prf?.results?.first;
  if (!first) {
    throw new Error(
      'This browser or authenticator didn\'t return the PRF output. '
      + 'Passkey identity needs the PRF extension. Try Chrome / Safari '
      + '17+ / a recent Android build, or use the passphrase flow.',
    );
  }
  return first;
}

/** Register a new Passkey and derive the Ed25519 keypair from its PRF
 *  output. Persists the credential ID + derived pubkey to IDB.
 *  Single-identity-per-device: clears any prior record before writing. */
export async function registerPasskey(): Promise<{
  priv: string;
  pub: string;
  credentialId: string;
}> {
  const challenge = crypto.getRandomValues(new Uint8Array(32));
  const userId = crypto.getRandomValues(new Uint8Array(16));
  const cred = await navigator.credentials.create({
    publicKey: {
      rp: { id: rpId(), name: 'Cove' },
      user: {
        id: userId,
        name: 'cove-user',
        displayName: 'Cove',
      },
      challenge,
      pubKeyCredParams: [
        { type: 'public-key', alg: -8 },   // Ed25519 (WebAuthn signing alg)
        { type: 'public-key', alg: -7 },   // ES256 fallback
      ],
      authenticatorSelection: {
        userVerification: 'required',
        residentKey: 'required',
      },
      extensions: {
        // The whole point.
        prf: { eval: { first: PRF_SALT as BufferSource } },
      } as AuthenticationExtensionsClientInputs,
    },
  }) as PublicKeyCredential | null;
  if (!cred) throw new Error('Passkey creation was cancelled.');

  const prfOutput = extractPrfOutput(cred);
  const { priv, pub } = deriveKeypair(prfOutput);
  const credentialId = b64urlEncode(new Uint8Array(cred.rawId));

  const record: PasskeyRecord = {
    pubkey: pub,
    credentialId,
    algo: ALGO_TAG,
    created_at: new Date().toISOString(),
  };
  await withStore('readwrite', (store) => {
    store.clear();
    store.put(record);
  });

  return { priv, pub, credentialId };
}

/** Sign in with an existing Passkey. Reads the persisted credential ID
 *  as an allowCredentials hint (so the OS picker preselects the right
 *  one), challenges via navigator.credentials.get, re-derives the priv.
 *  Verifies the derived pub matches the persisted pub — a mismatch
 *  indicates the user picked a different Passkey or the credential
 *  was reset. */
export async function unlockWithPasskey(): Promise<{
  priv: string;
  pub: string;
}> {
  const record = await readOne();
  if (!record) {
    throw new Error('no Passkey on this device — create one first');
  }
  if (record.algo !== ALGO_TAG) {
    throw new Error(`unknown Passkey algorithm ${record.algo}`);
  }
  const challenge = crypto.getRandomValues(new Uint8Array(32));
  const credIdBytes = b64urlDecode(record.credentialId);
  const cred = await navigator.credentials.get({
    publicKey: {
      rpId: rpId(),
      challenge,
      // v0.6.4: no transports hint — see vault-blob.ts note. Empty
      // picker on macOS-without-Touch-ID before falling through to
      // hybrid was confusing users on desktop Macs.
      allowCredentials: [{
        type: 'public-key',
        id: credIdBytes as BufferSource,
      }],
      userVerification: 'required',
      extensions: {
        prf: { eval: { first: PRF_SALT as BufferSource } },
      } as AuthenticationExtensionsClientInputs,
    },
  }) as PublicKeyCredential | null;
  if (!cred) throw new Error('Passkey sign-in was cancelled.');

  const prfOutput = extractPrfOutput(cred);
  const { priv, pub } = deriveKeypair(prfOutput);
  if (pub !== record.pubkey) {
    throw new Error(
      'That Passkey unlocks a different identity than the one stored '
      + 'on this device. Either the Passkey was reset, or a different '
      + 'credential was selected. Use "Forget this Passkey" and start '
      + 'over.',
    );
  }
  return { priv, pub };
}

export async function clearPasskeyStorage(): Promise<void> {
  try {
    await withStore('readwrite', (store) => store.clear());
  } catch {
    // Mirrors vault.clearVault: nothing to clear if IDB is unavailable.
  }
}
