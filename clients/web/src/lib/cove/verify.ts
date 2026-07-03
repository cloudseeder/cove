/**
 * Verification primitives — TS counterpart of cove.entry.verify_entry,
 * cove.translog.verify_sth / verify_inclusion, cove.identity.verify_attestation
 * / verify_directory_manifest.
 *
 * The full client-spec §5 chain composes from these. UI code should never
 * call individual primitives — it should consume VerifiedEntry-style
 * objects produced by the Client (slice 2) which has already run the chain.
 *
 * These are kept pure (no I/O, no globals) so they're trivially unit-
 * testable against fixtures captured from the Python implementation.
 */
import {
  canonicalize, contentId, sha256Hex, verify as verifySig,
} from './crypto';
import type {
  Attestation, DirectoryManifest, Entry, InclusionProof, STH,
} from './types';
import { bytesToHex, hexToBytes } from '@noble/hashes/utils';
import { sha256 } from '@noble/hashes/sha256';

// ---- entry (§3) -------------------------------------------------------
const KINDS = new Set([
  'notice', 'post', 'reply', 'supersede', 'membership', 'receipt', 'revoke',
  'branch', 'archive', 'reopen', 'audience', 'tombstone',
]);

const NON_CONTENT = new Set(['id', 'sig']);

/** content() — the dict the id + sig commit to: every field but id and sig.
 *  v0.4.27: audience is conditionally omitted when null/undefined,
 *  mirroring Python Entry.content(), so adding the field doesn't
 *  break verification of older entries.
 *  v0.4.38: same byte-identical-when-null rule for tombstone_valid_after.
 *  MUST mirror client.ts:signEntry exactly — otherwise every entry the
 *  client signs verifies against different bytes than it was signed
 *  over, and the whole app rejects its own posts. */
export function entryContent(ev: Entry): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(ev)) {
    if (NON_CONTENT.has(k)) continue;
    if (k === 'audience' && (v === null || v === undefined)) continue;
    if (k === 'tombstone_valid_after' && (v === null || v === undefined)) continue;
    out[k] = v;
  }
  return out;
}

/**
 * Verify an entry intrinsically: id matches sha256(canonical(content))
 * and sig verifies against author over the same canonical bytes.
 *
 * Does NOT check directory attestation or inclusion proof — that's
 * the Client's job (full §5 chain).
 */
export function verifyEntry(ev: Entry): boolean {
  if (ev.id === null || ev.sig === null) return false;
  if (!KINDS.has(ev.kind)) return false;
  if (contentId(entryContent(ev)) !== ev.id) return false;
  return verifySig(ev.author, ev.sig, canonicalize(entryContent(ev)));
}

// ---- STH (§6.4.1) ----------------------------------------------------
// v0.4.42: byte-identical-when-absent for the `thread` field. Ephemeral
// per-thread STHs bind the thread name into the signing payload (see
// cove.translog_ephemeral._sth_content); main-log STHs don't have the
// field at all. Both shapes flow through this one verifier — include
// `thread` only when the wire STH carries it, and the recomputed bytes
// match whichever shape the hub signed. MUST mirror the two Python
// _sth_content functions exactly.
function sthContent(sth: STH): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (sth.thread !== undefined && sth.thread !== null) {
    out.thread = sth.thread;
  }
  out.tree_size = sth.tree_size;
  out.root_hash = sth.root_hash;
  out.prev_sth_hash = sth.prev_sth_hash;
  out.timestamp = sth.timestamp;
  out.hub_key = sth.hub_key;
  return out;
}

export function verifySth(sth: STH): boolean {
  return verifySig(sth.hub_key, sth.sig, canonicalize(sthContent(sth)));
}

// ---- inclusion proof (§6.4.2 / RFC 6962) -----------------------------
const LEAF_PREFIX = new Uint8Array([0x00]);
const NODE_PREFIX = new Uint8Array([0x01]);

function concatBytes(...parts: Uint8Array[]): Uint8Array {
  let total = 0;
  for (const p of parts) total += p.length;
  const out = new Uint8Array(total);
  let off = 0;
  for (const p of parts) { out.set(p, off); off += p.length; }
  return out;
}

function u64BE(n: number): Uint8Array {
  const buf = new Uint8Array(8);
  // JS bitwise is 32-bit, but seq <= 2^53; split into hi/lo.
  const hi = Math.floor(n / 0x100000000);
  const lo = n >>> 0;
  buf[0] = (hi >>> 24) & 0xff;
  buf[1] = (hi >>> 16) & 0xff;
  buf[2] = (hi >>> 8) & 0xff;
  buf[3] = hi & 0xff;
  buf[4] = (lo >>> 24) & 0xff;
  buf[5] = (lo >>> 16) & 0xff;
  buf[6] = (lo >>> 8) & 0xff;
  buf[7] = lo & 0xff;
  return buf;
}

const enc = new TextEncoder();

export function hashLeaf(entryId: string, seq: number): string {
  // Mirror Python cove.translog.hash_leaf: sha256(0x00 || seq_be64 || entry_id_ascii)
  return bytesToHex(sha256(concatBytes(LEAF_PREFIX, u64BE(seq), enc.encode(entryId))));
}

export function hashNode(leftHex: string, rightHex: string): string {
  return bytesToHex(sha256(concatBytes(NODE_PREFIX, hexToBytes(leftHex), hexToBytes(rightHex))));
}

function lp2(n: number): number {
  // Largest power of two strictly less than n.
  return 1 << ((n - 1).toString(2).length - 1);
}

function recomputeRoot(leafHash: string, m: number, n: number, path: string[]): string {
  if (n <= 1) return leafHash;
  const k = lp2(n);
  const sib = path[path.length - 1];
  const rest = path.slice(0, -1);
  if (m < k) {
    return hashNode(recomputeRoot(leafHash, m, k, rest), sib);
  }
  return hashNode(sib, recomputeRoot(leafHash, m - k, n - k, rest));
}

export function verifyInclusion(
  entryId: string, seq: number, proof: InclusionProof, sth: STH,
): boolean {
  if (proof.leaf_index < 0 || proof.leaf_index >= proof.tree_size) return false;
  if (proof.tree_size !== sth.tree_size) return false;
  const leaf = hashLeaf(entryId, seq);
  return recomputeRoot(leaf, proof.leaf_index, proof.tree_size, proof.audit_path) === sth.root_hash;
}

// ---- attestation + directory manifest (§2.2 / §2.3) -------------------
function attContent(att: Attestation): Record<string, unknown> {
  const { sig, ...rest } = att;
  return rest;
}

export function verifyAttestation(att: Attestation): boolean {
  return verifySig(att.issuer, att.sig, canonicalize(attContent(att)));
}

function manifestContent(m: DirectoryManifest): Record<string, unknown> {
  const out: Record<string, unknown> = {
    org: m.org,
    attestations: m.attestations.map((a) => ({ ...a })),
    revocations: m.revocations.map((r) => ({ ...r })),
    updated_at: m.updated_at,
    prev_manifest_hash: m.prev_manifest_hash,
  };
  // v0.4.13: include only when set so pre-v0.4.13 manifests round-trip
  // byte-identical. Must match Python identity.py::_manifest_content.
  if (m.default_thread != null) {
    out.default_thread = m.default_thread;
  }
  // v0.4.25: same byte-identical-when-absent rule. The wire form from
  // the hub is already normalized (sorted + deduped per role); we
  // include it verbatim so the bytes we hash equal the bytes the
  // signer signed.
  if (m.capabilities_by_role != null) {
    const normalized: Record<string, string[]> = {};
    for (const [role, caps] of Object.entries(m.capabilities_by_role)) {
      normalized[role] = [...new Set(caps)].sort();
    }
    out.capabilities_by_role = normalized;
  }
  // v0.4.64: keypair groups. Same byte-identical-when-absent rule.
  // Normalize per-group (sort/dedupe pubkeys) and cross-group (sort by
  // name) so the bytes we hash equal the bytes the root signer signed.
  // Must match Python identity.py::_manifest_content and identity.ts.
  if (m.groups != null) {
    out.groups = [...m.groups]
      .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
      .map((g) => ({
        name: g.name,
        member_pubkeys: [...new Set(g.member_pubkeys)].sort(),
      }));
  }
  return out;
}

export function verifyDirectoryManifest(m: DirectoryManifest): boolean {
  if (!verifySig(m.org, m.sig, canonicalize(manifestContent(m)))) return false;
  for (const att of m.attestations) {
    if (!verifyAttestation(att)) return false;
  }
  return true;
}

export function hashManifest(m: DirectoryManifest): string {
  return 'sha256:' + sha256Hex(canonicalize({ ...manifestContent(m), sig: m.sig }));
}
