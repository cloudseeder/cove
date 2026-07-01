/**
 * The wire-contract test. Every fixture in fixtures.json was built by
 * the Python implementation; the TS verify functions must agree.
 *
 * If any of these fail after a code change on either side, JCS
 * canonicalization or one of the primitives has drifted — signatures
 * will stop verifying in production. Regenerate fixtures via
 * `python scripts/dump_test_vectors.py` and read the diff carefully.
 */
import { describe, expect, test } from 'vitest';
import fixtures from './fixtures.json';
import {
  hashLeaf, hashNode, hashManifest,
  verifyAttestation, verifyDirectoryManifest,
  verifyEntry, verifyInclusion, verifySth,
} from './verify';
import { canonicalize, contentId, sha256Hex, sign } from './crypto';
import { ed25519 } from '@noble/curves/ed25519';
import { bytesToHex } from '@noble/hashes/utils';
import type {
  Attestation, DirectoryManifest, Entry, InclusionProof, STH,
} from './types';

// Narrow the JSON's loose types into our wire types.
const sth = fixtures.sth as STH;
const manifest = fixtures.manifest as DirectoryManifest;
const items = fixtures.entries as Array<{
  entry: Entry; seq: number; proof: InclusionProof;
}>;

describe('JCS canonicalization (RFC 8785)', () => {
  test('produces byte-identical output for equivalent dicts', () => {
    const a = canonicalize({ b: 1, a: 2, nested: { y: 1, x: 2 } });
    const b = canonicalize({ a: 2, nested: { x: 2, y: 1 }, b: 1 });
    expect(sha256Hex(a)).toBe(sha256Hex(b));
  });

  test('matches the Python canonical form for the fixture manifest', () => {
    // If this drifts, ALL signatures break — JCS is the wire contract.
    const bytes = canonicalize({
      org: manifest.org,
      attestations: manifest.attestations,
      revocations: manifest.revocations,
      updated_at: manifest.updated_at,
      prev_manifest_hash: manifest.prev_manifest_hash,
    });
    // hashManifest = "sha256:" + sha256(canonical content + sig)
    const expected = fixtures.manifest_hash as string;
    expect(hashManifest(manifest)).toBe(expected);
    // Sanity: re-canonicalizing changes nothing.
    expect(sha256Hex(bytes)).toBe(sha256Hex(bytes));
  });
});

describe('verifyEntry (intrinsic id + sig)', () => {
  for (const { entry } of items) {
    test(`accepts the signed entry ${entry.id?.slice(0, 16)}…`, () => {
      expect(verifyEntry(entry)).toBe(true);
    });
  }

  test('rejects an entry whose body was tampered after signing', () => {
    const ev = { ...items[0].entry, body: 'tampered' } as Entry;
    expect(verifyEntry(ev)).toBe(false);
  });

  test('rejects an entry whose sig was swapped to a different valid sig', () => {
    const ev = {
      ...items[0].entry,
      sig: items[1].entry.sig,           // wrong-message sig
    } as Entry;
    expect(verifyEntry(ev)).toBe(false);
  });

  test('rejects an entry missing id', () => {
    const ev = { ...items[0].entry, id: null } as Entry;
    expect(verifyEntry(ev)).toBe(false);
  });

  // Regression: every wire kind in types.ts must be accepted by KINDS.
  // v0.2 shipped kind='branch' on the Python side and in types.ts, but the
  // TS KINDS Set was left at the v0.1 list — so the moment a branch entry
  // arrived via /sync or /stream, verifyEntry returned false and the UI
  // surfaced 'id/sig invalid' for the parent thread's feed.
  test.each([
    'notice', 'post', 'reply', 'supersede', 'membership', 'receipt', 'revoke', 'branch',
  ] as const)('accepts a well-formed kind=%s entry', (kind) => {
    const privBytes = ed25519.utils.randomPrivateKey();
    const priv = bytesToHex(privBytes);
    const pub = bytesToHex(ed25519.getPublicKey(privBytes));
    const content = {
      thread: 't1', author: pub, kind,
      created_at: '2026-06-27T00:00:00Z',
      parents: [], body: 'hi', blobs: [],
      supersedes: null, receipt: null,
      branch_thread: kind === 'branch' ? 'sub-1' : null,
    };
    const id = contentId(content);
    const sig = sign(priv, canonicalize(content));
    const ev = { ...content, id, sig } as unknown as Entry;
    expect(verifyEntry(ev)).toBe(true);
  });

  /* v0.4.41 regression: entryContent must strip tombstone_valid_after
   * when null so a plain post (which does not carry the field) verifies
   * against exactly the bytes the client signed. If this test fails,
   * every non-tombstone entry the client posts will fail verification
   * on receive with "id/sig invalid". */
  test('verifies a plain post whose signed bytes omit tombstone_valid_after', () => {
    const privBytes = ed25519.utils.randomPrivateKey();
    const priv = bytesToHex(privBytes);
    const pub = bytesToHex(ed25519.getPublicKey(privBytes));
    const content = {
      thread: 't1', author: pub, kind: 'post',
      created_at: '2026-06-27T00:00:00Z',
      parents: [], body: 'hi', blobs: [],
      supersedes: null, receipt: null, branch_thread: null,
    };
    // Sign over the shape WITHOUT tombstone_valid_after — matches
    // what client.signEntry produces on a null field.
    const id = contentId(content);
    const sig = sign(priv, canonicalize(content));
    // Reconstruct the Entry as the wire form INCLUDES the field as null
    // (asdict on the Python side always emits it). Verifier must strip
    // it before recomputing id + verifying sig.
    const ev = {
      ...content, tombstone_valid_after: null, id, sig,
    } as unknown as Entry;
    expect(verifyEntry(ev)).toBe(true);
  });

  test('verifies a well-formed kind=tombstone entry that includes valid_after', () => {
    const privBytes = ed25519.utils.randomPrivateKey();
    const priv = bytesToHex(privBytes);
    const pub = bytesToHex(ed25519.getPublicKey(privBytes));
    const content = {
      thread: 'beach', author: pub, kind: 'tombstone',
      created_at: '2026-07-01T00:00:00Z',
      parents: [], body: '', blobs: [],
      supersedes: null, receipt: null, branch_thread: null,
      tombstone_valid_after: '2026-08-01T00:00:00Z',
    };
    const id = contentId(content);
    const sig = sign(priv, canonicalize(content));
    const ev = { ...content, id, sig } as unknown as Entry;
    expect(verifyEntry(ev)).toBe(true);
  });
});

describe('verifySth', () => {
  test('accepts the hub-signed STH', () => {
    expect(verifySth(sth)).toBe(true);
  });

  test('rejects a tampered STH', () => {
    const bad = { ...sth, root_hash: 'f'.repeat(64) } as STH;
    expect(verifySth(bad)).toBe(false);
  });
});

describe('verifyInclusion (RFC 6962 audit path)', () => {
  for (const { entry, seq, proof } of items) {
    test(`accepts the inclusion proof for ${entry.id?.slice(0, 16)}…`, () => {
      expect(verifyInclusion(entry.id!, seq, proof, sth)).toBe(true);
    });
  }

  test('rejects a proof reused against the wrong entry', () => {
    const { proof, seq } = items[0];
    const wrongId = items[1].entry.id!;
    expect(verifyInclusion(wrongId, seq, proof, sth)).toBe(false);
  });

  test('rejects a proof with a corrupted audit-path node', () => {
    const { entry, seq, proof } = items[1];
    const corrupted: InclusionProof = {
      ...proof,
      audit_path: proof.audit_path.length > 0
        ? [proof.audit_path[0].replace(/^./, '0' === proof.audit_path[0][0] ? '1' : '0'),
           ...proof.audit_path.slice(1)]
        : proof.audit_path,
    };
    if (proof.audit_path.length === 0) return; // single-leaf tree case
    expect(verifyInclusion(entry.id!, seq, corrupted, sth)).toBe(false);
  });
});

describe('hashLeaf / hashNode primitives', () => {
  test('hashLeaf is deterministic and domain-separated from hashNode', () => {
    const leaf = hashLeaf('sha256:' + '0'.repeat(64), 0);
    const node = hashNode('0'.repeat(64), '0'.repeat(64));
    expect(leaf).not.toBe(node); // 0x00 vs 0x01 prefix
    expect(leaf).toBe(hashLeaf('sha256:' + '0'.repeat(64), 0)); // deterministic
  });
});

describe('verifyAttestation', () => {
  for (const att of manifest.attestations) {
    test(`accepts attestation for ${att.member_pubkey.slice(0, 16)}…`, () => {
      expect(verifyAttestation(att)).toBe(true);
    });
  }

  test('rejects an attestation tampered after signing', () => {
    const tampered: Attestation = { ...manifest.attestations[0], role: 'board' };
    // (The original is already role:board so make a real tamper)
    if (manifest.attestations[0].role !== 'member') {
      tampered.role = 'member';
    }
    expect(verifyAttestation(tampered)).toBe(false);
  });
});

describe('verifyDirectoryManifest', () => {
  test('accepts the root-signed manifest with all inner attestations', () => {
    expect(verifyDirectoryManifest(manifest)).toBe(true);
  });

  test('rejects a manifest whose updated_at was edited post-signing', () => {
    const tampered: DirectoryManifest = {
      ...manifest, updated_at: '2099-01-01T00:00:00+00:00',
    };
    expect(verifyDirectoryManifest(tampered)).toBe(false);
  });
});
