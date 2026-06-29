/**
 * Wire types — kept in lock-step with the Python dataclasses in
 * src/cove/entry.py, src/cove/identity.py, and src/cove/translog.py.
 *
 * Anything that goes over the wire must serialize identically in both
 * languages or the canonical-content hash drifts and signatures stop
 * verifying. JCS (RFC 8785) canonicalization is the contract.
 */

export interface BlobRef {
  hash: string; // "sha256:" + hex
  media_type: string;
  size: number;
  name: string;
}

export interface Receipt {
  high_water_seq: number;
  observed_sth_size: number;
  observed_sth_root: string;
}

/** Mirrors cove.entry.Entry. `id` and `sig` are populated after signing.
 *
 *  kind='branch' (v0.2) declares that a sub-thread spawned off this
 *  thread. The branch entry lives in the PARENT thread; `branch_thread`
 *  names the spawned child. Both ends of the link are in canonical
 *  content, so the signature covers them. */
export interface Entry {
  thread: string;
  author: string; // ed25519 pubkey hex
  kind: 'notice' | 'post' | 'reply' | 'supersede' | 'membership' | 'receipt' | 'revoke' | 'branch' | 'archive' | 'reopen';
  created_at: string; // rfc3339
  parents: string[];
  body: string;
  blobs: BlobRef[];
  supersedes: string | null;
  receipt: Receipt | null;
  branch_thread: string | null;
  id: string | null;
  sig: string | null;
}

/** Returned by GET /threads — one row per observed thread. Sorted
 *  by latest_seq descending. Used by the client-side thread navigation.
 *  parent_thread (v0.2) is set when this thread was spawned via a
 *  kind='branch' entry in another thread.
 *
 *  archived (v0.4.25) reflects the latest archive|reopen entry by a
 *  caller with the 'archive' capability. Server-computed under the
 *  current manifest so the rule matches what the hub enforces. */
export interface ThreadSummary {
  thread: string;
  entry_count: number;
  latest_seq: number;
  parent_thread: string | null;
  archived: boolean;
}

/** v0.4.19: returned by GET /inbox — one row per observed thread,
 *  enriched with what the InboxPanel needs to render a one-line preview
 *  + unread indicator. my_high_water is the per-thread seq of the
 *  caller's latest kind='receipt' entry, -1 if they've never receipted
 *  this thread. Unread = latest_seq > my_high_water. */
export interface InboxRow {
  thread: string;
  entry_count: number;
  latest_seq: number;
  parent_thread: string | null;
  my_high_water: number;
  latest_entry: InboxPreviewEntry | null;
  archived: boolean;
}

export interface InboxPreviewEntry {
  id: string;
  seq: number;
  author: string;
  kind: string;
  created_at: string;
  body_preview: string;
  display_name: string | null;
  role: string | null;
}

/** Mirrors cove.translog.STH. */
export interface STH {
  tree_size: number;
  root_hash: string;
  prev_sth_hash: string;
  timestamp: string;
  hub_key: string;
  sig: string;
}

/** Mirrors cove.translog.InclusionProof. */
export interface InclusionProof {
  leaf_index: number;
  tree_size: number;
  audit_path: string[];
}

/** Mirrors cove.translog.ConsistencyProof. */
export interface ConsistencyProof {
  first_size: number;
  second_size: number;
  path: string[];
}

/** Mirrors cove.identity.Attestation.
 *  v0.3 rename: `unit` → `affiliation` (generic org sub-grouping);
 *  add optional `title` (human-readable job title). Role stays as the
 *  protocol-level trust tier. */
export interface Attestation {
  member_pubkey: string;
  enc_pubkey: string | null;
  display_name: string;
  affiliation: string;
  role: 'member' | 'board' | 'officer' | string;
  title: string | null;
  issued_at: string;
  expires_at: string | null;
  issuer: string;
  sig: string;
}

/** Mirrors cove.identity.Revocation. */
export interface Revocation {
  pubkey: string;
  revoked_at: string;
  reason: string;
}

/** Mirrors cove.identity.DirectoryManifest. */
export interface DirectoryManifest {
  org: string;
  attestations: Attestation[];
  revocations: Revocation[];
  updated_at: string;
  prev_manifest_hash: string;
  /** v0.4.13: optional soft hint — "land new members on this thread
   *  after attestation." Hubs can omit. Pre-v0.4.13 manifests don't
   *  have the field at all; manifestContent() must NOT include it when
   *  undefined or signature verification of those older manifests
   *  fails. */
  default_thread?: string;
  /** v0.4.25: org-defined role → capability map. Same
   *  byte-identical-when-absent rule as default_thread. Drives
   *  hasCapability() in state and require_capability on the hub. */
  capabilities_by_role?: Record<string, string[]>;
  sig: string;
}

/** v0.4.25: hardcoded fallback when the manifest doesn't set
 *  capabilities_by_role. Mirrors cove.identity.DEFAULT_CAPABILITIES_BY_ROLE.
 *  Preserves the pre-v0.4.25 behavior (only board has admin + archive). */
export const DEFAULT_CAPABILITIES_BY_ROLE: Record<string, string[]> = {
  board: ['admin', 'archive'],
};

/** v0.4.25: closed set of protocol-defined capability strings.
 *  Manifests may reference unrecognized names — they're tolerated
 *  (forward-compat) but never grant anything. */
export const CAPABILITIES = ['admin', 'archive'] as const;
export type Capability = typeof CAPABILITIES[number];
