/**
 * Cove identity-vault (hub-stored) tests. v0.4.76.
 *
 * fake-indexeddb/auto shim isn't needed (this module doesn't touch IDB)
 * but importing keeps the environment shape consistent with vault.test.ts
 * and passkey.test.ts. crypto.subtle is native in Node 20+.
 */
import 'fake-indexeddb/auto';
import { afterEach, beforeAll, describe, expect, test, vi } from 'vitest';
import { ed25519 } from '@noble/curves/ed25519';
import { bytesToHex, hexToBytes } from '@noble/hashes/utils';

import {
  GENESIS_PREV, PBKDF2_ITERATIONS, VAULT_HKDF_INFO, VAULT_PRF_SALT_TAG,
  addPasskeySlot, addPassphraseSlot, canonicalize, createVault,
  hashVault, removeSlot, signVault,
  unlockWithPasskey, unlockWithPassphrase,
  type PasskeySlot, type PassphraseSlot, type VaultRecord,
} from './vault-blob';

// ---- Fake WebAuthn scaffolding (shape matches passkey.test.ts) --------

const FIRST_PRF = new Uint8Array(32).map((_, i) => (i * 7 + 3) & 0xff).buffer;
const SECOND_PRF = new Uint8Array(32).map((_, i) => (i * 11 + 1) & 0xff).buffer;
const CRED_ID_A = new Uint8Array(16).map((_, i) => (i * 13 + 5) & 0xff);
const CRED_ID_B = new Uint8Array(16).map((_, i) => (i * 17 + 9) & 0xff);

type Ceremony = { prf: ArrayBuffer; rawId: Uint8Array };

function installWebAuthn(nextCeremony: () => Ceremony): void {
  const fakeCredential = (c: Ceremony) => ({
    rawId: c.rawId.buffer,
    getClientExtensionResults: () => ({
      prf: { enabled: true, results: { first: c.prf } },
    }),
  });
  (globalThis as any).navigator = {
    ...(globalThis as any).navigator,
    credentials: {
      create: vi.fn(async () => fakeCredential(nextCeremony())),
      get: vi.fn(async () => fakeCredential(nextCeremony())),
    },
  };
  (globalThis as any).window = {
    ...(globalThis as any).window,
    location: { hostname: 'localhost' },
    PublicKeyCredential: {
      isUserVerifyingPlatformAuthenticatorAvailable: async () => true,
    },
  };
}

function uninstallWebAuthn(): void {
  delete (globalThis as any).window;
  delete (globalThis as any).navigator;
}

beforeAll(() => {
  installWebAuthn(() => ({ prf: FIRST_PRF, rawId: CRED_ID_A }));
});

afterEach(() => {
  installWebAuthn(() => ({ prf: FIRST_PRF, rawId: CRED_ID_A }));
});

// ---- Salt-separation invariant (compile-adjacent) ---------------------

test('vault-KEK PRF salt tag differs from identity-seed salt tag', async () => {
  // A leaked vault KEK must not be walkable back to the identity priv.
  // Enforced by using different salt strings in the two PRF ceremonies.
  const { PRF_SALT_TAG: identityTag } = { PRF_SALT_TAG: 'cove-passkey-prf-v1' };
  expect(VAULT_PRF_SALT_TAG).toBe('cove-vault-kek-v1');
  expect(VAULT_PRF_SALT_TAG).not.toBe(identityTag);
  expect(VAULT_HKDF_INFO).toBe('cove-vault-kek-v1');
  expect(PBKDF2_ITERATIONS).toBeGreaterThanOrEqual(600_000);
});

// ---- JCS canonicalization vs. Python rfc8785 --------------------------

test('canonicalize matches Python rfc8785 for a mixed golden vector', () => {
  // Generated via:
  //   python -c "import rfc8785; print(rfc8785.dumps({...}).decode())"
  // Any drift between this JS output and Python's will surface here first,
  // BEFORE the hub rejects a vault with a mismatched hash.
  const input = {
    method_slots: [
      { id: 'ffffffffffffffff', label: 'Backup', type: 'passphrase' },
      { id: 'eeeeeeeeeeeeeeee', label: 'iCloud', type: 'passkey' },
    ],
    nested: { z: 1, a: [3, 2, 1] },
    pubkey: 'abcd'.repeat(16),
    unicode: 'café — δθ',
    version: 1,
  };
  const expected = '{"method_slots":[{"id":"ffffffffffffffff","label":"Backup","type":"passphrase"},{"id":"eeeeeeeeeeeeeeee","label":"iCloud","type":"passkey"}],"nested":{"a":[3,2,1],"z":1},"pubkey":"abcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcd","unicode":"café — δθ","version":1}';
  expect(new TextDecoder().decode(canonicalize(input))).toBe(expected);
});

test('canonicalize is stable under key-reordering (hash invariant)', async () => {
  const a = { pubkey: 'x', version: 1, updated_at: '2026-07-05T00:00:00+00:00' };
  const b = { updated_at: '2026-07-05T00:00:00+00:00', version: 1, pubkey: 'x' };
  expect(new TextDecoder().decode(canonicalize(a)))
    .toBe(new TextDecoder().decode(canonicalize(b)));
});

// ---- Passphrase round-trip -------------------------------------------

test('createVault + unlockWithPassphrase round-trips priv/pub', async () => {
  const priv = bytesToHex(new Uint8Array(32).map((_, i) => (i * 3 + 1) & 0xff));
  const pub = bytesToHex(ed25519.getPublicKey(hexToBytes(priv)));
  const v = await createVault({
    priv, pub,
    firstUnlock: { kind: 'passphrase', passphrase: 'correct horse battery' },
  });
  expect(v.pubkey).toBe(pub);
  expect(v.prev_vault_hash).toBe(GENESIS_PREV);
  expect(v.method_slots).toHaveLength(1);
  expect(v.method_slots[0].type).toBe('passphrase');

  const unlocked = await unlockWithPassphrase({
    vault: v, passphrase: 'correct horse battery',
  });
  expect(unlocked.priv).toBe(priv);
  expect(unlocked.pub).toBe(pub);
  expect(unlocked.cek).toHaveLength(32);
});

test('unlockWithPassphrase throws on wrong passphrase', async () => {
  const priv = bytesToHex(new Uint8Array(32).map((_, i) => i & 0xff));
  const pub = bytesToHex(ed25519.getPublicKey(hexToBytes(priv)));
  const v = await createVault({
    priv, pub,
    firstUnlock: { kind: 'passphrase', passphrase: 'right passphrase here' },
  });
  await expect(unlockWithPassphrase({ vault: v, passphrase: 'nope wrong one' }))
    .rejects.toThrow(/none.*accepted/i);
});

// ---- Sig verify --------------------------------------------------------

test('signVault emits a sig that verifies against the owner pubkey', async () => {
  const priv = bytesToHex(new Uint8Array(32).map((_, i) => (i * 5 + 7) & 0xff));
  const pub = bytesToHex(ed25519.getPublicKey(hexToBytes(priv)));
  const v = await createVault({
    priv, pub,
    firstUnlock: { kind: 'passphrase', passphrase: 'twelve chars ok' },
  });
  const { sig, ...unsigned } = v;
  const ok = ed25519.verify(
    hexToBytes(sig),
    canonicalize(unsigned),
    hexToBytes(pub),
  );
  expect(ok).toBe(true);
});

// ---- Add / remove slot -------------------------------------------------

test('addPassphraseSlot preserves content ciphertext (CEK unchanged)', async () => {
  const priv = bytesToHex(new Uint8Array(32).map((_, i) => (i * 2 + 3) & 0xff));
  const pub = bytesToHex(ed25519.getPublicKey(hexToBytes(priv)));
  const v1 = await createVault({
    priv, pub,
    firstUnlock: { kind: 'passphrase', passphrase: 'first pass here now' },
  });
  const { cek } = await unlockWithPassphrase({
    vault: v1, passphrase: 'first pass here now',
  });
  const v2 = await addPassphraseSlot({
    vault: v1, cek, ownerPriv: priv,
    passphrase: 'second passphrase now', label: 'Alt',
  });
  // Content ciphertext + IV are untouched — only method_slots + prev_hash + sig change.
  expect(v2.content_ciphertext).toBe(v1.content_ciphertext);
  expect(v2.content_iv).toBe(v1.content_iv);
  expect(v2.method_slots).toHaveLength(2);
  expect(v2.prev_vault_hash).toBe(await hashVault(v1));

  // Both passphrases unlock the same priv.
  const u1 = await unlockWithPassphrase({ vault: v2, passphrase: 'first pass here now' });
  const u2 = await unlockWithPassphrase({ vault: v2, passphrase: 'second passphrase now' });
  expect(u1.priv).toBe(priv);
  expect(u2.priv).toBe(priv);
});

test('removeSlot refuses to drop the last slot', async () => {
  const priv = bytesToHex(new Uint8Array(32).map((_, i) => (i + 1) & 0xff));
  const pub = bytesToHex(ed25519.getPublicKey(hexToBytes(priv)));
  const v = await createVault({
    priv, pub,
    firstUnlock: { kind: 'passphrase', passphrase: 'just one here now' },
  });
  await expect(removeSlot({
    vault: v, slotId: v.method_slots[0].id, ownerPriv: priv,
  })).rejects.toThrow(/lock yourself out/i);
});

test('removeSlot succeeds when other slots remain, still sig-valid', async () => {
  const priv = bytesToHex(new Uint8Array(32).map((_, i) => (i * 9 + 4) & 0xff));
  const pub = bytesToHex(ed25519.getPublicKey(hexToBytes(priv)));
  const v1 = await createVault({
    priv, pub,
    firstUnlock: { kind: 'passphrase', passphrase: 'first passphrase here' },
  });
  const { cek } = await unlockWithPassphrase({
    vault: v1, passphrase: 'first passphrase here',
  });
  const v2 = await addPassphraseSlot({
    vault: v1, cek, ownerPriv: priv,
    passphrase: 'second passphrase go', label: 'Alt',
  });
  const v3 = await removeSlot({
    vault: v2, slotId: v1.method_slots[0].id, ownerPriv: priv,
  });
  expect(v3.method_slots).toHaveLength(1);
  const { sig, ...unsigned } = v3;
  expect(ed25519.verify(
    hexToBytes(sig), canonicalize(unsigned), hexToBytes(pub),
  )).toBe(true);
});

// ---- Passkey round-trip -----------------------------------------------

test('addPasskeySlot + unlockWithPasskey round-trips through PRF', async () => {
  const priv = bytesToHex(new Uint8Array(32).map((_, i) => (i * 6 + 2) & 0xff));
  const pub = bytesToHex(ed25519.getPublicKey(hexToBytes(priv)));

  // First slot: passphrase (so we have a CEK to add the Passkey with).
  const v1 = await createVault({
    priv, pub,
    firstUnlock: { kind: 'passphrase', passphrase: 'unlock via passphrase' },
  });
  const { cek } = await unlockWithPassphrase({
    vault: v1, passphrase: 'unlock via passphrase',
  });

  // Add the Passkey slot. The fake WebAuthn returns FIRST_PRF + CRED_ID_A.
  installWebAuthn(() => ({ prf: FIRST_PRF, rawId: CRED_ID_A }));
  const v2 = await addPasskeySlot({
    vault: v1, cek, ownerPriv: priv, label: 'iCloud',
  });
  expect(v2.method_slots).toHaveLength(2);
  expect(v2.method_slots[1].type).toBe('passkey');
  const pkSlot = v2.method_slots[1] as PasskeySlot;
  expect(pkSlot.prf_salt_tag).toBe(VAULT_PRF_SALT_TAG);
  expect(pkSlot.hkdf_info).toBe(VAULT_HKDF_INFO);

  // Unlock via the same Passkey ceremony.
  const unlocked = await unlockWithPasskey({ vault: v2 });
  expect(unlocked.priv).toBe(priv);
  expect(unlocked.pub).toBe(pub);
});

test('unlockWithPasskey throws when a non-matching credential ID is presented', async () => {
  const priv = bytesToHex(new Uint8Array(32).map((_, i) => (i * 8 + 5) & 0xff));
  const pub = bytesToHex(ed25519.getPublicKey(hexToBytes(priv)));
  const v1 = await createVault({
    priv, pub,
    firstUnlock: { kind: 'passphrase', passphrase: 'passphrase first here' },
  });
  const { cek } = await unlockWithPassphrase({
    vault: v1, passphrase: 'passphrase first here',
  });
  installWebAuthn(() => ({ prf: FIRST_PRF, rawId: CRED_ID_A }));
  const v2 = await addPasskeySlot({
    vault: v1, cek, ownerPriv: priv, label: 'iCloud',
  });
  // The user selects a DIFFERENT Passkey (CRED_ID_B); unlock ceremony
  // reports the wrong credentialId → the slot list has no match.
  installWebAuthn(() => ({ prf: SECOND_PRF, rawId: CRED_ID_B }));
  await expect(unlockWithPasskey({ vault: v2 }))
    .rejects.toThrow(/credential ID did not match|no Passkey slots/i);
});
