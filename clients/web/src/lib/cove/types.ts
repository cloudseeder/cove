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

/** v0.4.27: audience scope for a thread, set by a kind='audience' entry.
 *  Conditionally present on canonical Entry content. Mirrors
 *  cove.entry.Audience.
 *
 *  The hub computes per-thread current audience by walking audience
 *  entries oldest-first: first one establishes by any author, subsequent
 *  ones honored only if author is in the current audience. */
export interface Audience {
  pubkeys: string[];
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
  kind: 'notice' | 'post' | 'reply' | 'supersede' | 'membership' | 'receipt' | 'revoke' | 'branch' | 'archive' | 'reopen' | 'audience' | 'tombstone';
  created_at: string; // rfc3339
  parents: string[];
  body: string;
  blobs: BlobRef[];
  supersedes: string | null;
  receipt: Receipt | null;
  branch_thread: string | null;
  /** v0.4.27: set on kind='audience' entries. Conditionally omitted
   *  from canonical content when null so adding the field doesn't
   *  invalidate every pre-v0.4.27 signature. */
  audience?: Audience | null;
  /** v0.4.38: RFC3339 not-before on kind='tombstone' entries. Same
   *  byte-identical-when-null rule as audience. */
  tombstone_valid_after?: string | null;
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
  /** v0.4.27: server-computed current audience for this thread, or
   *  null when public. The hub only returns rows where the caller is
   *  in the audience, so receiving a non-null `audience` here means
   *  "I'm a member of this private thread." */
  audience: Audience | null;
  /** v0.4.38: thread lifecycle type.
   *   - "permanent"  — the default. Follows the main tamper-evident log.
   *   - "ephemeral"  — live thread with a TTL; deletes at expires_at.
   *   - "tombstoned" — sealed ephemeral. Entries are gone; final_sth
   *                    is the surviving commitment. */
  type?: 'permanent' | 'ephemeral' | 'tombstoned';
  expires_at?: string | null;      // ephemeral only
  creator_pubkey?: string;          // ephemeral only
  tombstoned_at?: string;           // tombstoned only
  final_sth?: EphemeralSTHWire | null; // tombstoned only
}

/** v0.4.38: per-thread ephemeral STH — same shape as STH plus a
 *  `thread` field bound into the signature. Cross-tree substitution
 *  fails because the signing payload includes the thread name. */
export interface EphemeralSTHWire {
  thread: string;
  tree_size: number;
  root_hash: string;
  prev_sth_hash: string;
  timestamp: string;
  hub_key: string;
  sig: string;
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
  /** v0.4.27: server-computed audience or null when public. Same
   *  semantics as ThreadSummary.audience. */
  audience: Audience | null;
}

/** v0.5.2: single hit from GET /search. The hub returns an audience-
 *  scoped snippet windowed around the match — no need to re-search
 *  client-side. Clicking a result jumps to `thread` in the sidebar;
 *  entry_id is kept for a future "highlight the matching entry" move. */
export interface SearchResult {
  thread: string;
  entry_id: string;
  seq: number;
  kind: string;
  author: string;
  created_at: string;
  snippet: string;
}

/** v0.4.33: an outstanding invite code. Carries enough metadata for
 *  the admin panel to render a row (when minted, when it expires, why
 *  the keymaster created it, current status). The code itself is the
 *  only field that matters for the wire — everything else is
 *  bookkeeping. */
export interface Invite {
  code: string;
  /** Seconds remaining until expiry — server-computed at fetch time
   *  so the client doesn't need to know the hub's process-start
   *  monotonic origin. */
  expires_in_seconds: number;
  created_at: number;     // monotonic; only useful relative to other fields
  expires_at: number;     // same clock
  name_hint: string | null;
  consumed_at: number | null;
  revoked_at: number | null;
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
  /** v0.4.38: present ONLY on ephemeral per-thread STHs. The signing
   *  payload includes it so an STH from thread A can't be relabeled
   *  as B. Absent on main-log STHs — the byte-identical-when-absent
   *  rule keeps verification stable across both shapes. */
  thread?: string;
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

/** v0.4.35: per-entry delivery ledger snapshot from `GET /ledger`. Mirrors
 *  `cove.index.Ledger.status`. `acked` and `not_acked` partition the
 *  attested directory by whether each member's high-water seq in the
 *  entry's thread is at or past the entry's seq. The not_acked list is
 *  the actionable one — those are the people whose hub hasn't yet posted
 *  a receipt covering this entry. */
export interface LedgerStatus {
  acked: string[];
  not_acked: string[];
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

/** Mirrors cove.identity.KeypairGroup. v0.4.64.
 *  Admin-defined logical grouping of member pubkeys under a display
 *  name — a shortcut for the audience picker so "Kevin + Kevin's Phone"
 *  becomes one click. Audience on the wire is still a flat pubkey
 *  list; group membership is UX layer only. */
export interface KeypairGroup {
  name: string;
  member_pubkeys: string[];
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
  /** v0.4.64: optional admin-defined keypair groups. Same
   *  byte-identical-when-absent rule. Used by the audience picker as
   *  a shortcut ("Add Kevin's 2 keypairs at once"); the wire audience
   *  is still a flat pubkey list. */
  groups?: KeypairGroup[];
  sig: string;
}

/** v0.4.25 + v0.5.0: hardcoded fallback when the manifest doesn't set
 *  capabilities_by_role. Mirrors cove.identity.DEFAULT_CAPABILITIES_BY_ROLE.
 *  v0.5.0 adds `manage_audience` (the gate for removing OTHER members from
 *  a thread's audience — self-leave and additive changes stay open); board
 *  gets it plus admin + archive, officer gets it on its own (officer's
 *  first default cap). */
export const DEFAULT_CAPABILITIES_BY_ROLE: Record<string, string[]> = {
  board: ['admin', 'archive', 'manage_audience'],
  officer: ['manage_audience'],
};

/** v0.4.25: closed set of protocol-defined capability strings.
 *  Manifests may reference unrecognized names — they're tolerated
 *  (forward-compat) but never grant anything. */
export const CAPABILITIES = ['admin', 'archive', 'manage_audience'] as const;
export type Capability = typeof CAPABILITIES[number];
