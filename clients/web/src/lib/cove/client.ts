/**
 * Cove TS client — counterpart to src/cove/client/client.py.
 *
 * Holds the member keypair + local state, talks to a hub. The full
 * client-spec §5 verification chain runs on every entry on the way out
 * of sync() / verify() / subscribe() — UI never re-implements the math
 * and never sees an unverified entry except as a VerificationError it
 * has to render as broken.
 *
 * What's NOT here (slice-2 deliberately):
 *   - on-disk persistence (session + high-water + last STH live in
 *     memory; slice 3 puts them in Tauri's secure storage).
 *   - throttle backoff queueing — 429 surfaces as ClientError.
 *   - blob fetch with re-hash verify.
 */
import { bytesToHex, utf8ToBytes } from '@noble/hashes/utils';
import { sha256 } from '@noble/hashes/sha256';

import { AuthenticationError, ClientError, VerificationError } from './errors';
import { canonicalize, sign } from './crypto';
import { keychain } from './tauri';
import {
  verifyDirectoryManifest, verifyEntry, verifyInclusion, verifySth,
} from './verify';
import type {
  Attestation, BlobRef, DirectoryManifest, Entry, InclusionProof, STH, ThreadSummary,
} from './types';

/**
 * Signer abstraction — the surface the Client uses to produce
 * signatures. Two implementations:
 *
 *   InJSSigner       — private key in the JS heap (browser-only mode,
 *                      tests). Convenient; not OS-keychain protected.
 *   TauriKeychainSigner — private key in the OS keychain via the Rust
 *                      shell. The private key NEVER reaches the JS
 *                      webview after import. Slice 3.
 */
export interface Signer {
  sign(message: Uint8Array): Promise<string>;
}

export class InJSSigner implements Signer {
  constructor(private privateKeyHex: string) {}
  async sign(message: Uint8Array): Promise<string> {
    return sign(this.privateKeyHex, message);
  }
}

export class TauriKeychainSigner implements Signer {
  async sign(message: Uint8Array): Promise<string> {
    return keychain.signMessage(message);
  }
}

export interface VerifiedEntry {
  entry: Entry;
  seq: number;
  sth: STH;
  inclusionProof: InclusionProof;
  attestation: Attestation;
}

/** Convenience read-only view of the verification chain for ceremony reveal. */
export function sigSummary(ve: VerifiedEntry): string {
  return (
    `Signed by ${ve.attestation.display_name} (${ve.attestation.role})`
    + ` → verified against root ${ve.attestation.issuer.slice(0, 8)}…`
    + ` → inclusion proof position ${ve.inclusionProof.leaf_index}`
    + ` of ${ve.sth.tree_size}`
  );
}

export interface ClientOptions {
  hubUrl: string;
  publicKey: string;
  /** One of: a Signer instance, or a privateKey hex string (wrapped as InJSSigner). */
  signer?: Signer;
  privateKey?: string;
  /** Override for tests; defaults to globalThis.fetch. */
  fetch?: typeof fetch;
  /** Override for tests; defaults to globalThis.WebSocket. */
  WebSocket?: typeof WebSocket;
}

export class Client {
  readonly hubUrl: string;
  readonly publicKey: string;
  private signer: Signer;
  private fetchImpl: typeof fetch;
  private WebSocketImpl: typeof WebSocket;

  private sessionToken: string | null = null;
  private sessionExpiresAt: number | null = null;
  private directory: DirectoryManifest | null = null;
  private directoryView: DirectoryView | null = null;
  private lastSth: STH | null = null;
  private highWater: Map<string, number> = new Map();

  constructor(opts: ClientOptions) {
    this.hubUrl = opts.hubUrl.replace(/\/+$/, '');
    this.publicKey = opts.publicKey;
    if (opts.signer) {
      this.signer = opts.signer;
    } else if (opts.privateKey) {
      this.signer = new InJSSigner(opts.privateKey);
    } else {
      throw new Error('Client requires either signer or privateKey');
    }
    this.fetchImpl = opts.fetch ?? globalThis.fetch.bind(globalThis);
    this.WebSocketImpl = opts.WebSocket ?? globalThis.WebSocket;
  }

  // ---- introspection -------------------------------------------------
  get authenticated(): boolean {
    return (
      this.sessionToken !== null
      && (this.sessionExpiresAt ?? 0) * 1000 > Date.now()
    );
  }

  highWaterFor(thread: string): number {
    return this.highWater.get(thread) ?? -1;
  }

  /** v0.4.0: my own attestation, resolved against the directory I
   *  most recently fetched. Returns null when the directory hasn't
   *  been loaded yet — caller must fetchDirectory first.
   *  Used by AppState to decide whether to show the admin tab. */
  myAttestation(): Attestation | null {
    if (this.directoryView === null) return null;
    return this.directoryView.resolve(this.publicKey);
  }

  /** Forget the per-thread delta-sync cursor so the next sync(thread)
   *  replays from the start. The UI calls this when it clears its
   *  in-memory entries (e.g. switching threads in/out of view) — the
   *  cursor's purpose is to avoid re-shipping already-rendered entries,
   *  so resetting it when those entries are gone is the correct pairing.
   *  Without this pairing, sync after a thread-switch round-trip returns
   *  zero entries and the feed renders empty even though the hub has
   *  entries for the thread. */
  resetHighWater(thread: string): void {
    this.highWater.delete(thread);
  }

  // ---- auth (§5) -----------------------------------------------------
  async authenticate(): Promise<string> {
    const ch = await this.requestJson('POST', '/auth/challenge');
    const sig = await this.signer.sign(utf8ToBytes(ch.nonce));
    const resp = await this.fetchImpl(this.hubUrl + '/auth/verify', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ pubkey: this.publicKey, nonce: ch.nonce, sig }),
    });
    if (resp.status !== 200) {
      const body = await safeJson(resp);
      throw new AuthenticationError(body.reason ?? `auth_verify_failed: ${resp.status}`);
    }
    const body = await resp.json();
    this.sessionToken = body.token;
    this.sessionExpiresAt = body.expires_at;
    return body.token;
  }

  // ---- directory + STH ----------------------------------------------
  async fetchDirectory(): Promise<DirectoryManifest> {
    const m = (await this.requestJson('GET', '/directory')) as DirectoryManifest;
    if (!verifyDirectoryManifest(m)) {
      throw new VerificationError('directory manifest signature invalid');
    }
    this.directory = m;
    this.directoryView = buildDirectoryView(m);
    return m;
  }

  /** v0.4.0: POST /pending — public, no auth required. The device
   *  surfaces itself in the keymaster's queue. Throws on 409
   *  already_attested so the caller can short-circuit straight to
   *  the auth flow (the pubkey is already in the directory). */
  async registerPending(opts: {
    pubkey: string;
    nameHint: string;
    requestedAt?: string;
  }): Promise<void> {
    const resp = await this.fetchImpl(this.hubUrl + '/pending', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        pubkey: opts.pubkey,
        name_hint: opts.nameHint,
        requested_at: opts.requestedAt ?? new Date().toISOString(),
      }),
    });
    if (resp.status === 409) {
      throw new ClientError('already_attested');
    }
    if (resp.status !== 200) {
      const body = await safeJson(resp);
      throw new ClientError(
        `register pending failed: ${resp.status} ${JSON.stringify(body)}`,
      );
    }
  }

  /** v0.4.0: open WS /pending/watch?pubkey=X. Resolves with the
   *  manifest_hash carried by the 'attested' push as soon as the
   *  hub signals the pubkey is now in the directory. Reconnect-safe
   *  by design: an already-attested key gets pushed immediately on
   *  handshake, so a network blip mid-attest doesn't strand the
   *  caller. */
  watchPending(pubkey: string): {
    promise: Promise<{ manifestHash: string }>;
    cancel: () => void;
  } {
    const wsUrl = new URL(this.hubUrl.replace(/^http/, 'ws') + '/pending/watch');
    wsUrl.searchParams.set('pubkey', pubkey);
    const ws = new this.WebSocketImpl(wsUrl.toString());
    let cancelled = false;
    const promise = new Promise<{ manifestHash: string }>((resolve, reject) => {
      ws.onmessage = async (event) => {
        try {
          const msg = JSON.parse(typeof event.data === 'string'
            ? event.data
            : await new Response(event.data as Blob).text());
          if (msg.type === 'attested' && msg.pubkey === pubkey) {
            resolve({ manifestHash: msg.manifest_hash });
            try { ws.close(); } catch { /* already closed */ }
          }
        } catch (err) {
          reject(err as Error);
        }
      };
      ws.onerror = () => {
        if (!cancelled) reject(new ClientError('pending watch WS error'));
      };
      ws.onclose = () => {
        if (!cancelled) {
          // Server closed without sending 'attested' — surface as error
          // so the UI can show "connection lost" instead of waiting
          // forever. The Promise resolves first when 'attested' arrived,
          // so this only fires on premature close.
          reject(new ClientError('pending watch closed before attestation'));
        }
      };
    });
    return {
      promise,
      cancel: () => {
        cancelled = true;
        try { ws.close(); } catch { /* already closed */ }
      },
    };
  }

  /** v0.4.0: GET /pending — board-auth required. Returns the queue
   *  for the admin UI. Caller is presumed to already be authenticated
   *  as a board-role member; non-board members get 403. */
  async listPending(): Promise<Array<{
    pubkey: string;
    name_hint: string;
    requested_at: string;
  }>> {
    this.requireAuth();
    const data = await this.requestJson('GET', '/pending');
    return data.pending;
  }

  /** v0.4.0: DELETE /pending/{pubkey} — board-auth required. Admin
   *  rejects a queued request (typo, suspected impostor, duplicate).
   *  Idempotent on the server. */
  async clearPending(pubkey: string): Promise<void> {
    this.requireAuth();
    const resp = await this.fetchImpl(this.hubUrl + `/pending/${pubkey}`, {
      method: 'DELETE',
      headers: this.authHeaders(),
    });
    if (resp.status !== 200) {
      throw new ClientError(`clear pending failed: ${resp.status}`);
    }
  }

  /** v0.4.0: POST /admin/attest — board-auth required AND the
   *  caller must have already root-signed the manifest. This client
   *  method is the thin POST wrapper; manifest assembly (canonical
   *  content + root sig) is done by the admin UI before calling. */
  async submitAttestation(manifest: DirectoryManifest): Promise<{
    manifest_hash: string;
  }> {
    this.requireAuth();
    return await this.requestJson('POST', '/admin/attest', { manifest });
  }

  async fetchSth(): Promise<STH> {
    const sth = (await this.requestJson('GET', '/sth')) as STH;
    if (!verifySth(sth)) {
      throw new VerificationError('STH signature invalid — pinned hub key check failed');
    }
    this.lastSth = sth;
    return sth;
  }

  /** GET /threads — used by client-side thread navigation. Returns the
   *  list of observed threads with entry_count + latest_seq, sorted by
   *  latest_seq desc (most recent activity first). */
  async fetchThreads(): Promise<ThreadSummary[]> {
    const data = await this.requestJson('GET', '/threads');
    return data.threads as ThreadSummary[];
  }

  // ---- sync (§7 + client-spec §4.1 + §5) ----------------------------
  async sync(thread: string): Promise<VerifiedEntry[]> {
    this.requireAuth();
    if (this.directory === null) await this.fetchDirectory();
    const sth = await this.fetchSth();

    const since = this.highWaterFor(thread);
    const params = new URLSearchParams({ thread, since: String(since) });
    const data = await this.requestJson('GET', `/sync?${params}`);
    const items = data.entries as Array<{ entry: Entry; seq: number }>;

    const verified: VerifiedEntry[] = [];
    for (const item of items) {
      verified.push(await this.verify(item.entry, item.seq, sth));
    }

    if (verified.length > 0) {
      const maxSeq = verified.reduce((m, v) => Math.max(m, v.seq), -1);
      this.highWater.set(thread, Math.max(this.highWaterFor(thread), maxSeq));
    }
    return verified;
  }

  /** Standalone verification — used by subscribe() to verify pushed entries.
   *
   *  IMPORTANT: when no sthArg is passed, this fetches a FRESH STH rather
   *  than reusing the cached one. The cache is stale by definition on the
   *  push path: by the time a pushed entry arrives, the tree has grown to
   *  include it, but the cached STH was captured BEFORE that growth.
   *  Sync passes sthArg explicitly so its batched entries share a single
   *  consistent STH; push gets its own fresh fetch per entry.
   *
   *  Bug history: omitting this caused 'inclusion proof failed under sth
   *  size=N' on every post-sync push — proof was for size N+1 but verify
   *  was checking against the cached size-N STH. */
  async verify(entry: Entry, seq: number, sthArg?: STH): Promise<VerifiedEntry> {
    this.requireAuth();
    if (this.directory === null) await this.fetchDirectory();
    const sth = sthArg ?? await this.fetchSth();

    // 1+2. id + sig (recomputes id, verifies sig over canonical content).
    if (!verifyEntry(entry)) {
      throw new VerificationError(`entry ${entry.id} id/sig invalid`);
    }

    // 3. directory resolution. If the cached directory doesn't know
    //    the author, the keymaster may have just attested them while
    //    this client was connected — the manifest update hasn't reached
    //    us through any push channel yet. Re-fetch /directory once and
    //    retry before giving up; that closes the window from "newly
    //    attested" to "showing up as 'not attested' to existing
    //    connected clients."
    let att = this.directoryView!.resolve(entry.author);
    if (att === null) {
      await this.fetchDirectory();
      att = this.directoryView!.resolve(entry.author);
    }
    if (att === null) {
      throw new VerificationError(`author ${entry.author} not attested`);
    }

    // 4. revocation as-of entry time. §2.3: entries signed BEFORE revocation
    // remain valid; entries signed AFTER are rejected.
    if (this.directoryView!.isRevoked(entry.author, entry.created_at)) {
      throw new VerificationError(
        `author ${entry.author} was revoked as-of ${entry.created_at}`,
      );
    }

    // 5. inclusion proof under the current STH.
    const proof = await this.fetchInclusionProof(entry.id!);
    if (!verifyInclusion(entry.id!, seq, proof, sth)) {
      throw new VerificationError(
        `inclusion proof failed for ${entry.id} under sth size=${sth.tree_size}`,
      );
    }

    return { entry, seq, sth, inclusionProof: proof, attestation: att };
  }

  // ---- post (§3) -----------------------------------------------------
  async post(entry: Entry): Promise<number> {
    this.requireAuth();
    const signed = entry.id && entry.sig ? entry : await this.signEntry(entry);
    const resp = await this.requestJson('POST', '/entries', signed);
    return resp.seq as number;
  }

  // ---- blobs (§4) ----------------------------------------------------
  /** Upload raw bytes to /blobs and assemble a BlobRef from the server
   *  response (which carries the content-addressed hash) plus the
   *  caller-supplied filename + media type. The server dedups on hash
   *  collision, so re-uploading identical bytes is cheap.
   *
   *  client-spec §3: blobs are uploaded BEFORE the entry that references
   *  them. The acceptance pipeline strict-checks that referenced blobs
   *  exist on the hub when the entry posts.  */
  async uploadBlob(file: File): Promise<BlobRef> {
    this.requireAuth();
    const buf = await file.arrayBuffer();
    const resp = await this.fetchImpl(this.hubUrl + '/blobs', {
      method: 'POST',
      headers: {
        ...this.authHeaders(),
        'content-type': file.type || 'application/octet-stream',
      },
      body: buf,
    });
    if (resp.status !== 200) {
      const body = await safeJson(resp);
      throw new ClientError(
        body?.error
          ? `blob upload ${body.error}: ${body.reason ?? resp.status}`
          : `blob upload failed: ${resp.status}`,
      );
    }
    const { hash, size } = await resp.json() as { hash: string; size: number };
    return {
      hash,
      media_type: file.type || 'application/octet-stream',
      size,
      name: file.name,
    };
  }

  /** Download a blob's raw bytes with the session bearer. Returns a Blob
   *  ready to be turned into an object URL for inline preview. Bytes
   *  are re-hashed against the BlobRef.hash on the way back — a hash
   *  mismatch is rejected as VerificationError per client-spec §4. */
  async fetchBlobBytes(ref: BlobRef): Promise<Blob> {
    this.requireAuth();
    // The path param is just the hex; the BlobRef.hash carries the
    // "sha256:" prefix which is server-side metadata, not URL.
    const hex = ref.hash.startsWith('sha256:') ? ref.hash.slice(7) : ref.hash;
    const resp = await this.fetchImpl(this.hubUrl + '/blobs/' + hex, {
      headers: this.authHeaders(),
    });
    if (resp.status !== 200) {
      throw new ClientError(`blob fetch failed: ${resp.status}`);
    }
    const bytes = new Uint8Array(await resp.arrayBuffer());
    // Re-hash to detect tamper between hub and client. The content-
    // address IS the integrity check — if the hub returns different
    // bytes than the BlobRef claims, that's a hub bug or active
    // tampering and the user should NOT see the result.
    const computed = 'sha256:' + bytesToHex(sha256(bytes));
    if (computed !== ref.hash) {
      throw new VerificationError(
        `blob hash mismatch: ref=${ref.hash} got=${computed}`,
      );
    }
    return new Blob([bytes], { type: ref.media_type });
  }

  /** Receipt assembly + sign + post in one call (§8 + §6.4.3). */
  async postReceipt(opts: {
    thread: string;
    highWaterSeq: number;
    observedSth: STH;
  }): Promise<number> {
    const ev: Entry = {
      thread: opts.thread,
      author: this.publicKey,
      kind: 'receipt',
      created_at: new Date().toISOString(),
      parents: [],
      body: '',
      blobs: [],
      supersedes: null,
      receipt: {
        high_water_seq: opts.highWaterSeq,
        observed_sth_size: opts.observedSth.tree_size,
        observed_sth_root: opts.observedSth.root_hash,
      },
      branch_thread: null,
      id: null,
      sig: null,
    };
    return this.post(ev);
  }

  // ---- subscribe (§7 WS /stream + §4.1) -----------------------------
  /**
   * Open a /stream WebSocket and verify every pushed entry through the
   * full §5 chain before invoking onEntry. Returns a teardown function.
   *
   * Use this AFTER an initial sync() so the high-water is set; per
   * client-spec §4.1 the canonical ordering is subscribe-then-sync,
   * but here we expose just the subscribe primitive — UI orchestrates
   * the order.
   */
  subscribe(
    thread: string,
    onEntry: (ve: VerifiedEntry) => void,
    onError?: (err: Error) => void,
  ): () => void {
    this.requireAuth();
    const wsUrl = new URL(this.hubUrl.replace(/^http/, 'ws') + '/stream');
    // Browsers can't set custom headers on WS handshake — the hub
    // accepts ?token= as a fallback.
    wsUrl.searchParams.set('token', this.sessionToken!);
    const ws = new this.WebSocketImpl(wsUrl.toString());

    ws.onmessage = async (event) => {
      try {
        const msg = JSON.parse(typeof event.data === 'string'
          ? event.data
          : await new Response(event.data as Blob).text());
        if (msg.type !== 'entry') return;
        const ve = await this.verify(msg.entry as Entry, msg.seq as number);
        if (ve.entry.thread !== thread) return;
        // Advance high-water as we go, so a later sync() doesn't redeliver.
        this.highWater.set(thread, Math.max(this.highWaterFor(thread), ve.seq));
        onEntry(ve);
      } catch (err) {
        onError?.(err as Error);
      }
    };
    ws.onerror = () => onError?.(new ClientError('WebSocket error'));

    return () => {
      try {
        ws.close();
      } catch {
        // already closed; ignore
      }
    };
  }

  // ---- internals -----------------------------------------------------
  private requireAuth(): void {
    if (!this.authenticated) {
      throw new AuthenticationError('not authenticated; call authenticate() first');
    }
  }

  private async signEntry(entry: Entry): Promise<Entry> {
    const content: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(entry)) {
      if (k !== 'id' && k !== 'sig') content[k] = v;
    }
    const canonical = canonicalize(content);
    const id = 'sha256:' + bytesToHex(sha256(canonical));
    // Signing happens via the Signer abstraction — InJSSigner for browser
    // mode, TauriKeychainSigner for the OS-keychain path. Either way, the
    // bytes hashed and the bytes signed are the same.
    const sig = await this.signer.sign(canonical);
    return { ...entry, id, sig };
  }

  private async fetchInclusionProof(entryId: string): Promise<InclusionProof> {
    const params = new URLSearchParams({ entry: entryId });
    const resp = await this.fetchImpl(
      `${this.hubUrl}/proof/inclusion?${params}`,
      { headers: this.authHeaders() },
    );
    if (resp.status !== 200) {
      throw new VerificationError(
        `no inclusion proof for ${entryId} (status ${resp.status})`,
      );
    }
    return await resp.json();
  }

  private async requestJson(
    method: 'GET' | 'POST',
    path: string,
    body?: unknown,
  ): Promise<any> {
    const init: RequestInit = { method, headers: this.authHeaders() };
    if (body !== undefined) {
      (init.headers as Record<string, string>)['content-type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const resp = await this.fetchImpl(this.hubUrl + path, init);
    if (resp.status >= 400) {
      const err = await safeJson(resp);
      throw new ClientError(`${path} returned ${resp.status}: ${JSON.stringify(err)}`);
    }
    return await resp.json();
  }

  private authHeaders(): Record<string, string> {
    return this.sessionToken
      ? { authorization: `Bearer ${this.sessionToken}` }
      : {};
  }
}

// ---- directory view (resolve + revocation lookups) ------------------
interface DirectoryView {
  resolve(pubkey: string): Attestation | null;
  isRevoked(pubkey: string, asOf?: string): boolean;
}

function buildDirectoryView(m: DirectoryManifest): DirectoryView {
  // Latest attestation per pubkey (by issued_at).
  const attMap = new Map<string, Attestation>();
  for (const a of m.attestations) {
    const cur = attMap.get(a.member_pubkey);
    if (!cur || a.issued_at > cur.issued_at) attMap.set(a.member_pubkey, a);
  }
  // EARLIEST revocation per pubkey — keys can't be un-revoked.
  const revMap = new Map<string, { revoked_at: string }>();
  for (const r of m.revocations) {
    const cur = revMap.get(r.pubkey);
    if (!cur || r.revoked_at < cur.revoked_at) revMap.set(r.pubkey, r);
  }
  return {
    resolve(pk) {
      return attMap.get(pk) ?? null;
    },
    isRevoked(pk, asOf) {
      const r = revMap.get(pk);
      if (!r) return false;
      if (asOf === undefined) return true;
      return asOf >= r.revoked_at;
    },
  };
}

async function safeJson(resp: Response): Promise<any> {
  try {
    return await resp.json();
  } catch {
    return {};
  }
}
