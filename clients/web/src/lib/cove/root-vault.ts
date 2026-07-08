/**
 * PWA-safe root key custody. v0.4.80.
 *
 * The Tauri desktop app stores `root.priv` in the OS keychain via the
 * `keyring` crate — the private material never crosses back into the JS
 * webview after import. On the PWA there's no equivalent secure store,
 * so root custody was previously locked out entirely.
 *
 * This module fills the gap using the same discipline as the v0.4.34
 * identity vault: PBKDF2-derived KEK, AES-GCM ciphertext in IndexedDB,
 * priv decrypted in-JS only during an admin operation and wiped from
 * heap after. Different threat profile than Tauri keychain — a
 * compromised browser extension can potentially observe the decrypted
 * priv during the signing window — but strictly better than "not
 * possible on this device."
 *
 * Storage layout:
 *   IndexedDB database: 'cove-root-vault'
 *   Store:              'roots'
 *   keyPath:            'org' (org pubkey, hex)
 *
 * One record per (device, org). A keymaster on N hubs holds N records,
 * mirroring the Tauri path's per-hub root-keychain slots. If the
 * keymaster wants root on M devices, they re-import on each — this
 * module does NOT sync across devices (deliberate: root should not
 * propagate automatically).
 */

import { pbkdf2Async } from '@noble/hashes/pbkdf2';
import { sha256 } from '@noble/hashes/sha256';

const DB_NAME = 'cove-root-vault';
const STORE_NAME = 'roots';
const DB_VERSION = 1;

/** OWASP 2023 minimum for PBKDF2-SHA256. Matches vault.ts. */
const PBKDF2_ITERATIONS = 600_000;
const SALT_BYTES = 16;
const IV_BYTES = 12;
const ALGO_TAG = 'PBKDF2-SHA256-AES-GCM-256-v1';

export interface RootVaultStatus {
  present: boolean;
  public_key?: string;
  created_at?: string;
}

interface RootVaultRecord {
  org: string;                       // 64-hex org pubkey — primary key
  public_key: string;                // 64-hex root pubkey (redundant with org in single-root setups, but explicit)
  algo: string;                      // 'PBKDF2-SHA256-AES-GCM-256-v1'
  kdf_salt: ArrayBuffer;
  kdf_iterations: number;
  iv: ArrayBuffer;
  ciphertext: ArrayBuffer;           // AES-GCM(KEK, IV, hex_bytes_of_priv)
  created_at: string;
}

// ---- IndexedDB primitives ---------------------------------------------

async function openDB(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: 'org' });
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

async function readOne(org: string): Promise<RootVaultRecord | null> {
  return withStore('readonly', (store) =>
    new Promise<RootVaultRecord | null>((resolve, reject) => {
      const req = store.get(org);
      req.onsuccess = () => resolve((req.result as RootVaultRecord | undefined) ?? null);
      req.onerror = () => reject(req.error);
    }),
  );
}

// ---- KEK derivation ---------------------------------------------------

async function passphraseKek(
  passphrase: string,
  salt: Uint8Array,
  iterations: number,
): Promise<CryptoKey> {
  // @noble/hashes/pbkdf2 is used elsewhere in the client for HKDF; for
  // AES-GCM key derivation we use Web Crypto's built-in PBKDF2 →
  // deriveKey because it produces a non-extractable CryptoKey object
  // directly, which is exactly what AES-GCM wants.
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
    false,
    ['encrypt', 'decrypt'],
  );
  // pbkdf2Async is imported but unused in this path — kept as an escape
  // hatch if Web Crypto's PBKDF2 turns out to be unavailable on some
  // browser we care about.
  void pbkdf2Async; void sha256;
}

function hexToBytes(hex: string): Uint8Array {
  if (hex.length % 2 !== 0) throw new Error('hex string must have even length');
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

// ---- Public API -------------------------------------------------------

/** Read the current status for an org's root vault — has one been
 *  imported, and what's the pubkey. Does NOT decrypt. */
export async function rootVaultStatus(org: string): Promise<RootVaultStatus> {
  if (!org) return { present: false };
  try {
    const record = await readOne(org);
    if (!record) return { present: false };
    return {
      present: true,
      public_key: record.public_key,
      created_at: record.created_at,
    };
  } catch {
    return { present: false };
  }
}

/** Import a root keypair for the given org, encrypted under a passphrase.
 *  Overwrites any prior record for the same org (matches the Tauri
 *  path's single-slot-per-org behavior). */
export async function importRootToVault(opts: {
  privateKey: string;                // 64-hex Ed25519 priv
  publicKey: string;                 // 64-hex Ed25519 pub
  passphrase: string;
  org: string;                       // 64-hex org pubkey
}): Promise<void> {
  if (opts.passphrase.length < 12) {
    throw new Error('passphrase must be at least 12 characters');
  }
  if (opts.privateKey.length !== 64 || !/^[0-9a-f]{64}$/.test(opts.privateKey)) {
    throw new Error('private key must be 64 hex chars');
  }
  if (opts.publicKey.length !== 64 || !/^[0-9a-f]{64}$/.test(opts.publicKey)) {
    throw new Error('public key must be 64 hex chars');
  }
  const salt = crypto.getRandomValues(new Uint8Array(SALT_BYTES));
  const iv = crypto.getRandomValues(new Uint8Array(IV_BYTES));
  const kek = await passphraseKek(opts.passphrase, salt, PBKDF2_ITERATIONS);
  const ciphertext = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: iv as BufferSource },
    kek,
    hexToBytes(opts.privateKey) as BufferSource,
  );
  const record: RootVaultRecord = {
    org: opts.org,
    public_key: opts.publicKey,
    algo: ALGO_TAG,
    kdf_salt: salt.buffer,
    kdf_iterations: PBKDF2_ITERATIONS,
    iv: iv.buffer,
    ciphertext,
    created_at: new Date().toISOString(),
  };
  await withStore('readwrite', (store) => {
    store.delete(opts.org);
    store.put(record);
  });
}

/** Decrypt the root priv for `org` using `passphrase`. Returns the
 *  64-hex priv. Throws on wrong passphrase (AES-GCM auth tag failure)
 *  or missing record. Caller is responsible for wiping the returned
 *  string when done. */
export async function unlockRootVault(opts: {
  passphrase: string;
  org: string;
}): Promise<string> {
  const record = await readOne(opts.org);
  if (!record) {
    throw new Error(`no root vault for org ${opts.org.slice(0, 12)}…`);
  }
  if (record.algo !== ALGO_TAG) {
    throw new Error(`unknown root vault algo ${record.algo}`);
  }
  const kek = await passphraseKek(
    opts.passphrase,
    new Uint8Array(record.kdf_salt),
    record.kdf_iterations,
  );
  let priv: ArrayBuffer;
  try {
    priv = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: new Uint8Array(record.iv) as BufferSource },
      kek,
      record.ciphertext,
    );
  } catch {
    throw new Error('wrong passphrase');
  }
  const bytes = new Uint8Array(priv);
  let hex = '';
  for (const b of bytes) hex += b.toString(16).padStart(2, '0');
  // Zero the decrypted buffer we still hold; the returned `hex` string
  // is a JS string (immutable, GC-lifecycle) and can't be zeroed here.
  bytes.fill(0);
  return hex;
}

/** Wipe the root vault record for `org` from IndexedDB. Does NOT touch
 *  any in-memory decrypted priv held by AppState — the caller is
 *  responsible for that lifecycle. */
export async function clearRootVault(org: string): Promise<void> {
  if (!org) return;
  try {
    await withStore('readwrite', (store) => store.delete(org));
  } catch {
    // IDB unavailable — nothing to clear.
  }
}
