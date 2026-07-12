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

import { AuthenticationError, ClientError, StaleVaultError, VerificationError } from './errors';
import type { VaultRecord } from './vault-blob';
import { canonicalize, sign } from './crypto';
import { keychain } from './tauri';
import {
  verifyDirectoryManifest, verifyEntry, verifyInclusion, verifySth,
} from './verify';
import type {
  Attestation, BlobRef, DirectoryManifest, Entry, InboxRow, InclusionProof,
  Invite, LedgerStatus, Revocation, STH, SearchResult, ThreadSummary,
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
  /** v0.4.17: fired after a session token is refreshed (NOT on the initial
   *  authenticate). State uses this to swap the token into the long-lived
   *  Tauri WS subscriber, which would otherwise reconnect forever with the
   *  expired token. */
  onSessionRefreshed?: (token: string, expiresAt: number) => void;
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

  // v0.4.17: transparent session refresh. Pre-emptive timer fires at
  // ~60s before expiresAt so the next call sees a fresh token without
  // friction; the 401-retry path inside authedFetch covers the case
  // where the timer didn't fire on time (host slept past expiry).
  private onSessionRefreshed?: (token: string, expiresAt: number) => void;
  private refreshTimer: ReturnType<typeof setTimeout> | null = null;
  private refreshInFlight: Promise<void> | null = null;

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
    this.onSessionRefreshed = opts.onSessionRefreshed;
  }

  /** Release the pre-emptive-refresh timer. Called by AppState.reset()
   *  so an orphaned client doesn't keep re-authenticating in the
   *  background after the user signs out. */
  dispose(): void {
    if (this.refreshTimer !== null) {
      clearTimeout(this.refreshTimer);
      this.refreshTimer = null;
    }
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

  /** v0.4.25: the cached directory manifest from the last fetch.
   *  Lets AppState read capabilities_by_role + default_thread without
   *  re-fetching. Null until fetchDirectory has run. */
  currentManifest(): DirectoryManifest | null {
    return this.directory;
  }

  /** v0.4.23: enumerate all currently-attested, non-revoked members.
   *  Drives the AdminPanel membership editor. Empty array (not null)
   *  when the directory hasn't been loaded yet so callers can map
   *  over it unconditionally. */
  currentMembers(): Attestation[] {
    if (this.directoryView === null) return [];
    return this.directoryView.currentMembers();
  }

  /** v0.4.24: revocations + their last-known attestation, newest first.
   *  Drives the AdminPanel "Recently revoked" tombstone section. */
  recentlyRevoked(): RevokedEntry[] {
    if (this.directoryView === null) return [];
    return this.directoryView.recentlyRevoked();
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
    this.scheduleSessionRefresh();
    return body.token;
  }

  /** v0.4.17: schedule a pre-emptive re-auth ~60s before sessionExpiresAt.
   *  When it fires, refreshSession() updates the in-memory token and
   *  notifies AppState via onSessionRefreshed so the WS subscriber can
   *  swap in the fresh credential.
   *
   *  setTimeout in Node uses a signed 32-bit int for the delay (max
   *  ~24.86 days). A larger value is silently clamped to 1 ms, which
   *  would fire-and-re-schedule in a tight loop. We cap defensively;
   *  for the production 1h TTL this never triggers. */
  private scheduleSessionRefresh(): void {
    if (this.refreshTimer !== null) {
      clearTimeout(this.refreshTimer);
      this.refreshTimer = null;
    }
    if (this.sessionExpiresAt === null) return;
    const SAFE_MAX = 2_147_483_647;
    const ms = (this.sessionExpiresAt * 1000) - Date.now() - 60_000;
    const delay = Math.min(SAFE_MAX, Math.max(0, ms));
    this.refreshTimer = setTimeout(() => {
      void this.refreshSession().catch(() => {
        // A refresh failure here is logged through the next authedFetch
        // attempt — that's the path that surfaces user-visible state.
        // Swallow here so an unhandled rejection doesn't escape the
        // setTimeout callback.
      });
    }, delay);
  }

  /** v0.4.17: re-runs the auth handshake, then notifies the AppState
   *  listener so the long-lived WS subscriber can pick up the new token.
   *  Concurrent callers (pre-emptive timer + a 401-retry firing at once)
   *  share one in-flight refresh — we don't want N parallel /auth/verify
   *  round-trips. */
  private async refreshSession(): Promise<void> {
    if (this.refreshInFlight) return this.refreshInFlight;
    this.refreshInFlight = (async () => {
      try {
        await this.authenticate();
        if (this.sessionToken !== null && this.sessionExpiresAt !== null) {
          this.onSessionRefreshed?.(this.sessionToken, this.sessionExpiresAt);
        }
      } finally {
        this.refreshInFlight = null;
      }
    })();
    return this.refreshInFlight;
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
   *  the auth flow (the pubkey is already in the directory).
   *
   *  v0.4.33: `invite` is REQUIRED. The hub returns 401 with reason
   *  invite_required / invite_unusable if missing or invalid; the
   *  caller surfaces the specific reason in the onboarding UI. */
  async registerPending(opts: {
    pubkey: string;
    nameHint: string;
    invite: string;
    requestedAt?: string;
  }): Promise<void> {
    const resp = await this.fetchImpl(this.hubUrl + '/pending', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        pubkey: opts.pubkey,
        name_hint: opts.nameHint,
        requested_at: opts.requestedAt ?? new Date().toISOString(),
        invite: opts.invite,
      }),
    });
    if (resp.status === 409) {
      throw new ClientError('already_attested');
    }
    if (resp.status === 401) {
      const body = await safeJson(resp);
      // v0.4.33: surface the precise reason so the onboarding UI can
      // pick the right message ("ask keymaster for a new code" vs
      // "code already used by someone" vs "code expired").
      throw new ClientError(
        body?.reason
          ? `invite_${body.reason}`
          : (body?.error ?? 'invite_required'),
      );
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
    const resp = await this.authedFetch(this.hubUrl + `/pending/${pubkey}`, {
      method: 'DELETE',
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

  /** v0.4.23: POST /admin/revoke — sibling of submitAttestation. The
   *  endpoint expects the same manifest shape (root-signed, chained off
   *  the current head, full attestations + revocations); the only
   *  semantic difference is the hub does NOT fire pending-watcher hooks
   *  for revoke posts. Use this when the manifest's net change is a
   *  new Revocation. */
  async submitRevocation(manifest: DirectoryManifest): Promise<{
    manifest_hash: string;
  }> {
    this.requireAuth();
    return await this.requestJson('POST', '/admin/revoke', { manifest });
  }

  /** v0.4.24: POST /admin/limits — per-identity throttle override.
   *  payload + sig were assembled by the caller (root-signed canonical
   *  bytes of `payload`). Throttle overrides are PROCESS-LOCAL on the
   *  hub by design (spec §7.2.2): they evaporate on hub restart, so
   *  this is a "raise the board's tier for an annual mailing" knob,
   *  not a durable role change. Use the membership editor for the
   *  latter. */
  async submitTierOverride(opts: {
    payload: { pubkey: string; tier: string };
    sig: string;
  }): Promise<{ pubkey: string; tier: string }> {
    this.requireAuth();
    return await this.requestJson('POST', '/admin/limits', opts);
  }

  /** v0.4.33: POST /admin/invites — root-signed mint of a single-use,
   *  time-limited invite code. The keymaster delivers the returned
   *  code out-of-band (text / in-person / Signal) to the prospective
   *  member; the member enters it in "Get started" to admit themselves
   *  to the queue. */
  async submitInviteMint(opts: {
    payload: { ttl_seconds: number; name_hint?: string };
    sig: string;
  }): Promise<Invite> {
    this.requireAuth();
    return await this.requestJson('POST', '/admin/invites', opts);
  }

  /** v0.4.33: GET /admin/invites — admin-cap-gated list of currently
   *  active (unused, unrevoked, unexpired) codes. The admin panel
   *  surfaces this so the keymaster can see what's outstanding and
   *  revoke any that shouldn't be. */
  async fetchInvites(): Promise<Invite[]> {
    this.requireAuth();
    const data = await this.requestJson('GET', '/admin/invites');
    return data.invites as Invite[];
  }

  /** v0.4.33: DELETE /admin/invites/{code} — root-signed revoke.
   *  payload.code MUST equal the URL path (binds the sig to the
   *  specific code, not a generic 'revoke' action). */
  async submitInviteRevoke(opts: {
    code: string;
    payload: { code: string };
    sig: string;
  }): Promise<Invite> {
    this.requireAuth();
    const resp = await this.authedFetch(
      `${this.hubUrl}/admin/invites/${encodeURIComponent(opts.code)}`,
      {
        method: 'DELETE',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ payload: opts.payload, sig: opts.sig }),
      },
    );
    if (resp.status !== 200) {
      const body = await safeJson(resp);
      throw new ClientError(
        `revoke invite failed: ${resp.status} ${JSON.stringify(body)}`,
      );
    }
    return await resp.json();
  }

  /** v0.4.76: GET /vault/{pubkey}. PUBLIC — no auth. Returns null on
   *  404 (a missing vault is a normal cold-start state, not an error).
   *  The response body is opaque ciphertext; decryption is up to the
   *  caller via vault-blob.ts::unlockWithPassphrase / unlockWithPasskey. */
  async fetchVault(pubkey: string): Promise<VaultRecord | null> {
    const resp = await this.fetchImpl(
      `${this.hubUrl}/vault/${encodeURIComponent(pubkey)}`,
      { method: 'GET' },
    );
    if (resp.status === 404) return null;
    if (resp.status !== 200) {
      const body = await safeJson(resp);
      throw new ClientError(
        `fetch vault failed: ${resp.status} ${JSON.stringify(body)}`,
      );
    }
    const wrapped = await resp.json();
    // The hub wraps the JCS body in a b64url envelope for JSON transport.
    // Decode + parse to recover the VaultRecord shape.
    const bodyBytes = b64urlDecode(wrapped.body as string);
    return JSON.parse(new TextDecoder().decode(bodyBytes)) as VaultRecord;
  }

  /** v0.4.76: PUT /vault/{pubkey}. Auth'd. Body is the full VaultRecord
   *  (including sig). Hub verifies caller-owns-key, sig, membership, CAS.
   *
   *  On 409 stale_prev_hash, throws a typed StaleVaultError carrying the
   *  hub's current head hash — the caller (AppState.saveVault) uses that
   *  to pull-merge-retry without a second GET round-trip. */
  async pushVault(vault: VaultRecord): Promise<{
    pubkey: string; version: number; hash: string; updated_at: string;
  }> {
    this.requireAuth();
    const resp = await this.authedFetch(
      `${this.hubUrl}/vault/${encodeURIComponent(vault.pubkey)}`,
      {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(vault),
      },
    );
    if (resp.status === 409) {
      const body = await safeJson(resp);
      throw new StaleVaultError(
        `vault push rejected: ${body.reason ?? 'stale_prev_hash'}`,
        body.head_hash ?? '',
        vault.pubkey,
      );
    }
    if (resp.status !== 200) {
      const body = await safeJson(resp);
      throw new ClientError(
        `push vault failed: ${resp.status} ${JSON.stringify(body)}`,
      );
    }
    return await resp.json();
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

  /** v0.4.19: GET /inbox — landing-view bundle. One row per observed
   *  thread carrying the latest non-receipt entry preview plus the
   *  caller's high-water (= seq of their latest receipt in that thread).
   *  Drives InboxPanel; single round-trip so Unlock → painted-inbox is
   *  fast even with many threads. */
  async fetchInbox(): Promise<InboxRow[]> {
    this.requireAuth();
    const data = await this.requestJson('GET', '/inbox');
    return data.threads as InboxRow[];
  }

  /** v0.5.2: GET /search — substring search over post/reply/notice
   *  bodies + thread names. Results are audience-scoped (removed
   *  members see nothing past their removal seq) and exclude ephemeral
   *  threads. */
  async searchThreads(q: string, limit: number = 50): Promise<SearchResult[]> {
    this.requireAuth();
    const data = await this.requestJson(
      'GET', `/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    );
    return data.results as SearchResult[];
  }

  /** v0.4.35: GET /ledger?entry=… — per-entry delivery status against
   *  the attested directory. The hub returns `{acked, not_acked}` lists
   *  of member pubkeys; the caller resolves display names through the
   *  cached directory. Drives DeliveryIndicator's "N of M delivered"
   *  card. This is the accountability surface — the protocol's whole
   *  purpose was that you can see when a message hasn't landed. */
  async fetchLedger(entryId: string): Promise<LedgerStatus> {
    this.requireAuth();
    const params = new URLSearchParams({ entry: entryId });
    const data = await this.requestJson('GET', `/ledger?${params}`);
    return { acked: data.acked ?? [], not_acked: data.not_acked ?? [] };
  }

  /** v0.4.19: latest STH this client has fetched, or null. Used by
   *  state to attach an audit-grade observed_sth to receipts without
   *  paying for a fresh /sth fetch on every thread-view (sync just
   *  ran one). Falls back to fetchSth() at the call site if null. */
  latestSth(): STH | null {
    return this.lastSth;
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

    // v0.4.52: verify each entry independently. Previously an
    // exception on any single entry aborted the whole batch — one
    // broken historical entry hid every subsequent entry in the same
    // sync, and the UI looked like "only new messages showed up."
    // Now a failed verify is logged and skipped; the rest of the
    // batch still lands. The advanced high-water still reflects the
    // highest successfully-verified seq so a future sync retries the
    // failed one at some point (either it heals or stays hidden — the
    // rest of history is no longer collateral damage).
    const verified: VerifiedEntry[] = [];
    for (const item of items) {
      try {
        verified.push(await this.verify(item.entry, item.seq, sth));
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn(
          `[cove] sync: skipping entry ${item.entry.id} at seq ${item.seq}`,
          err,
        );
      }
    }

    if (verified.length > 0) {
      const maxSeq = verified.reduce((m, v) => Math.max(m, v.seq), -1);
      this.highWater.set(thread, Math.max(this.highWaterFor(thread), maxSeq));
    }
    return verified;
  }

  /** Standalone verification — used by subscribe() to verify pushed entries.
   *
   *  v0.4.31: the inclusion proof + STH come bundled from a single
   *  atomic snapshot on the hub. There's no separate /sth fetch in the
   *  verify path anymore, so the race that used to produce
   *  'inclusion proof failed under sth size=N' on the first entry of
   *  a brand-new thread is gone. sthArg is still accepted for
   *  backwards-compat (sync used to pass a batch STH) but is ignored
   *  in favor of the per-entry bundled STH. */
  async verify(entry: Entry, seq: number, _sthArg?: STH): Promise<VerifiedEntry> {
    this.requireAuth();
    if (this.directory === null) await this.fetchDirectory();

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

    // 5. inclusion proof under the bundled STH. Atomic on the hub
    // — proof.tree_size === sth.tree_size by construction.
    const { proof, sth } = await this.fetchInclusionProof(entry.id!);
    if (!verifySth(sth)) {
      throw new VerificationError('STH signature invalid — pinned hub key check failed');
    }
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

  /** v0.4.38: open a new ephemeral thread with a TTL. Builds and signs
   *  the pre-signed tombstone Entry the hub will hold for auto-seal at
   *  TTL expiration. Returns the server's open response
   *  ({thread, creator_pubkey, created_at, ttl_seconds, expires_at}). */
  async openEphemeralThread(opts: {
    thread: string;
    ttlSeconds: number;
  }): Promise<{
    thread: string;
    creator_pubkey: string;
    created_at: string;
    ttl_seconds: number;
    expires_at: string;
  }> {
    this.requireAuth();
    const nowIso = new Date().toISOString();
    const validAfterIso = new Date(
      Date.now() + opts.ttlSeconds * 1000,
    ).toISOString();
    const ts = await this.signEntry({
      thread: opts.thread,
      author: this.publicKey,
      kind: 'tombstone',
      created_at: nowIso,
      parents: [],
      body: '',
      blobs: [],
      supersedes: null,
      receipt: null,
      branch_thread: null,
      audience: null,
      tombstone_valid_after: validAfterIso,
      id: null,
      sig: null,
    });
    return await this.requestJson('POST', '/threads/ephemeral', {
      thread: opts.thread,
      ttl_seconds: opts.ttlSeconds,
      tombstone_entry: ts,
    });
  }

  /** v0.4.38: seal an ephemeral thread early. Builds a fresh tombstone
   *  Entry with valid_after = now (hub accepts ≤ now + 60s skew) and
   *  POSTs it. Returns the seal payload — final STH plus the
   *  tombstone entry as published in the main log. */
  async tombstoneThread(thread: string): Promise<unknown> {
    this.requireAuth();
    const nowIso = new Date().toISOString();
    const ts = await this.signEntry({
      thread, author: this.publicKey, kind: 'tombstone',
      created_at: nowIso, parents: [], body: '',
      blobs: [], supersedes: null, receipt: null,
      branch_thread: null, audience: null,
      tombstone_valid_after: nowIso, id: null, sig: null,
    });
    return await this.requestJson(
      'POST', `/threads/${encodeURIComponent(thread)}/tombstone`,
      { tombstone_entry: ts },
    );
  }

  /** v0.4.38: fetch the sealed EphemeralSTH for a tombstoned thread.
   *  Returns null on 404. */
  async fetchFinalSth(thread: string): Promise<{
    thread: string; tree_size: number; root_hash: string;
    prev_sth_hash: string; timestamp: string; hub_key: string; sig: string;
  } | null> {
    this.requireAuth();
    const params = new URLSearchParams({ thread });
    const resp = await this.authedFetch(
      this.hubUrl + '/ephemeral/final_sth?' + params, { method: 'GET' },
    );
    if (resp.status === 404) return null;
    if (resp.status >= 400) {
      const err = await safeJson(resp);
      throw new ClientError(`/ephemeral/final_sth returned ${resp.status}: ${JSON.stringify(err)}`);
    }
    return await resp.json();
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
    const resp = await this.authedFetch(this.hubUrl + '/blobs', {
      method: 'POST',
      headers: { 'content-type': file.type || 'application/octet-stream' },
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
    const resp = await this.authedFetch(this.hubUrl + '/blobs/' + hex, {});
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
        // v0.4.18: hub pushed a directory mutation. Refresh the local
        // cache so the next verify() resolves a freshly-attested author
        // without falling back to the retry-on-miss safety net.
        if (msg.type === 'directory_changed') {
          await this.fetchDirectory();
          return;
        }
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
      if (k === 'id' || k === 'sig') continue;
      // v0.4.27: byte-identical-when-null rule for the audience field
      // mirrors Python's Entry.content(). Pre-v0.4.27 entries don't
      // carry audience at all; including it as null would break their
      // verifiability under the new client. Conditional omission keeps
      // canonicalization stable.
      if (k === 'audience' && (v === null || v === undefined)) continue;
      // v0.4.38: same byte-identical-when-null rule for
      // tombstone_valid_after. Only kind='tombstone' entries carry it.
      if (k === 'tombstone_valid_after' && (v === null || v === undefined)) continue;
      // v0.6.0: ballot / vote fields carry the same rule so pre-v0.6.0
      // signatures still verify under the new client.
      if (k === 'ballot' && (v === null || v === undefined)) continue;
      if (k === 'vote' && (v === null || v === undefined)) continue;
      content[k] = v;
    }
    const canonical = canonicalize(content);
    const id = 'sha256:' + bytesToHex(sha256(canonical));
    // Signing happens via the Signer abstraction — InJSSigner for browser
    // mode, TauriKeychainSigner for the OS-keychain path. Either way, the
    // bytes hashed and the bytes signed are the same.
    const sig = await this.signer.sign(canonical);
    return { ...entry, id, sig };
  }

  /** v0.4.31: fetch inclusion proof + STH bundled. The hub returns
   *  both from a single atomic translog snapshot, so proof.tree_size
   *  is guaranteed to equal sth.tree_size — no client-side race
   *  between sequential /sth and /proof/inclusion fetches. */
  private async fetchInclusionProof(
    entryId: string,
  ): Promise<{ proof: InclusionProof; sth: STH }> {
    const params = new URLSearchParams({ entry: entryId });
    const resp = await this.authedFetch(
      `${this.hubUrl}/proof/inclusion?${params}`,
      {},
    );
    if (resp.status !== 200) {
      throw new VerificationError(
        `no inclusion proof for ${entryId} (status ${resp.status})`,
      );
    }
    const body = await resp.json() as InclusionProof & { sth?: STH };
    if (!body.sth) {
      // Older hub that doesn't bundle. Fall back to a separate /sth
      // fetch and accept the (small) race — LWCCOA pilot's hub is
      // always current so this branch is for browser-testing against
      // a pinned older hub.
      const sth = await this.fetchSth();
      return {
        proof: {
          leaf_index: body.leaf_index,
          tree_size: body.tree_size,
          audit_path: body.audit_path,
        },
        sth,
      };
    }
    const { sth, ...proofFields } = body;
    return { proof: proofFields, sth };
  }

  private async requestJson(
    method: 'GET' | 'POST',
    path: string,
    body?: unknown,
  ): Promise<any> {
    const headers: Record<string, string> = {};
    let init: RequestInit = { method, headers };
    if (body !== undefined) {
      headers['content-type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const resp = await this.authedFetch(this.hubUrl + path, init);
    if (resp.status >= 400) {
      const err = await safeJson(resp);
      throw new ClientError(`${path} returned ${resp.status}: ${JSON.stringify(err)}`);
    }
    return await resp.json();
  }

  /** v0.4.17: every authenticated request goes through here. Adds the
   *  Bearer header from the current session; on a 401 response (which
   *  means the hub thinks our session is dead, despite the client-side
   *  expiry check) we run refreshSession() once and replay the request.
   *
   *  This is the safety net for the case the pre-emptive timer didn't
   *  cover — e.g. the host slept past expiry, or the hub restarted and
   *  forgot our session. The common case is handled by the timer with
   *  no extra round-trip here. */
  private async authedFetch(url: string, init: RequestInit): Promise<Response> {
    const send = (): Promise<Response> => {
      const merged: Record<string, string> = {
        ...(init.headers as Record<string, string> | undefined),
      };
      if (this.sessionToken) merged.authorization = `Bearer ${this.sessionToken}`;
      return this.fetchImpl(url, { ...init, headers: merged });
    };
    const resp = await send();
    if (resp.status !== 401 || this.sessionToken === null) return resp;
    // We had a token and got 401: try once to refresh + replay. The
    // signer is still present (keychain or InJSSigner) so this is a
    // silent retry — no user prompt.
    try {
      await this.refreshSession();
    } catch {
      // Surface the original 401 to the caller; refreshSession itself
      // throwing means the hub is genuinely refusing us, not just an
      // expired session.
      return resp;
    }
    return await send();
  }

  private authHeaders(): Record<string, string> {
    return this.sessionToken
      ? { authorization: `Bearer ${this.sessionToken}` }
      : {};
  }
}

// ---- directory view (resolve + revocation lookups) ------------------
export interface RevokedEntry {
  revocation: Revocation;
  /** Last attestation ever held by the revoked pubkey, or null if the
   *  manifest contains a revocation for a pubkey that was never attested
   *  in any preserved attestation (shouldn't happen for current pilots
   *  but the type permits it). */
  attestation: Attestation | null;
}

interface DirectoryView {
  resolve(pubkey: string): Attestation | null;
  isRevoked(pubkey: string, asOf?: string): boolean;
  /** v0.4.23: enumerate currently-attested, non-revoked members for the
   *  admin membership editor. Sorted by display_name (case-insensitive)
   *  so the panel doesn't reshuffle on each refresh. */
  currentMembers(): Attestation[];
  /** v0.4.24: enumerate revocations + each one's last-known attestation
   *  for the "Recently revoked" tombstone view. Sorted by revoked_at
   *  desc (most recent first). */
  recentlyRevoked(): RevokedEntry[];
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
    currentMembers() {
      const out: Attestation[] = [];
      for (const [pk, att] of attMap) {
        if (revMap.has(pk)) continue;
        out.push(att);
      }
      out.sort((a, b) =>
        a.display_name.localeCompare(b.display_name, undefined, { sensitivity: 'base' }),
      );
      return out;
    },
    recentlyRevoked() {
      const out: RevokedEntry[] = [];
      for (const r of m.revocations) {
        // Use the earliest revoked_at for a given pubkey — same rule
        // the resolve / isRevoked path uses, so the tombstone matches
        // what every other verifier sees.
        const earliest = revMap.get(r.pubkey);
        if (!earliest || earliest.revoked_at !== r.revoked_at) continue;
        out.push({
          revocation: r,
          attestation: attMap.get(r.pubkey) ?? null,
        });
      }
      out.sort((a, b) =>
        a.revocation.revoked_at < b.revocation.revoked_at ? 1 : -1,
      );
      return out;
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

/** v0.4.76: b64url decode (no padding) for vault body envelope.
 *  Kept module-local so vault-blob.ts and client.ts don't share a
 *  cross-file b64 helper — same 4 lines each and cheaper to inline. */
function b64urlDecode(s: string): Uint8Array {
  const padded = s.replace(/-/g, '+').replace(/_/g, '/')
    + '='.repeat((4 - (s.length % 4)) % 4);
  const bin = atob(padded);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
