/**
 * HubConnection — the per-hub reactive state.
 *
 * v0.4.68 extracts everything hub-scoped out of AppState so that Phase 2
 * can hold N connections in AppState.hubs: Map<HubUrl, HubConnection>.
 * Phase 1 (this file) is a pure extraction: AppState.hub holds one
 * HubConnection and every existing `app.xxx` surface delegates via
 * getters/methods. Consumer .svelte files are unchanged.
 *
 * Owns everything the client needs to talk to one hub:
 *   - The verified-entries feed, threads, inbox rows, members, manifest
 *   - The Client instance (crypto/session/wire)
 *   - The WS teardown (Rust in Tauri; browser WS in PWA)
 *   - The onSessionRefreshed closure — captures `this` on HubConnection,
 *     which will keep hubs isolated when Phase 2 lands N of them
 *   - Admin ops (attest, revoke, capabilities, groups, invites, tiers)
 *
 * Reaches back to AppState for state that stays global:
 *   - route (inbox|thread), inTauri, inPWA, rootKeysPresent
 *
 * See /home/brooks/.claude/plans/glimmering-fluttering-boole.md for the
 * full phasing rationale.
 */
import {
  Client, TauriKeychainSigner, type RevokedEntry, type VerifiedEntry,
} from './client';
import { canonicalize } from './crypto';
import { issueAttestation, issueDirectory, type RootSigner } from './identity';
import type { AppState } from './state.svelte';
import {
  ensureNotificationPermission, rootKeychain, stream,
} from './tauri';
import type {
  Attestation, DirectoryManifest, InboxRow, Invite, KeypairGroup,
  ThreadSummary,
} from './types';
import { DEFAULT_CAPABILITIES_BY_ROLE } from './types';
import { hashManifest } from './verify';
import { saveThreadFor } from './hubs';

// Same errMsg helper as state.svelte.ts. Tauri's invoke() rejects with a
// raw string when the Rust side returns Err(String); casting that to
// Error and reading .message yields undefined. This handles all three
// shapes: Tauri string rejection, JS Error object, anything else.
function errMsg(e: unknown): string {
  if (typeof e === 'string') return e;
  if (e instanceof Error) return e.message;
  return String(e);
}

export type AuthStatus =
  | { kind: 'unauthenticated' }
  | { kind: 'connecting' }
  | { kind: 'authenticated'; pubkey: string }
  | { kind: 'failed'; reason: string };

export type ThreadStatus =
  | { kind: 'idle' }
  | { kind: 'syncing' }
  | { kind: 'error'; message: string };

/** v0.1.10: main pane faces per thread — chronological feed or
 *  per-thread files list. Reset to 'messages' on every thread switch
 *  so navigation doesn't trap the user in Files.
 *
 *  v0.4.0: 'admin' — global view (not per-thread) for the keymaster's
 *  pending-queue UI. Visible only to board-role members. Setting this
 *  doesn't change app.thread; switching threads resets back to 'messages'. */
export type View = 'messages' | 'files' | 'admin';

export interface ConnectOpts {
  hubUrl: string;
  publicKey: string;
  thread: string;
  privateKey?: string;
  mode?: 'paste' | 'keychain';
}

export class HubConnection {
  // Reactive per-hub state ------------------------------------------------
  authStatus = $state<AuthStatus>({ kind: 'unauthenticated' });
  thread = $state<string>('annual-meeting');
  threadStatus = $state<ThreadStatus>({ kind: 'idle' });
  entries = $state<VerifiedEntry[]>([]);
  /** v0.4.19: rows powering InboxPanel. Loaded by loadInbox(); refreshed
   *  on directory_changed and on goToInbox(). */
  inboxRows = $state<InboxRow[]>([]);
  inboxStatus = $state<{ kind: 'idle' } | { kind: 'loading' }
    | { kind: 'error'; message: string }>({ kind: 'idle' });
  /** All observed threads on the hub. */
  threads = $state<ThreadSummary[]>([]);
  /** When non-null, the reply panel is open pinned to this entry. */
  replyOpen = $state<VerifiedEntry | null>(null);
  /** Which face of the active thread to render. Reset to 'messages' on
   *  every switchThread. */
  view = $state<View>('messages');
  /** v0.4.0: cached pending queue for AdminPanel. */
  pendingQueue = $state<Array<{
    pubkey: string; name_hint: string; requested_at: string;
  }>>([]);
  /** v0.4.0: status of an in-flight admin action. */
  adminStatus = $state<{ kind: 'idle' } | { kind: 'submitting' }
    | { kind: 'error'; message: string }>({ kind: 'idle' });
  /** v0.4.0: caller's own attestation, resolved at connect-time. */
  myAttestation = $state<Attestation | null>(null);
  /** v0.4.25: the current directory manifest, cached so hasCapability
   *  can read capabilities_by_role without a re-fetch. */
  manifest = $state<DirectoryManifest | null>(null);
  /** v0.4.23: snapshot of currently-attested, non-revoked members. */
  members = $state<Attestation[]>([]);
  /** v0.4.24: revocations + their last-known attestation, newest first. */
  revoked = $state<RevokedEntry[]>([]);
  /** v0.4.33: minted invites for AdminPanel. */
  invites = $state<Invite[]>([]);
  invitesStatus = $state<{ kind: 'idle' } | { kind: 'loading' }
    | { kind: 'error'; message: string }>({ kind: 'idle' });
  /** v0.4.30: + New thread dialog state — audience refs per-hub members. */
  newThreadDialog = $state<{
    name: string;
    scope: 'public' | 'private';
    selected: Set<string>;
    message: string;
    submitting: boolean;
    error: string | null;
    /** v0.4.38: opt in to an ephemeral thread with a TTL. */
    ephemeral: boolean;
    ttlDays: number;
  } | null>(null);

  // Private per-hub state -------------------------------------------------
  /** v0.4.19: per-thread seq of the last receipt this session has posted
   *  (or pulled in from /inbox). */
  private myReceiptSeq: Map<string, number> = new Map();
  /** Track ids we've already shown so we never double-render after dedup. */
  private seenIds = new Set<string>();
  /** v0.4.69: the hub this connection is bound to. Set at construction
   *  time (Phase 2 restores placeholders from localStorage before any
   *  auth has run) and re-affirmed by authenticate() on the wire. Public
   *  so the sidebar switcher / storage helpers can read it. */
  hubUrl: string;
  private sessionToken = '';

  client: Client | null = null;
  private teardown: (() => void) | null = null;

  /** Back-reference to the AppState. Needed for global state that lives
   *  on AppState: `route`, `inTauri`, `inPWA`, `rootKeysPresent`, plus
   *  a handful of vault/keychain refreshes. Phase 2 keeps N HubConnection
   *  instances, all pointing at the same AppState — the app-wide state
   *  is genuinely singleton. */
  constructor(private app: AppState, hubUrl: string = '') {
    this.hubUrl = hubUrl;
  }

  // ---------------------------------------------------------------------
  // Auth / lifecycle
  // ---------------------------------------------------------------------

  /**
   * Connect to the hub with the given credentials. Constructed lazily
   * from AppState.connect() so the caller's `opts` shape is unchanged.
   *
   *   - browser / paste mode: caller provides privateKey, wrapped as
   *     InJSSigner inside Client.
   *   - Tauri / keychain mode: the private key is already in the OS
   *     keychain; caller passes mode='keychain' + publicKey. Signing
   *     roundtrips through Rust.
   */
  async authenticate(opts: ConnectOpts): Promise<void> {
    this.authStatus = { kind: 'connecting' };
    try {
      this.thread = opts.thread;
      this.hubUrl = opts.hubUrl;
      this.client = new Client({
        hubUrl: opts.hubUrl,
        publicKey: opts.publicKey,
        signer: opts.mode === 'keychain' ? new TauriKeychainSigner() : undefined,
        privateKey: opts.mode === 'keychain' ? undefined : opts.privateKey,
        // v0.4.17: transparent session refresh. When the 1h hub session
        // is renewed, swap the token into the long-lived WS subscriber.
        // Closure captures `this` on HubConnection — Phase 2 keeps each
        // hub's refresh isolated from the others.
        onSessionRefreshed: (token) => { void this.handleSessionRefreshed(token); },
      });
      const sessionToken = await this.client.authenticate();
      this.sessionToken = sessionToken;
      this.authStatus = { kind: 'authenticated', pubkey: opts.publicKey };
      // Notifications: ask once, here — the user just authenticated.
      if (this.app.inTauri) {
        void ensureNotificationPermission();
      }
      // v0.4.19: land on the email-style Inbox by default. Threads open
      // on click. Fetch the directory once up-front so myAttestation
      // resolves (AdminPanel visibility) without waiting.
      this.app.route = 'inbox';
      await this.client.fetchDirectory();
      this.myAttestation = this.client.myAttestation();
      this.manifest = this.client.currentManifest();
      this.members = this.client.currentMembers();
      this.revoked = this.client.recentlyRevoked();
      await this.loadInbox();
      // Sidebar thread list — non-blocking.
      void this.loadThreads();
      // v0.4.53: open the WebSocket subscription eagerly.
      void this.ensureSubscribed();
      // Tauri keymaster stations: surface the root keychain state.
      if (this.app.inTauri) void this.app.refreshRootKeychain();
    } catch (err) {
      this.authStatus = { kind: 'failed', reason: errMsg(err) };
      this.client?.dispose();
      this.client = null;
    }
  }

  /** v0.4.73: root-signer scoped to THIS hub's org pubkey. Every admin
   *  op that would previously build a fresh RootSigner uses this
   *  helper so per-hub root-keychain slots (Rust side) are addressed
   *  correctly. Falls back to the legacy un-suffixed slot when the
   *  manifest hasn't loaded yet — same behavior as pre-v0.4.73.
   *
   *  v0.4.80: PWA path. When !inTauri, use the decrypted root priv
   *  held in AppState.liveRootPriv. If it's null, the admin op throws
   *  a clear error the UI can catch and prompt an unlock ceremony. */
  private rootSigner(): RootSigner {
    const org = this.manifest?.org;
    if (!this.app.inTauri) {
      return {
        sign: async (m) => {
          const priv = this.app.liveRootPriv;
          if (!priv) {
            throw new Error(
              'Root vault is locked. Open Admin → Root key custody '
              + 'and enter your passphrase to unlock.',
            );
          }
          const { sign: signMessage } = await import('./crypto');
          return signMessage(priv, m);
        },
        pubkey: async () => {
          const { rootVaultStatus } = await import('./root-vault');
          const st = await rootVaultStatus(org ?? '');
          if (!st.public_key) throw new Error('root vault has no pubkey record');
          return st.public_key;
        },
      };
    }
    return {
      sign: (m) => rootKeychain.signMessage(m, org),
      pubkey: async () => (await rootKeychain.status(org)).public_key!,
    };
  }

  /** v0.4.83: sign an already-canonicalized admin payload (mint invite,
   *  revoke invite, tier override — the /admin/* endpoints that take
   *  {payload, sig} rather than a full signed manifest). Same Tauri /
   *  PWA branch as rootSigner() so admin-payload flows work from the
   *  PWA too, not just from Tauri. */
  private async signRootPayload(message: Uint8Array): Promise<string> {
    const org = this.manifest?.org;
    if (!this.app.inTauri) {
      const priv = this.app.liveRootPriv;
      if (!priv) {
        throw new Error(
          'Root vault is locked. Open Admin → Root key custody and '
          + 'enter your passphrase to unlock.',
        );
      }
      const { sign: signMessage } = await import('./crypto');
      return signMessage(priv, message);
    }
    return rootKeychain.signMessage(message, org);
  }

  /** Tear down all per-hub resources. Called by AppState.reset() and by
   *  Phase 2's removeHub(). */
  dispose(): void {
    this.entries = [];
    this.seenIds = new Set();
    this.threadStatus = { kind: 'idle' };
    this.replyOpen = null;
    this.teardown?.();
    this.teardown = null;
    this.client?.dispose();
    this.client = null;
    this.authStatus = { kind: 'unauthenticated' };
  }

  // ---------------------------------------------------------------------
  // Thread / feed
  // ---------------------------------------------------------------------

  /** Slide the reply panel open, pinned to the given entry. */
  openReplyPanel(ve: VerifiedEntry): void {
    this.replyOpen = ve;
  }

  closeReplyPanel(): void {
    this.replyOpen = null;
  }

  setView(v: View): void {
    this.view = v;
    // v0.4.19: setView is the gesture used to enter Admin / Files from
    // anywhere — it implies "leave Inbox if I'm on it."
    if (this.app.route !== 'thread') this.app.route = 'thread';
  }

  /**
   * client-spec §4.1: subscribe FIRST, then sync. Anything that lands in
   * the window between subscribe and sync arrives on both channels and
   * is deduped via seenIds.
   */
  async syncAndSubscribe(): Promise<void> {
    if (this.client === null) return;
    const client = this.client;
    this.threadStatus = { kind: 'syncing' };
    try {
      // 1. Subscribe FIRST — tearing down any prior subscription.
      await this.ensureSubscribed({ restart: true });
      // 2. Then sync from last-known seq.
      const initial = await client.sync(this.thread);
      for (const ve of initial) this.appendIfNew(ve);
      this.threadStatus = { kind: 'idle' };
    } catch (err) {
      this.threadStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.53: bring up the WS subscription if it isn't running. */
  async ensureSubscribed(opts: { restart?: boolean } = {}): Promise<void> {
    if (this.client === null) return;
    if (this.teardown !== null && !opts.restart) return;
    if (this.teardown !== null && opts.restart) {
      this.teardown();
      this.teardown = null;
    }
    if (this.app.inTauri) {
      const teardown = await stream.start(
        { hubUrl: this.hubUrl, token: this.sessionToken, thread: this.thread },
        (raw) => { void this.handlePushedRaw(raw); },
      );
      this.teardown = () => { void teardown(); };
    } else {
      // v0.4.55: browser subscribe forwards raw payloads through the
      // same handler as Tauri so unknown-thread detection fires
      // consistently across platforms.
      this.teardown = this.subscribeRawWs();
    }
  }

  /** v0.4.55: minimal WS subscriber for browser mode. */
  private subscribeRawWs(): () => void {
    if (this.client === null) return () => {};
    const wsUrl = new URL(this.hubUrl.replace(/^http/, 'ws') + '/stream');
    wsUrl.searchParams.set('token', this.sessionToken);
    const ws = new WebSocket(wsUrl.toString());
    ws.onmessage = (event) => {
      const raw = typeof event.data === 'string' ? event.data : '';
      if (raw) void this.handlePushedRaw(raw);
    };
    ws.onerror = () => {
      this.threadStatus = { kind: 'error', message: 'stream: connection error' };
    };
    return () => {
      try { ws.close(); } catch { /* already closed */ }
    };
  }

  /** v0.4.17: the Client signalled a session refresh. */
  private async handleSessionRefreshed(token: string): Promise<void> {
    this.sessionToken = token;
    if (this.client === null) return;
    this.teardown?.();
    this.teardown = null;
    await this.syncAndSubscribe();
  }

  private async handlePushedRaw(raw: string): Promise<void> {
    if (this.client === null) return;
    try {
      const msg = JSON.parse(raw);
      // v0.4.18: hub pushed a directory mutation.
      if (msg.type === 'directory_changed') {
        await this.client.fetchDirectory();
        this.myAttestation = this.client.myAttestation();
        this.manifest = this.client.currentManifest();
        this.members = this.client.currentMembers();
        this.revoked = this.client.recentlyRevoked();
        if (this.app.route === 'inbox') void this.loadInbox();
        return;
      }
      // v0.4.48 → v0.4.54: retired thread_opened broadcast. Kept as a
      // no-op for forward-compat with older self-hosted hubs.
      if (msg.type === 'thread_opened') {
        return;
      }
      // v0.4.38: an ephemeral thread was sealed.
      if (msg.type === 'thread_tombstoned') {
        const t = msg.thread as string;
        if (this.thread === t) {
          this.entries = [];
          this.seenIds.clear();
        }
        void this.loadThreads();
        if (this.app.route === 'inbox') void this.loadInbox();
        return;
      }
      if (msg.type !== 'entry') return;
      const ve = await this.client.verify(msg.entry, msg.seq);
      // v0.4.48: pushed entry may announce an unknown thread.
      if (!this.threads.some((t) => t.thread === ve.entry.thread)) {
        void this.loadThreads();
      }
      if (ve.entry.thread !== this.thread) return;
      this.appendIfNew(ve);
    } catch (err) {
      this.threadStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  private appendIfNew(ve: VerifiedEntry) {
    if (!ve.entry.id || this.seenIds.has(ve.entry.id)) return;
    this.seenIds.add(ve.entry.id);
    this.entries = [...this.entries, ve].sort((a, b) => a.seq - b.seq);
  }

  /** v0.2: branch off a sub-thread from the current thread. */
  async branchOff(newThread: string, body: string,
                  files: File[] = []): Promise<void> {
    if (this.client === null || this.authStatus.kind !== 'authenticated') return;
    if (!newThread.trim() || newThread === this.thread) return;
    const blobs = files.length === 0
      ? []
      : await Promise.all(files.map((f) => this.client!.uploadBlob(f)));
    const ev = {
      thread: this.thread,
      author: this.authStatus.pubkey,
      kind: 'branch' as const,
      created_at: new Date().toISOString(),
      parents: [],
      body,
      blobs,
      supersedes: null,
      receipt: null,
      branch_thread: newThread,
      id: null,
      sig: null,
    };
    await this.client.post(ev);
    await this.switchThread(newThread);
    void this.loadThreads();
  }

  /** v0.4.25: post a kind='archive' or kind='reopen' entry. */
  async setThreadArchived(thread: string, archived: boolean,
                          rationale: string): Promise<void> {
    if (this.client === null || this.authStatus.kind !== 'authenticated') return;
    const ev = {
      thread,
      author: this.authStatus.pubkey,
      kind: (archived ? 'archive' : 'reopen') as 'archive' | 'reopen',
      created_at: new Date().toISOString(),
      parents: [],
      body: rationale,
      blobs: [],
      supersedes: null,
      receipt: null,
      branch_thread: null,
      id: null,
      sig: null,
    };
    await this.client.post(ev);
    await this.loadInbox();
    void this.loadThreads();
  }

  /** v0.4.27: post a kind='audience' entry. */
  async setThreadAudience(thread: string, pubkeys: string[]): Promise<void> {
    if (this.client === null || this.authStatus.kind !== 'authenticated') return;
    const ev = {
      thread,
      author: this.authStatus.pubkey,
      kind: 'audience' as const,
      created_at: new Date().toISOString(),
      parents: [],
      body: '',
      blobs: [],
      supersedes: null,
      receipt: null,
      branch_thread: null,
      audience: { pubkeys: [...pubkeys] },
      id: null,
      sig: null,
    };
    await this.client.post(ev);
    await this.loadInbox();
    void this.loadThreads();
  }

  /** v0.4.27: create a new thread with an audience + first message. */
  async createDirectThread(opts: {
    thread: string;
    pubkeys: string[];
    message: string;
  }): Promise<void> {
    if (this.client === null || this.authStatus.kind !== 'authenticated') return;
    const me = this.authStatus.pubkey;
    const audiencePubkeys = opts.pubkeys.includes(me)
      ? [...opts.pubkeys]
      : [me, ...opts.pubkeys];
    await this.setThreadAudience(opts.thread, audiencePubkeys);
    await this.switchThread(opts.thread);
    if (opts.message.trim()) {
      await this.post(opts.message);
    }
  }

  async post(body: string, files: File[] = [],
             replyTo: VerifiedEntry | null = null): Promise<void> {
    if (this.client === null || this.authStatus.kind !== 'authenticated') return;
    const blobs = files.length === 0
      ? []
      : await Promise.all(files.map((f) => this.client!.uploadBlob(f)));
    const ev = {
      thread: this.thread,
      author: this.authStatus.pubkey,
      kind: 'post' as const,
      created_at: new Date().toISOString(),
      parents: replyTo ? [replyTo.entry.id!] : [],
      body,
      blobs,
      supersedes: null,
      receipt: null,
      branch_thread: null,
      id: null,
      sig: null,
    };
    await this.client.post(ev);
    void this.loadThreads();
  }

  async loadThreads(): Promise<void> {
    if (this.client === null) return;
    try {
      this.threads = await this.client.fetchThreads();
    } catch (_err) {
      // Non-fatal — stale thread list is preferable to throwing.
    }
  }

  /** v0.4.19: pull the landing-view bundle from /inbox. */
  async loadInbox(): Promise<void> {
    if (this.client === null) return;
    this.inboxStatus = { kind: 'loading' };
    try {
      const rows = await this.client.fetchInbox();
      this.inboxRows = rows;
      for (const row of rows) {
        if (row.my_high_water >= 0) {
          this.myReceiptSeq.set(row.thread, row.my_high_water);
        }
      }
      this.inboxStatus = { kind: 'idle' };
    } catch (err) {
      this.inboxStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.19: return to the email-style landing view. */
  async goToInbox(): Promise<void> {
    // v0.4.53: keep the WS subscription open on Inbox so pushes still
    // refresh inboxRows / threads in the background.
    this.entries = [];
    this.seenIds = new Set();
    this.replyOpen = null;
    this.app.route = 'inbox';
    await this.loadInbox();
  }

  /** v0.4.19: post a kind='receipt' entry acking the latest non-receipt
   *  seq this session has loaded for the thread. */
  private async markThreadRead(thread: string): Promise<void> {
    if (this.client === null) return;
    let latestUserSeq = -1;
    for (const ve of this.entries) {
      if (ve.entry.kind === 'receipt') continue;
      if (ve.entry.thread !== thread) continue;
      if (ve.seq > latestUserSeq) latestUserSeq = ve.seq;
    }
    if (latestUserSeq < 0) return;
    const lastReceipted = this.myReceiptSeq.get(thread) ?? -1;
    if (latestUserSeq <= lastReceipted) return;
    try {
      const sth = this.client.latestSth() ?? await this.client.fetchSth();
      const receiptSeq = await this.client.postReceipt({
        thread, highWaterSeq: latestUserSeq, observedSth: sth,
      });
      this.myReceiptSeq.set(thread, receiptSeq);
    } catch {
      // Receipt-posting is best-effort.
    }
  }

  async switchThread(name: string): Promise<void> {
    if (this.client === null) return;
    // v0.4.19: also covers the inbox→thread transition.
    if (name === this.thread && this.app.route === 'thread') return;
    this.app.route = 'thread';
    this.thread = name;
    // v0.4.69: persist last-viewed thread per-hub. Previously used a
    // single global `cove.thread` key which collided when the client
    // talks to multiple hubs — switching to Hub B would silently
    // rehydrate Hub A's last-viewed thread name.
    saveThreadFor(this.hubUrl, name);
    this.entries = [];
    this.seenIds = new Set();
    // Pair with entries=[] so /sync?since=N replays from scratch.
    this.client.resetHighWater(name);
    // Close any open reply panel — its parent belongs to the old thread.
    this.replyOpen = null;
    // Reset the main pane to the chronological feed.
    this.view = 'messages';
    this.teardown?.();
    this.teardown = null;
    await this.syncAndSubscribe();
    // v0.4.19: fire-and-forget receipt.
    void this.markThreadRead(name);
  }

  // ---------------------------------------------------------------------
  // Admin (keymaster) ops
  // ---------------------------------------------------------------------

  /** v0.4.0: refresh pending-queue snapshot. */
  async loadPendingQueue(): Promise<void> {
    if (this.client === null) return;
    try {
      this.pendingQueue = await this.client.listPending();
    } catch {
      this.pendingQueue = [];
    }
  }

  /** Reject a pending registration. */
  async rejectPending(pubkey: string): Promise<void> {
    if (this.client === null) return;
    try { await this.client.clearPending(pubkey); } catch { /* tolerate */ }
    await this.loadPendingQueue();
  }

  /** Approve a pending registration: issue an Attestation root-signed via
   *  the keychain, chain a fresh DirectoryManifest, POST /admin/attest. */
  async approvePending(opts: {
    pubkey: string;
    displayName: string;
    affiliation: string;
    role: 'member' | 'officer' | 'board' | string;
    title?: string | null;
  }): Promise<void> {
    if (this.client === null) {
      this.adminStatus = { kind: 'error', message: 'Not connected.' };
      return;
    }
    if (!this.app.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: 'Root key not loaded. Import root.priv before approving.',
      };
      return;
    }
    this.adminStatus = { kind: 'submitting' };
    const signer: RootSigner = this.rootSigner();
    try {
      const current = await this.client.fetchDirectory();
      const rootPub = await signer.pubkey();
      if (rootPub !== current.org) {
        throw new Error(
          'Root key on this device does not match the hub org pubkey.',
        );
      }
      const newAtt = await issueAttestation(signer, {
        memberPubkey: opts.pubkey,
        displayName: opts.displayName,
        affiliation: opts.affiliation,
        role: opts.role,
        title: opts.title ?? null,
      });
      const newManifest = await issueDirectory(signer, {
        org: current.org,
        attestations: [...current.attestations, newAtt],
        revocations: [...current.revocations],
        prevManifestHash: hashManifest(current),
        defaultThread: current.default_thread,
        capabilitiesByRole: current.capabilities_by_role ?? null,
        groups: current.groups ?? null,
      });
      await this.client.submitAttestation(newManifest);
      this.adminStatus = { kind: 'idle' };
      await this.loadPendingQueue();
      await this.client.fetchDirectory();
      this.members = this.client.currentMembers();
      this.revoked = this.client.recentlyRevoked();
    } catch (err) {
      this.adminStatus = {
        kind: 'error', message: errMsg(err),
      };
    }
  }

  /** v0.4.71: attest an arbitrary pubkey. Same wire path as
   *  approvePending (issueAttestation → issueDirectory → submit) but
   *  the pubkey does NOT need to be in the pending queue. Used to
   *  federate an identity that already exists on another hub — the
   *  keymaster paste the pubkey (copied from the other device's
   *  identity chip) instead of waiting for an invite-code flow. */
  async attestPubkey(opts: {
    pubkey: string;
    displayName: string;
    affiliation: string;
    role: 'member' | 'officer' | 'board' | string;
    title?: string | null;
  }): Promise<void> {
    if (this.client === null) {
      this.adminStatus = { kind: 'error', message: 'Not connected.' };
      return;
    }
    if (!this.app.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: 'Root key not loaded. Import root.priv before attesting.',
      };
      return;
    }
    this.adminStatus = { kind: 'submitting' };
    const signer: RootSigner = this.rootSigner();
    try {
      const current = await this.client.fetchDirectory();
      const rootPub = await signer.pubkey();
      if (rootPub !== current.org) {
        throw new Error(
          'Root key on this device does not match the hub org pubkey.',
        );
      }
      const newAtt = await issueAttestation(signer, {
        memberPubkey: opts.pubkey,
        displayName: opts.displayName,
        affiliation: opts.affiliation,
        role: opts.role,
        title: opts.title ?? null,
      });
      const newManifest = await issueDirectory(signer, {
        org: current.org,
        attestations: [...current.attestations, newAtt],
        revocations: [...current.revocations],
        prevManifestHash: hashManifest(current),
        defaultThread: current.default_thread,
        capabilitiesByRole: current.capabilities_by_role ?? null,
        groups: current.groups ?? null,
      });
      await this.client.submitAttestation(newManifest);
      this.adminStatus = { kind: 'idle' };
      // Refresh local snapshots so the AdminPanel members list shows
      // the newly-attested pubkey without waiting for the WS push.
      await this.client.fetchDirectory();
      this.myAttestation = this.client.myAttestation();
      this.manifest = this.client.currentManifest();
      this.members = this.client.currentMembers();
      this.revoked = this.client.recentlyRevoked();
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.23: re-attest an existing member with updated fields. */
  async updateMember(opts: {
    pubkey: string;
    displayName: string;
    affiliation: string;
    role: 'member' | 'officer' | 'board' | string;
    title?: string | null;
  }): Promise<void> {
    if (this.client === null) {
      this.adminStatus = { kind: 'error', message: 'Not connected.' };
      return;
    }
    if (!this.app.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: 'Root key not loaded. Import root.priv before editing members.',
      };
      return;
    }
    this.adminStatus = { kind: 'submitting' };
    const signer: RootSigner = this.rootSigner();
    try {
      const current = await this.client.fetchDirectory();
      const rootPub = await signer.pubkey();
      if (rootPub !== current.org) {
        throw new Error(
          'Root key on this device does not match the hub org pubkey.',
        );
      }
      const newAtt = await issueAttestation(signer, {
        memberPubkey: opts.pubkey,
        displayName: opts.displayName,
        affiliation: opts.affiliation,
        role: opts.role,
        title: opts.title ?? null,
      });
      const newManifest = await issueDirectory(signer, {
        org: current.org,
        attestations: [...current.attestations, newAtt],
        revocations: [...current.revocations],
        prevManifestHash: hashManifest(current),
        defaultThread: current.default_thread,
        capabilitiesByRole: current.capabilities_by_role ?? null,
        groups: current.groups ?? null,
      });
      await this.client.submitAttestation(newManifest);
      this.adminStatus = { kind: 'idle' };
      await this.client.fetchDirectory();
      this.myAttestation = this.client.myAttestation();
      this.manifest = this.client.currentManifest();
      this.members = this.client.currentMembers();
      this.revoked = this.client.recentlyRevoked();
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.23: revoke a member's pubkey. */
  async revokeMember(opts: {
    pubkey: string;
    reason: string;
  }): Promise<void> {
    if (this.client === null) {
      this.adminStatus = { kind: 'error', message: 'Not connected.' };
      return;
    }
    if (!this.app.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: 'Root key not loaded. Import root.priv before revoking.',
      };
      return;
    }
    this.adminStatus = { kind: 'submitting' };
    const signer: RootSigner = this.rootSigner();
    try {
      const current = await this.client.fetchDirectory();
      const rootPub = await signer.pubkey();
      if (rootPub !== current.org) {
        throw new Error(
          'Root key on this device does not match the hub org pubkey.',
        );
      }
      const nowIso = new Date().toISOString();
      const revocation = {
        pubkey: opts.pubkey,
        revoked_at: nowIso,
        reason: opts.reason,
      };
      const newManifest = await issueDirectory(signer, {
        org: current.org,
        attestations: [...current.attestations],
        revocations: [...current.revocations, revocation],
        prevManifestHash: hashManifest(current),
        defaultThread: current.default_thread,
        capabilitiesByRole: current.capabilities_by_role ?? null,
        groups: current.groups ?? null,
      });
      await this.client.submitAttestation(newManifest);
      this.adminStatus = { kind: 'idle' };
      await this.client.fetchDirectory();
      this.myAttestation = this.client.myAttestation();
      this.manifest = this.client.currentManifest();
      this.members = this.client.currentMembers();
      this.revoked = this.client.recentlyRevoked();
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.25: re-issue the directory manifest with a new role → caps map. */
  async setCapabilitiesByRole(
    next: Record<string, string[]> | null,
  ): Promise<void> {
    if (this.client === null) {
      this.adminStatus = { kind: 'error', message: 'Not connected.' };
      return;
    }
    if (!this.app.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: 'Root key not loaded. Import root.priv before changing roles.',
      };
      return;
    }
    this.adminStatus = { kind: 'submitting' };
    const signer: RootSigner = this.rootSigner();
    try {
      const current = await this.client.fetchDirectory();
      const rootPub = await signer.pubkey();
      if (rootPub !== current.org) {
        throw new Error(
          'Root key on this device does not match the hub org pubkey.',
        );
      }
      const newManifest = await issueDirectory(signer, {
        org: current.org,
        attestations: [...current.attestations],
        revocations: [...current.revocations],
        prevManifestHash: hashManifest(current),
        defaultThread: current.default_thread,
        capabilitiesByRole: next,
        // v0.4.64: forward existing groups so a caps edit doesn't strip them.
        groups: current.groups ?? null,
      });
      await this.client.submitAttestation(newManifest);
      this.adminStatus = { kind: 'idle' };
      await this.client.fetchDirectory();
      this.manifest = this.client.currentManifest();
      this.myAttestation = this.client.myAttestation();
      this.members = this.client.currentMembers();
      this.revoked = this.client.recentlyRevoked();
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.64: re-issue the directory manifest with a new keypair groups
   *  list. */
  async saveGroups(next: KeypairGroup[] | null): Promise<void> {
    if (this.client === null) {
      this.adminStatus = { kind: 'error', message: 'Not connected.' };
      return;
    }
    if (!this.app.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: 'Root key not loaded. Import root.priv before editing groups.',
      };
      return;
    }
    this.adminStatus = { kind: 'submitting' };
    const signer: RootSigner = this.rootSigner();
    try {
      const current = await this.client.fetchDirectory();
      const rootPub = await signer.pubkey();
      if (rootPub !== current.org) {
        throw new Error(
          'Root key on this device does not match the hub org pubkey.',
        );
      }
      const newManifest = await issueDirectory(signer, {
        org: current.org,
        attestations: [...current.attestations],
        revocations: [...current.revocations],
        prevManifestHash: hashManifest(current),
        defaultThread: current.default_thread,
        capabilitiesByRole: current.capabilities_by_role ?? null,
        groups: next,
      });
      await this.client.submitAttestation(newManifest);
      this.adminStatus = { kind: 'idle' };
      await this.client.fetchDirectory();
      this.manifest = this.client.currentManifest();
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.24: set a per-identity throttle tier override. */
  async setMemberTier(opts: {
    pubkey: string;
    tier: 'member' | 'officer' | 'board';
  }): Promise<void> {
    if (this.client === null) {
      this.adminStatus = { kind: 'error', message: 'Not connected.' };
      return;
    }
    if (!this.app.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: 'Root key not loaded. Import root.priv before changing limits.',
      };
      return;
    }
    this.adminStatus = { kind: 'submitting' };
    try {
      const payload = { pubkey: opts.pubkey, tier: opts.tier };
      const sig = await this.signRootPayload(canonicalize(payload));
      await this.client.submitTierOverride({ payload, sig });
      this.adminStatus = { kind: 'idle' };
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  // ---------------------------------------------------------------------
  // Invites
  // ---------------------------------------------------------------------

  async loadInvites(): Promise<void> {
    if (this.client === null) return;
    this.invitesStatus = { kind: 'loading' };
    try {
      this.invites = await this.client.fetchInvites();
      this.invitesStatus = { kind: 'idle' };
    } catch (err) {
      this.invitesStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  async mintInvite(opts: {
    ttlSeconds: number;
    nameHint?: string;
  }): Promise<Invite | null> {
    if (this.client === null || !this.app.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: this.client
          ? 'Root key not loaded.'
          : 'Not connected.',
      };
      return null;
    }
    this.adminStatus = { kind: 'submitting' };
    try {
      const payload: { ttl_seconds: number; name_hint?: string } = {
        ttl_seconds: opts.ttlSeconds,
      };
      if (opts.nameHint && opts.nameHint.trim()) {
        payload.name_hint = opts.nameHint.trim();
      }
      const sig = await this.signRootPayload(canonicalize(payload));
      const inv = await this.client.submitInviteMint({ payload, sig });
      this.adminStatus = { kind: 'idle' };
      this.invites = [...this.invites, inv];
      return inv;
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
      return null;
    }
  }

  async revokeInvite(code: string): Promise<void> {
    if (this.client === null || !this.app.rootKeysPresent) return;
    this.adminStatus = { kind: 'submitting' };
    try {
      const payload = { code };
      const sig = await this.signRootPayload(canonicalize(payload));
      await this.client.submitInviteRevoke({ code, payload, sig });
      this.adminStatus = { kind: 'idle' };
      this.invites = this.invites.filter((i) => i.code !== code);
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.13: keymaster sets (or clears) the org's default landing thread. */
  async setDefaultThread(newDefault: string | null): Promise<string | null> {
    if (this.client === null) {
      this.adminStatus = { kind: 'error', message: 'Not connected.' };
      throw new Error('not connected');
    }
    if (!this.app.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: 'Root key not loaded. Import root.priv before changing org settings.',
      };
      throw new Error('root key not loaded');
    }
    this.adminStatus = { kind: 'submitting' };
    const signer: RootSigner = this.rootSigner();
    try {
      const current = await this.client.fetchDirectory();
      const rootPub = await signer.pubkey();
      if (rootPub !== current.org) {
        throw new Error(
          'Root key on this device does not match the hub org pubkey.',
        );
      }
      const newManifest = await issueDirectory(signer, {
        org: current.org,
        attestations: [...current.attestations],
        revocations: [...current.revocations],
        prevManifestHash: hashManifest(current),
        defaultThread: newDefault ?? undefined,
        capabilitiesByRole: current.capabilities_by_role ?? null,
        groups: current.groups ?? null,
      });
      await this.client.submitAttestation(newManifest);
      this.adminStatus = { kind: 'idle' };
      return newDefault;
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
      throw err;
    }
  }

  /** v0.4.25: capability check based on manifest.capabilities_by_role. */
  hasCapability(cap: string): boolean {
    const role = this.myAttestation?.role;
    if (!role) return false;
    const map = this.manifest?.capabilities_by_role ?? DEFAULT_CAPABILITIES_BY_ROLE;
    return (map[role] ?? []).includes(cap);
  }

  /** Back-compat: pre-v0.4.25 callers used isBoardMember to gate the
   *  AdminPanel. Semantics are now "has the 'admin' capability." */
  get isBoardMember(): boolean {
    return this.hasCapability('admin');
  }

  // ---------------------------------------------------------------------
  // New-thread dialog
  // ---------------------------------------------------------------------

  openNewThreadDialog(): void {
    this.newThreadDialog = {
      name: '',
      scope: 'public',
      selected: new Set<string>(),
      message: '',
      submitting: false,
      error: null,
      ephemeral: false,
      ttlDays: 30,
    };
  }

  closeNewThreadDialog(): void {
    this.newThreadDialog = null;
  }

  toggleNewThreadMember(pubkey: string): void {
    if (!this.newThreadDialog) return;
    const next = new Set(this.newThreadDialog.selected);
    if (next.has(pubkey)) next.delete(pubkey);
    else next.add(pubkey);
    this.newThreadDialog = { ...this.newThreadDialog, selected: next };
  }

  /** v0.4.64: bulk-add pubkeys from a group to the new-thread audience. */
  addGroupToNewThread(pubkeys: readonly string[]): void {
    if (!this.newThreadDialog) return;
    const attested = new Set(this.members.map((m) => m.member_pubkey));
    const next = new Set(this.newThreadDialog.selected);
    for (const pk of pubkeys) if (attested.has(pk)) next.add(pk);
    this.newThreadDialog = { ...this.newThreadDialog, selected: next };
  }

  /** Run the new-thread submit through createDirectThread (private) or
   *  switchThread+post (public). */
  async submitNewThread(): Promise<void> {
    if (!this.newThreadDialog) return;
    const sanitized = this.newThreadDialog.name.trim().toLowerCase()
      .replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '');
    if (!sanitized) {
      this.newThreadDialog = {
        ...this.newThreadDialog, error: 'Thread name is required.',
      };
      return;
    }
    this.newThreadDialog = {
      ...this.newThreadDialog, submitting: true, error: null,
    };
    try {
      const d = this.newThreadDialog;
      if (d.ephemeral) {
        if (this.client === null || this.authStatus.kind !== 'authenticated') {
          throw new Error('not connected');
        }
        const ttlSeconds = Math.max(1, Math.round(d.ttlDays)) * 86400;
        await this.client.openEphemeralThread({
          thread: sanitized, ttlSeconds,
        });
        if (d.scope === 'private') {
          const me = this.authStatus.pubkey;
          const pubkeys = d.selected.has(me)
            ? Array.from(d.selected)
            : [me, ...Array.from(d.selected)];
          await this.setThreadAudience(sanitized, pubkeys);
        }
        await this.switchThread(sanitized);
        if (d.message.trim()) await this.post(d.message);
      } else if (d.scope === 'private') {
        await this.createDirectThread({
          thread: sanitized,
          pubkeys: Array.from(d.selected),
          message: d.message,
        });
      } else {
        await this.switchThread(sanitized);
        if (d.message.trim()) await this.post(d.message);
      }
      this.newThreadDialog = null;
    } catch (err) {
      this.newThreadDialog = this.newThreadDialog
        ? {
            ...this.newThreadDialog,
            submitting: false,
            error: errMsg(err),
          }
        : null;
    }
  }

  // ---------------------------------------------------------------------
  // Predicates
  // ---------------------------------------------------------------------

  /** v0.4.25: is a named thread currently archived? */
  isThreadArchived(name: string): boolean {
    const inboxRow = this.inboxRows.find((r) => r.thread === name);
    if (inboxRow) return inboxRow.archived;
    const threadRow = this.threads.find((t) => t.thread === name);
    if (threadRow) return threadRow.archived;
    return false;
  }

  /** v0.4.27: current audience scope for a thread, or null if public.
   *  The hub only surfaces audience-scoped threads to members, so a
   *  non-null result here also means "I'm in the audience." */
  threadAudience(name: string): { pubkeys: string[] } | null {
    const inboxRow = this.inboxRows.find((r) => r.thread === name);
    if (inboxRow) return inboxRow.audience;
    const threadRow = this.threads.find((t) => t.thread === name);
    if (threadRow) return threadRow.audience;
    return null;
  }
}
