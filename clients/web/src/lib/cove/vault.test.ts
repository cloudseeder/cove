/**
 * Vault round-trip + tamper + wrong-passphrase tests.
 *
 * Vitest's `node` environment doesn't ship indexedDB; fake-indexeddb's
 * /auto module installs an in-memory shim into the global scope so the
 * vault module sees a real IDB shape. crypto.subtle is native in Node
 * 20+ (the version this repo targets); no shim needed for that half.
 */
import 'fake-indexeddb/auto';
import { afterEach, describe, expect, test } from 'vitest';

import { clearVault, storeKey, unlockKey, vaultStatus } from './vault';

// Realistic 64-char hex test fixtures. Not real keys.
const PRIV = 'a'.repeat(64);
const PUB = 'b'.repeat(64);

describe('vault', () => {
  afterEach(async () => {
    await clearVault();
  });

  test('vaultStatus is exists:false before any storeKey', async () => {
    const s = await vaultStatus();
    expect(s.exists).toBe(false);
    expect(s.pubkey).toBeUndefined();
  });

  test('storeKey + unlockKey round-trips the priv exactly', async () => {
    await storeKey({ priv: PRIV, pub: PUB, passphrase: 'correct horse battery staple' });
    const { priv, pub } = await unlockKey('correct horse battery staple');
    expect(priv).toBe(PRIV);
    expect(pub).toBe(PUB);
  });

  test('vaultStatus reflects the stored identity after storeKey', async () => {
    await storeKey({ priv: PRIV, pub: PUB, passphrase: 'correct horse battery staple' });
    const s = await vaultStatus();
    expect(s.exists).toBe(true);
    expect(s.pubkey).toBe(PUB);
    expect(typeof s.created_at).toBe('string');
  });

  test('wrong passphrase throws — AES-GCM auth tag catches it', async () => {
    await storeKey({ priv: PRIV, pub: PUB, passphrase: 'correct horse battery staple' });
    await expect(unlockKey('wrong passphrase here')).rejects.toThrow(/wrong passphrase/);
  });

  test('storeKey rejects short passphrases', async () => {
    await expect(
      storeKey({ priv: PRIV, pub: PUB, passphrase: 'short' }),
    ).rejects.toThrow(/at least 12/);
  });

  test('clearVault leaves the store empty', async () => {
    await storeKey({ priv: PRIV, pub: PUB, passphrase: 'correct horse battery staple' });
    await clearVault();
    expect((await vaultStatus()).exists).toBe(false);
    await expect(unlockKey('correct horse battery staple')).rejects.toThrow(/no vault/);
  });

  test('storeKey replaces a prior record (single identity per device)', async () => {
    await storeKey({ priv: PRIV, pub: PUB, passphrase: 'correct horse battery staple' });
    const newPriv = 'c'.repeat(64);
    const newPub = 'd'.repeat(64);
    await storeKey({ priv: newPriv, pub: newPub, passphrase: 'another twelve chars min' });
    const s = await vaultStatus();
    expect(s.pubkey).toBe(newPub);
    const unlocked = await unlockKey('another twelve chars min');
    expect(unlocked.priv).toBe(newPriv);
  });
});
