/**
 * Root-vault round-trip tests. v0.4.80.
 *
 * fake-indexeddb/auto shim + native crypto.subtle in Node 20+.
 */
import 'fake-indexeddb/auto';
import { afterEach, describe, expect, test } from 'vitest';
import { ed25519 } from '@noble/curves/ed25519';
import { bytesToHex, hexToBytes } from '@noble/hashes/utils';

import {
  clearRootVault, importRootToVault, rootVaultStatus, unlockRootVault,
} from './root-vault';

const org1 = 'a'.repeat(64);
const org2 = 'b'.repeat(64);

function fixtureKeypair(seed = 1): { priv: string; pub: string } {
  const priv = new Uint8Array(32).map((_, i) => (i * 3 + seed) & 0xff);
  const pub = ed25519.getPublicKey(priv);
  return { priv: bytesToHex(priv), pub: bytesToHex(pub) };
}

afterEach(async () => {
  await clearRootVault(org1);
  await clearRootVault(org2);
});

describe('root-vault', () => {
  test('status is present:false before any import', async () => {
    expect((await rootVaultStatus(org1)).present).toBe(false);
  });

  test('import + unlock round-trips the priv exactly', async () => {
    const { priv, pub } = fixtureKeypair(7);
    await importRootToVault({
      privateKey: priv, publicKey: pub,
      passphrase: 'correct horse battery',
      org: org1,
    });
    const st = await rootVaultStatus(org1);
    expect(st.present).toBe(true);
    expect(st.public_key).toBe(pub);

    const unlocked = await unlockRootVault({
      passphrase: 'correct horse battery',
      org: org1,
    });
    expect(unlocked).toBe(priv);

    // Round-trip signature: sign with unlocked priv, verify with pub.
    const msg = new TextEncoder().encode('attest something');
    const sig = ed25519.sign(msg, hexToBytes(unlocked));
    expect(ed25519.verify(sig, msg, hexToBytes(pub))).toBe(true);
  });

  test('wrong passphrase throws', async () => {
    const { priv, pub } = fixtureKeypair(3);
    await importRootToVault({
      privateKey: priv, publicKey: pub,
      passphrase: 'right passphrase now',
      org: org1,
    });
    await expect(unlockRootVault({
      passphrase: 'wrong passphrase now',
      org: org1,
    })).rejects.toThrow(/wrong passphrase/i);
  });

  test('separate orgs get separate records', async () => {
    const a = fixtureKeypair(11);
    const b = fixtureKeypair(23);
    await importRootToVault({
      privateKey: a.priv, publicKey: a.pub,
      passphrase: 'first passphrase here',
      org: org1,
    });
    await importRootToVault({
      privateKey: b.priv, publicKey: b.pub,
      passphrase: 'second passphrase now',
      org: org2,
    });
    // Each org's own passphrase unlocks the right priv.
    expect(await unlockRootVault({ passphrase: 'first passphrase here', org: org1 }))
      .toBe(a.priv);
    expect(await unlockRootVault({ passphrase: 'second passphrase now', org: org2 }))
      .toBe(b.priv);
    // Cross-passphrase fails.
    await expect(unlockRootVault({ passphrase: 'first passphrase here', org: org2 }))
      .rejects.toThrow();
  });

  test('re-import overwrites the prior record for the same org', async () => {
    const a = fixtureKeypair(5);
    const b = fixtureKeypair(9);
    await importRootToVault({
      privateKey: a.priv, publicKey: a.pub,
      passphrase: 'passphrase one now',
      org: org1,
    });
    await importRootToVault({
      privateKey: b.priv, publicKey: b.pub,
      passphrase: 'passphrase two now',
      org: org1,
    });
    // The old passphrase no longer works.
    await expect(unlockRootVault({ passphrase: 'passphrase one now', org: org1 }))
      .rejects.toThrow();
    // The new passphrase decrypts the new priv.
    expect(await unlockRootVault({ passphrase: 'passphrase two now', org: org1 }))
      .toBe(b.priv);
  });

  test('short passphrase rejected at import time', async () => {
    const { priv, pub } = fixtureKeypair(1);
    await expect(importRootToVault({
      privateKey: priv, publicKey: pub,
      passphrase: 'short',
      org: org1,
    })).rejects.toThrow(/at least 12/i);
  });

  test('clearRootVault removes the record', async () => {
    const { priv, pub } = fixtureKeypair(1);
    await importRootToVault({
      privateKey: priv, publicKey: pub,
      passphrase: 'passphrase here now',
      org: org1,
    });
    await clearRootVault(org1);
    expect((await rootVaultStatus(org1)).present).toBe(false);
  });
});
