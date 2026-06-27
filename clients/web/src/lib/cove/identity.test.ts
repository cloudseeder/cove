/**
 * issueAttestation / issueDirectory wire-contract tests. v0.4.0 admin.
 *
 * Produces signed records via the TS admin path; verifies them through
 * the SAME verify_attestation / verify_directory_manifest the member
 * client uses on read. Round-trip is the contract: anything the admin
 * UI builds must be accepted by every other client.
 */
import { describe, expect, test } from 'vitest';
import { ed25519 } from '@noble/curves/ed25519';
import { bytesToHex } from '@noble/hashes/utils';

import { issueAttestation, issueDirectory, type RootSigner } from './identity';
import { sign } from './crypto';
import { verifyAttestation, verifyDirectoryManifest, hashManifest } from './verify';

class InProcessRootSigner implements RootSigner {
  constructor(private priv: string, private pub: string) {}
  async sign(message: Uint8Array): Promise<string> {
    return sign(this.priv, message);
  }
  async pubkey(): Promise<string> {
    return this.pub;
  }
}

function freshSigner(): InProcessRootSigner {
  const priv = ed25519.utils.randomPrivateKey();
  const pub = ed25519.getPublicKey(priv);
  return new InProcessRootSigner(bytesToHex(priv), bytesToHex(pub));
}

describe('issueAttestation', () => {
  test('produces a signed attestation that verifyAttestation accepts', async () => {
    const signer = freshSigner();
    const att = await issueAttestation(signer, {
      memberPubkey: 'a'.repeat(64),
      displayName: 'Jane Doe',
      affiliation: 'Lot 27',
      role: 'member',
      title: 'Treasurer',
    });
    expect(att.member_pubkey).toBe('a'.repeat(64));
    expect(att.display_name).toBe('Jane Doe');
    expect(att.issuer).toBe(await signer.pubkey());
    expect(att.sig).toMatch(/^[0-9a-f]{128}$/);
    expect(verifyAttestation(att)).toBe(true);
  });

  test('mutating any field after signing breaks verification', async () => {
    const signer = freshSigner();
    const att = await issueAttestation(signer, {
      memberPubkey: 'b'.repeat(64), displayName: 'Bob',
      affiliation: 'Lot 12', role: 'member',
    });
    const tampered = { ...att, display_name: 'Mallory' };
    expect(verifyAttestation(tampered)).toBe(false);
  });

  test('handles optional fields (title, enc_pubkey, expires_at) cleanly', async () => {
    const signer = freshSigner();
    const att = await issueAttestation(signer, {
      memberPubkey: 'c'.repeat(64), displayName: 'Carol',
      affiliation: 'Lot 99', role: 'board',
      title: 'President', encPubkey: 'd'.repeat(64),
      expiresAt: '2027-01-01T00:00:00Z',
    });
    expect(att.title).toBe('President');
    expect(att.enc_pubkey).toBe('d'.repeat(64));
    expect(att.expires_at).toBe('2027-01-01T00:00:00Z');
    expect(verifyAttestation(att)).toBe(true);
  });
});

describe('issueDirectory', () => {
  test('produces a signed manifest that verifyDirectoryManifest accepts', async () => {
    const signer = freshSigner();
    const att = await issueAttestation(signer, {
      memberPubkey: 'a'.repeat(64), displayName: 'Jane',
      affiliation: 'Lot 27', role: 'member',
    });
    const manifest = await issueDirectory(signer, {
      attestations: [att],
      revocations: [],
    });
    expect(manifest.org).toBe(await signer.pubkey());
    expect(manifest.attestations).toHaveLength(1);
    expect(verifyDirectoryManifest(manifest)).toBe(true);
  });

  test('chains via prev_manifest_hash so the next update has a backref', async () => {
    const signer = freshSigner();
    const first = await issueDirectory(signer, {
      attestations: [], revocations: [],
    });
    const headHash = hashManifest(first);
    const second = await issueDirectory(signer, {
      attestations: [], revocations: [],
      prevManifestHash: headHash,
    });
    expect(second.prev_manifest_hash).toBe(headHash);
    expect(verifyDirectoryManifest(second)).toBe(true);
  });

  test('rejects an attestation forged by a non-root key inside an otherwise valid manifest', async () => {
    // The outer manifest is signed by the real root; an inner attestation
    // has a tampered sig. verifyDirectoryManifest must catch this — the
    // outer signature is necessary but not sufficient.
    const signer = freshSigner();
    const realAtt = await issueAttestation(signer, {
      memberPubkey: 'a'.repeat(64), displayName: 'Jane',
      affiliation: 'Lot 27', role: 'member',
    });
    const tamperedAtt = { ...realAtt, role: 'board' };
    const manifest = await issueDirectory(signer, {
      attestations: [tamperedAtt], revocations: [],
    });
    expect(verifyDirectoryManifest(manifest)).toBe(false);
  });
});
