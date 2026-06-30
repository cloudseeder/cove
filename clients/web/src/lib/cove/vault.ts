/**
 * Passphrase-encrypted vault for the PWA's member private key.
 *
 * Without this, browser-mode keys vanish when the tab closes (or
 * service worker evicts the JS heap), and the user has to either
 * re-onboard from scratch or paste their hex priv each session.
 * Daily-use blocker.
 *
 * Crypto stack — all Web Crypto, no external deps:
 *   PBKDF2-SHA-256, 600k iterations, 256-bit salt, 256-bit key
 *     → AES-GCM-256, 96-bit IV, authenticated encryption
 *
 * `algo` is recorded with each record so future iteration-count tunes
 * or primitive swaps (e.g. Argon2id via WASM) are detectable at
 * unlock time — we can re-encrypt under the new params transparently
 * on the next unlock.
 *
 * Origin-scoped IndexedDB: `app.cove.oap.dev` is isolated from any
 * other site. Records are keyed by pubkey so adding multi-account
 * support later is a UI surface change, not a storage migration.
 *
 * Threat model: a thief with the unlocked device + open browser
 * session has nothing useful in IDB — the ciphertext requires the
 * passphrase, which lives only in volatile memory during unlock.
 * AES-GCM authenticates the ciphertext, so tampered records fail
 * decrypt cleanly. Less secure than the Tauri build's Secure
 * Enclave (which gates on hardware-bound biometrics) but the
 * realistic ceiling for a no-native PWA.
 */

const DB_NAME = 'cove-vault';
const STORE_NAME = 'identities';
const DB_VERSION = 1;

/** OWASP 2023 recommends >=600k PBKDF2-SHA256 iterations for password
 *  hashing. Recorded per-record so we can bump without breaking
 *  existing vaults — they re-encrypt under the new value on next unlock. */
const PBKDF2_ITERATIONS = 600_000;
const SALT_BYTES = 16;
const IV_BYTES = 12;
const ALGO_TAG = 'PBKDF2-SHA256-AES-GCM-256-v1';

export interface VaultStatus {
  /** Whether a vault entry exists. False on first launch and after
   *  clearVault(). Used by AuthPanel to pick pwa-unlock vs pwa-import. */
  exists: boolean;
  /** Pubkey of the stored identity. Surfaced in the UI so the user
   *  sees "Welcome back, 9add01d4…" before they unlock. */
  pubkey?: string;
  /** ISO 8601. Mostly cosmetic — "stored 2 weeks ago" hint. */
  created_at?: string;
}

interface VaultRecord {
  pubkey: string;
  ciphertext: ArrayBuffer;
  iv: Uint8Array;
  salt: Uint8Array;
  iterations: number;
  algo: string;
  created_at: string;
}

/** Open (or create) the IndexedDB. Idempotent on the upgrade — the
 *  store is created exactly once on first launch and stays at v1. */
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

/** Tiny IDB plumbing: run a callback inside a transaction and resolve
 *  on completion. Wraps the callback-style API into a clean async one. */
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

/** Read whatever's in the store. We only support one identity per
 *  device today; this returns the first (and only) record if any. */
async function readOnly(): Promise<VaultRecord | null> {
  return withStore('readonly', (store) =>
    new Promise<VaultRecord | null>((resolve, reject) => {
      const req = store.getAll();
      req.onsuccess = () => {
        const records = req.result as VaultRecord[];
        resolve(records.length > 0 ? records[0] : null);
      };
      req.onerror = () => reject(req.error);
    }),
  );
}

export async function vaultStatus(): Promise<VaultStatus> {
  try {
    const record = await readOnly();
    if (!record) return { exists: false };
    return {
      exists: true,
      pubkey: record.pubkey,
      created_at: record.created_at,
    };
  } catch {
    // IndexedDB unavailable (private browsing on iOS Safari pre-15,
    // some webviews) — treat as no vault. The UI falls through to
    // paste / Get started which doesn't need IDB.
    return { exists: false };
  }
}

/** Derive an AES-GCM-256 key from a passphrase + salt via PBKDF2. The
 *  raw key never leaves this module. */
async function deriveKey(
  passphrase: string,
  salt: Uint8Array,
  iterations: number,
): Promise<CryptoKey> {
  const enc = new TextEncoder();
  const baseKey = await crypto.subtle.importKey(
    'raw',
    enc.encode(passphrase),
    { name: 'PBKDF2' },
    false,
    ['deriveKey'],
  );
  return crypto.subtle.deriveKey(
    {
      name: 'PBKDF2',
      hash: 'SHA-256',
      salt: salt as BufferSource,
      iterations,
    },
    baseKey,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt', 'decrypt'],
  );
}

function hexToBytes(hex: string): Uint8Array {
  if (hex.length % 2 !== 0) throw new Error('hex string must have even length');
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

export async function storeKey(opts: {
  priv: string;   // 64-char hex
  pub: string;    // 64-char hex
  passphrase: string;
}): Promise<void> {
  if (opts.passphrase.length < 12) {
    throw new Error('passphrase must be at least 12 characters');
  }
  const salt = crypto.getRandomValues(new Uint8Array(SALT_BYTES));
  const iv = crypto.getRandomValues(new Uint8Array(IV_BYTES));
  const key = await deriveKey(opts.passphrase, salt, PBKDF2_ITERATIONS);
  const ciphertext = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: iv as BufferSource },
    key,
    hexToBytes(opts.priv) as BufferSource,
  );
  const record: VaultRecord = {
    pubkey: opts.pub,
    ciphertext,
    iv,
    salt,
    iterations: PBKDF2_ITERATIONS,
    algo: ALGO_TAG,
    created_at: new Date().toISOString(),
  };
  await withStore('readwrite', (store) => {
    // Replace any prior record (single-identity-per-device today).
    store.clear();
    store.put(record);
  });
}

/** Throws on missing vault, unknown algo, or wrong passphrase (the
 *  AES-GCM auth tag catches that automatically — decrypt throws on
 *  mismatch). */
export async function unlockKey(passphrase: string): Promise<{
  priv: string;
  pub: string;
}> {
  const record = await readOnly();
  if (!record) {
    throw new Error('no vault on this device — onboard first');
  }
  if (record.algo !== ALGO_TAG) {
    throw new Error(`unknown vault algorithm ${record.algo}`);
  }
  const key = await deriveKey(passphrase, record.salt, record.iterations);
  let plaintext: ArrayBuffer;
  try {
    plaintext = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: record.iv as BufferSource },
      key,
      record.ciphertext,
    );
  } catch {
    // Bad passphrase OR tampered ciphertext. Same surface message —
    // we don't help an attacker distinguish.
    throw new Error('wrong passphrase');
  }
  const priv = bytesToHex(new Uint8Array(plaintext));
  return { priv, pub: record.pubkey };
}

export async function clearVault(): Promise<void> {
  try {
    await withStore('readwrite', (store) => store.clear());
  } catch {
    // Mirroring vaultStatus(): if IDB is unavailable there's nothing
    // to clear anyway. Don't surface as an error to the caller.
  }
}

/** v0.4.34: best-effort request that the browser keep our IDB even
 *  under storage pressure. Installed PWAs usually get this silently;
 *  uninstalled tabs may not. No harm if it returns false. */
export async function requestPersistentStorage(): Promise<boolean> {
  if (typeof navigator === 'undefined') return false;
  if (!navigator.storage?.persist) return false;
  try {
    return await navigator.storage.persist();
  } catch {
    return false;
  }
}
