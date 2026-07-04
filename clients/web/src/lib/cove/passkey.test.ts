/**
 * Passkey → Ed25519 tests (v0.4.74).
 *
 * Mocks navigator.credentials.{create,get} to return canned PRF output.
 * fake-indexeddb/auto shim reused from vault.test.ts. crypto.subtle is
 * native in Node 20+.
 */
import 'fake-indexeddb/auto';
import { afterEach, beforeAll, describe, expect, test, vi } from 'vitest';
import { ed25519 } from '@noble/curves/ed25519';
import { hexToBytes } from '@noble/hashes/utils';

import {
  clearPasskeyStorage, passkeyStatus, passkeySupported,
  registerPasskey, unlockWithPasskey,
} from './passkey';

// ---- Fake WebAuthn scaffolding -----------------------------------------

/** Fixed 32-byte PRF output — the mock returns this on every ceremony,
 *  which is what a real synced Passkey would do for the same salt. */
const FAKE_PRF_OUTPUT = new Uint8Array(32).map((_, i) => (i * 7 + 3) & 0xff).buffer;
const FAKE_CREDENTIAL_ID = new Uint8Array(16).map((_, i) => (i * 13 + 5) & 0xff);

/** A different Passkey's PRF output — used to simulate "user picked the
 *  wrong Passkey at unlock time." */
const ALT_PRF_OUTPUT = new Uint8Array(32).map((_, i) => (i * 11 + 1) & 0xff).buffer;

/** Mock PublicKeyCredential returned by create() and get(). */
function fakeCredential(prf: ArrayBuffer): any {
  return {
    rawId: FAKE_CREDENTIAL_ID.buffer,
    getClientExtensionResults: () => ({
      prf: { enabled: true, results: { first: prf } },
    }),
  };
}

/** Install a working navigator.credentials + PublicKeyCredential on
 *  globalThis. Uses the `prfSource` closure so a single test can set
 *  the mock to return whatever it wants. */
function installWebAuthn(prfSource: () => ArrayBuffer | null): void {
  (globalThis as any).navigator = {
    ...(globalThis as any).navigator,
    credentials: {
      create: vi.fn(async () => {
        const prf = prfSource();
        return prf === null ? null : fakeCredential(prf);
      }),
      get: vi.fn(async () => {
        const prf = prfSource();
        return prf === null ? null : fakeCredential(prf);
      }),
    },
  };
  (globalThis as any).window = {
    ...(globalThis as any).window,
    location: { hostname: 'localhost' },
    PublicKeyCredential: {
      isUserVerifyingPlatformAuthenticatorAvailable: async () => true,
      getClientCapabilities: async () => ({ extensionPrf: true }),
    },
  };
}

/** Remove the shims so a following test starts clean. */
function uninstallWebAuthn(): void {
  delete (globalThis as any).window;
  delete (globalThis as any).navigator;
}

beforeAll(() => {
  installWebAuthn(() => FAKE_PRF_OUTPUT);
});

afterEach(async () => {
  await clearPasskeyStorage();
  // Reset to the happy PRF for the next test.
  installWebAuthn(() => FAKE_PRF_OUTPUT);
});

// ---- Tests -------------------------------------------------------------

describe('passkey', () => {
  test('passkeySupported returns true when PRF is reported by getClientCapabilities', async () => {
    expect(await passkeySupported()).toBe(true);
  });

  test('passkeySupported returns false when getClientCapabilities reports no PRF', async () => {
    (globalThis as any).window.PublicKeyCredential.getClientCapabilities
      = async () => ({ extensionPrf: false });
    expect(await passkeySupported()).toBe(false);
  });

  test('passkeyStatus is exists:false before any register', async () => {
    const s = await passkeyStatus();
    expect(s.exists).toBe(false);
    expect(s.pubkey).toBeUndefined();
  });

  test('registerPasskey writes IDB + returns an Ed25519 keypair that round-trips through sign/verify', async () => {
    const { priv, pub, credentialId } = await registerPasskey();
    // Sanity: 64-char hex.
    expect(priv).toMatch(/^[0-9a-f]{64}$/);
    expect(pub).toMatch(/^[0-9a-f]{64}$/);
    expect(credentialId.length).toBeGreaterThan(0);
    // Deriving pub from priv matches the returned pub.
    const derivedPub = ed25519.getPublicKey(hexToBytes(priv));
    expect(Buffer.from(derivedPub).toString('hex')).toBe(pub);
    // A round-trip signature verifies.
    const msg = new TextEncoder().encode('hello passkey');
    const sig = ed25519.sign(msg, hexToBytes(priv));
    expect(ed25519.verify(sig, msg, hexToBytes(pub))).toBe(true);
    // IDB reflects the record.
    const s = await passkeyStatus();
    expect(s.exists).toBe(true);
    expect(s.pubkey).toBe(pub);
    expect(s.credentialId).toBe(credentialId);
  });

  test('unlockWithPasskey returns the SAME priv+pub as register (deterministic across ceremonies)', async () => {
    const r = await registerPasskey();
    const u = await unlockWithPasskey();
    expect(u.priv).toBe(r.priv);
    expect(u.pub).toBe(r.pub);
  });

  test('unlockWithPasskey throws when the derived pub does not match the persisted pub', async () => {
    await registerPasskey();
    // Simulate the user picking a DIFFERENT Passkey at unlock time
    // (different PRF output → different derived pub).
    installWebAuthn(() => ALT_PRF_OUTPUT);
    await expect(unlockWithPasskey()).rejects.toThrow(/different identity/i);
  });

  test('clearPasskeyStorage wipes IDB but does not touch other DBs', async () => {
    await registerPasskey();
    expect((await passkeyStatus()).exists).toBe(true);
    await clearPasskeyStorage();
    expect((await passkeyStatus()).exists).toBe(false);
  });
});
