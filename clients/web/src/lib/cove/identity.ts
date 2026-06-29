/**
 * Identity assembly + root-side signing. v0.4.0 admin UI.
 *
 * Counterpart to cove.identity.issue_attestation / issue_directory: lets
 * the keymaster's Cove client build a fresh root-signed manifest in-app
 * and post it to /admin/attest, with the same JCS canonicalization the
 * Python side uses. The actual Ed25519 signing roundtrips through Rust
 * (rootKeychain.signMessage) so root.priv never reaches the JS heap.
 *
 * Trust posture: building a manifest here doesn't grant the keymaster
 * anything they didn't already have — the hub validates the root
 * signature over the canonical content on receipt. If this code
 * canonicalizes wrong, the sig fails and the hub rejects.
 */
import { canonicalize } from './crypto';
import type { Attestation, DirectoryManifest, Revocation } from './types';

/** All-zero sentinel matching cove.identity._ZERO_PREV_MANIFEST. Used
 *  for the very first manifest in a chain; non-genesis updates pass
 *  the prior head's hash via hashManifest. */
const ZERO_PREV_MANIFEST = 'sha256:' + '0'.repeat(64);

/** Signer that can produce an Ed25519 signature over arbitrary bytes
 *  with the org root private key. In practice this is a thin wrapper
 *  around `rootKeychain.signMessage` so root.priv stays in the OS
 *  keychain on the keymaster's device. */
export interface RootSigner {
  /** Returns hex-encoded signature. */
  sign(message: Uint8Array): Promise<string>;
  /** The org pubkey hex this signer corresponds to. Used to populate
   *  the `issuer` field on attestations + the `org` field on manifests. */
  pubkey(): Promise<string>;
}

function attContent(att: Omit<Attestation, 'sig'>): Record<string, unknown> {
  // Mirror Python _att_content: every field except sig, in dict form.
  // canonicalize() handles JCS sort order so insertion order here
  // doesn't matter.
  return {
    member_pubkey: att.member_pubkey,
    enc_pubkey: att.enc_pubkey,
    display_name: att.display_name,
    affiliation: att.affiliation,
    role: att.role,
    title: att.title,
    issued_at: att.issued_at,
    expires_at: att.expires_at,
    issuer: att.issuer,
  };
}

function manifestContent(m: Omit<DirectoryManifest, 'sig'>): Record<string, unknown> {
  const out: Record<string, unknown> = {
    org: m.org,
    // Full signed attestations are passed through verbatim — each
    // attestation's own sig is part of the manifest's signed payload.
    attestations: m.attestations.map((a) => ({ ...a })),
    revocations: m.revocations.map((r) => ({ ...r })),
    updated_at: m.updated_at,
    prev_manifest_hash: m.prev_manifest_hash,
  };
  // v0.4.13: omit when undefined so this is byte-identical to the
  // pre-v0.4.13 payload for manifests that don't carry the hint. Must
  // match Python identity.py::_manifest_content and verify.ts.
  if (m.default_thread != null) {
    out.default_thread = m.default_thread;
  }
  return out;
}

/** Build and root-sign a single Attestation. The keymaster fills
 *  display_name/affiliation/role/title; issued_at defaults to now. */
export async function issueAttestation(
  signer: RootSigner,
  opts: {
    memberPubkey: string;
    displayName: string;
    affiliation: string;
    role: 'member' | 'officer' | 'board' | string;
    title?: string | null;
    encPubkey?: string | null;
    issuedAt?: string;
    expiresAt?: string | null;
  },
): Promise<Attestation> {
  const issuer = await signer.pubkey();
  const att: Omit<Attestation, 'sig'> = {
    member_pubkey: opts.memberPubkey,
    enc_pubkey: opts.encPubkey ?? null,
    display_name: opts.displayName,
    affiliation: opts.affiliation,
    role: opts.role,
    title: opts.title ?? null,
    issued_at: opts.issuedAt ?? new Date().toISOString(),
    expires_at: opts.expiresAt ?? null,
    issuer,
  };
  const sig = await signer.sign(canonicalize(attContent(att)));
  return { ...att, sig };
}

/** Build and root-sign a full DirectoryManifest. The caller supplies
 *  the full attestation + revocation lists (typically the existing
 *  manifest's lists plus one freshly-issued attestation) plus the
 *  prev_manifest_hash from the current head. The hub rejects an
 *  update with a stale prev hash, so two admins acting concurrently
 *  get a 409 and re-pull. */
export async function issueDirectory(
  signer: RootSigner,
  opts: {
    org?: string;
    attestations: Attestation[];
    revocations: Revocation[];
    updatedAt?: string;
    prevManifestHash?: string;
    /** v0.4.13: forward the existing manifest's default_thread when
     *  re-issuing so an admin who's only updating attestations doesn't
     *  silently strip the hint. Set explicitly to override. */
    defaultThread?: string | null;
  },
): Promise<DirectoryManifest> {
  const org = opts.org ?? await signer.pubkey();
  const m: Omit<DirectoryManifest, 'sig'> = {
    org,
    attestations: opts.attestations,
    revocations: opts.revocations,
    updated_at: opts.updatedAt ?? new Date().toISOString(),
    prev_manifest_hash: opts.prevManifestHash ?? ZERO_PREV_MANIFEST,
    ...(opts.defaultThread != null ? { default_thread: opts.defaultThread } : {}),
  };
  const sig = await signer.sign(canonicalize(manifestContent(m)));
  return { ...m, sig };
}
