/**
 * Reactive app state — Svelte 5 runes wrapped in a small class.
 *
 * The Client class itself is not reactive; this wrapper is. UI reads
 * `state.entries`, `state.client`, etc. and re-renders when they change.
 * One mutation point per concern keeps it analyzable.
 */
import { Client, TauriKeychainSigner, type VerifiedEntry } from './client';
import {
  ensureNotificationPermission, isTauri, keychain, stream, updater,
  type AvailableUpdate,
} from './tauri';
import type { ThreadSummary } from './types';

type AuthStatus =
  | { kind: 'unauthenticated' }
  | { kind: 'connecting' }
  | { kind: 'authenticated'; pubkey: string }
  | { kind: 'failed'; reason: string };

type ThreadStatus =
  | { kind: 'idle' }
  | { kind: 'syncing' }
  | { kind: 'error'; message: string };

/** v0.1.10: main pane has two faces per thread — chronological feed
 *  or per-thread files list. Reset to 'messages' on every thread
 *  switch so navigation doesn't trap the user in Files. */
type View = 'messages' | 'files';

type UpdateStatus =
  | { kind: 'idle' }
  | { kind: 'checking' }
  | { kind: 'available'; update: AvailableUpdate }
  | { kind: 'installing'; downloaded: number; total: number | null }
  | { kind: 'error'; message: string };

export class AppState {
  authStatus = $state<AuthStatus>({ kind: 'unauthenticated' });
  thread = $state<string>('annual-meeting');
  threadStatus = $state<ThreadStatus>({ kind: 'idle' });
  entries = $state<VerifiedEntry[]>([]);
  /** All observed threads on the hub. Populated by loadThreads();
   *  refreshed after post and on subscribe push. Used by ThreadList. */
  threads = $state<ThreadSummary[]>([]);
  /** True iff running inside the Tauri shell — drives the keychain
   *  vs paste-box branch in the auth panel. */
  inTauri = $state<boolean>(isTauri());
  /** Public key stored in the OS keychain (Tauri only). When set,
   *  AuthPanel shows 'Unlock' rather than the import form. */
  storedPublicKey = $state<string | null>(null);
  /** Updater status — drives the quiet 'Update available' affordance.
   *  Set by checkForUpdate(); resolution by installUpdate(). */
  updateStatus = $state<UpdateStatus>({ kind: 'idle' });
  /** When non-null, the reply panel is open and pinned to this entry.
   *  Set by openReplyPanel() from the EntryCard reply button; cleared
   *  by closeReplyPanel(), switchThread(), and reset(). */
  replyOpen = $state<VerifiedEntry | null>(null);
  /** Which face of the active thread to render — chronological feed
   *  or files list. Reset to 'messages' on every switchThread. */
  view = $state<View>('messages');
  /** Track ids we've already shown so we never double-render after dedup. */
  private seenIds = new Set<string>();

  client: Client | null = null;
  private teardown: (() => void) | null = null;

  /** Re-read the keychain status. Call on app load so AuthPanel picks
   *  the right branch. No-op outside Tauri. */
  async refreshKeychain(): Promise<void> {
    if (!this.inTauri) return;
    const st = await keychain.status();
    this.storedPublicKey = st.has_keys ? st.public_key : null;
  }

  /** Import a paired (priv, pub) into the OS keychain. Slice 3 — only
   *  in Tauri. The private key goes to Rust and never comes back.
   *
   *  After import we verify the keychain actually has the entry by
   *  reading it back via refreshKeychain. If it doesn't (suspected
   *  unsigned-macOS-app silent-no-op), throw loud — DO NOT leave the
   *  caller thinking import succeeded when it didn't. Catches both
   *  the unsigned-app pattern and any other case where the OS reports
   *  store-OK but the value isn't there. */
  async importKeysToKeychain(privateKey: string, publicKey: string): Promise<void> {
    if (!this.inTauri) throw new Error('keychain custody requires the Tauri shell');
    await keychain.import(privateKey, publicKey);
    await this.refreshKeychain();
    if (this.storedPublicKey !== publicKey) {
      throw new Error(
        'Keychain import did not persist. The OS keychain reports no '
        + 'entry was stored even though the import call returned OK. '
        + 'On unsigned macOS builds this is a known symptom of the '
        + 'keychain refusing to trust an app without a stable code '
        + 'identity. See terminal stderr / Console.app for details.',
      );
    }
  }

  /** Wipe the keychain. Used for 'switch identity' / 'this device left
   *  the org' cleanup. */
  async clearKeychain(): Promise<void> {
    if (!this.inTauri) return;
    await keychain.clear();
    await this.refreshKeychain();
  }

  /** Slide the reply panel open, pinned to the given entry. The panel
   *  shows that entry + all entries whose parents include its id, plus
   *  a ComposeBox configured to post as a reply. */
  openReplyPanel(ve: VerifiedEntry): void {
    this.replyOpen = ve;
  }

  closeReplyPanel(): void {
    this.replyOpen = null;
  }

  setView(v: View): void {
    this.view = v;
  }

  /**
   * Quietly check the updater feed. Call once on app load; the feed
   * URL + pubkey live in tauri.conf.json. A signature-verification
   * failure is surfaced as an error — the rest of the chain (no
   * network, no update available) is silent so the UI doesn't shout
   * about routine outcomes.
   */
  async checkForUpdate(): Promise<void> {
    if (!this.inTauri) return;
    this.updateStatus = { kind: 'checking' };
    try {
      const available = await updater.check();
      this.updateStatus = available === null
        ? { kind: 'idle' }
        : { kind: 'available', update: available };
    } catch (err) {
      this.updateStatus = { kind: 'error', message: (err as Error).message };
    }
  }

  /**
   * Install the available update and restart. Only callable when
   * updateStatus.kind === 'available'. The plugin verifies the
   * downloaded bundle against the Tauri-signer pubkey BEFORE install
   * — a tampered or unsigned bundle is refused, lands here as an
   * error.
   */
  async installUpdate(): Promise<void> {
    if (!this.inTauri) return;
    if (this.updateStatus.kind !== 'available') return;
    this.updateStatus = { kind: 'installing', downloaded: 0, total: null };
    try {
      await updater.downloadAndInstallAndRestart((downloaded, total) => {
        this.updateStatus = { kind: 'installing', downloaded, total };
      });
      // Process restarts before we reach here in practice; leave the
      // status as installing so any frame painted between download
      // and restart shows the progress, not 'idle'.
    } catch (err) {
      this.updateStatus = { kind: 'error', message: (err as Error).message };
    }
  }

  /** Reset all per-session state. Used on disconnect / re-auth. */
  reset() {
    this.entries = [];
    this.seenIds = new Set();
    this.threadStatus = { kind: 'idle' };
    this.replyOpen = null;
    this.teardown?.();
    this.teardown = null;
    this.client = null;
    this.authStatus = { kind: 'unauthenticated' };
  }

  /**
   * Connect to a hub. Two paths:
   *
   *   - browser / paste mode: caller provides privateKey, wrapped as
   *     InJSSigner inside Client. Slice-2 behaviour.
   *   - Tauri / keychain mode: the private key is already in the OS
   *     keychain; caller passes mode='keychain' and the publicKey
   *     (from storedPublicKey). Signing roundtrips through Rust.
   */
  async connect(opts: {
    hubUrl: string;
    publicKey: string;
    thread: string;
    privateKey?: string;
    mode?: 'paste' | 'keychain';
  }): Promise<void> {
    this.authStatus = { kind: 'connecting' };
    try {
      this.thread = opts.thread;
      this.hubUrl = opts.hubUrl;
      this.client = new Client({
        hubUrl: opts.hubUrl,
        publicKey: opts.publicKey,
        signer: opts.mode === 'keychain' ? new TauriKeychainSigner() : undefined,
        privateKey: opts.mode === 'keychain' ? undefined : opts.privateKey,
      });
      const sessionToken = await this.client.authenticate();
      this.sessionToken = sessionToken;
      this.authStatus = { kind: 'authenticated', pubkey: opts.publicKey };
      // Notifications: ask once, here — the user just authenticated, so
      // the OS prompt arrives WITH context (they consciously connected).
      if (this.inTauri) {
        void ensureNotificationPermission();
      }
      await this.syncAndSubscribe();
      // Load the thread list once we're connected. Non-blocking so a
      // hub that's slow to respond on /threads doesn't gate the feed.
      void this.loadThreads();
    } catch (err) {
      this.authStatus = { kind: 'failed', reason: (err as Error).message };
      this.client = null;
    }
  }

  /** Captured at connect() time so subscribe can hand them to Rust. */
  private hubUrl = '';
  private sessionToken = '';

  /**
   * client-spec §4.1: subscribe FIRST, then sync. Anything that lands in
   * the window between subscribe and sync arrives on both channels and is
   * deduped via seenIds.
   *
   * In the Tauri shell the subscription runs in the Rust process so
   * notifications keep firing when the webview is closed. In a browser
   * we fall back to the Client's in-tab WebSocket. Either way the
   * verification path (Client.verify) is the same — we never render an
   * entry that hasn't passed §5.
   */
  async syncAndSubscribe(): Promise<void> {
    if (this.client === null) return;
    const client = this.client;
    this.threadStatus = { kind: 'syncing' };
    try {
      // 1. Subscribe FIRST.
      if (this.inTauri) {
        const teardown = await stream.start(
          { hubUrl: this.hubUrl, token: this.sessionToken, thread: this.thread },
          (raw) => { void this.handlePushedRaw(raw); },
        );
        this.teardown = () => { void teardown(); };
      } else {
        this.teardown = client.subscribe(
          this.thread,
          (ve) => this.appendIfNew(ve),
          (err) => {
            this.threadStatus = { kind: 'error', message: `stream: ${err.message}` };
          },
        );
      }
      // 2. Then sync from last-known seq.
      const initial = await client.sync(this.thread);
      for (const ve of initial) this.appendIfNew(ve);
      this.threadStatus = { kind: 'idle' };
    } catch (err) {
      this.threadStatus = { kind: 'error', message: (err as Error).message };
    }
  }

  /**
   * Tauri-only: a raw push from the Rust subscriber arrived. Rust does
   * NOT verify — it relays. We run the full §5 chain via client.verify
   * before showing the entry, so the trust posture lives in one place.
   */
  private async handlePushedRaw(raw: string): Promise<void> {
    if (this.client === null) return;
    try {
      const msg = JSON.parse(raw);
      if (msg.type !== 'entry') return;
      const ve = await this.client.verify(msg.entry, msg.seq);
      if (ve.entry.thread !== this.thread) return;
      this.appendIfNew(ve);
    } catch (err) {
      // VerificationError lands here — DO NOT render. A failed verify on
      // a pushed entry is exactly the case the spec calls for refusing.
      this.threadStatus = { kind: 'error', message: (err as Error).message };
    }
  }

  private appendIfNew(ve: VerifiedEntry) {
    if (!ve.entry.id || this.seenIds.has(ve.entry.id)) return;
    this.seenIds.add(ve.entry.id);
    this.entries = [...this.entries, ve].sort((a, b) => a.seq - b.seq);
  }

  async post(body: string, files: File[] = [],
             replyTo: VerifiedEntry | null = null): Promise<void> {
    if (this.client === null || this.authStatus.kind !== 'authenticated') return;
    // client-spec §3: upload blobs FIRST. The acceptance pipeline strict-
    // checks that referenced blobs exist on the hub, so a failed upload
    // must abort the post — we don't ship an entry that references
    // missing bytes.
    const blobs = files.length === 0
      ? []
      : await Promise.all(files.map((f) => this.client!.uploadBlob(f)));
    // Replies set parents = [parent.id]; top-level entries have parents=[].
    // The hub validates parents exist (§7.1) but doesn't enforce that
    // replies stay within the same thread — that's a client convention.
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
      id: null,
      sig: null,
    };
    await this.client.post(ev);
    // Refresh the thread list so the latest_seq + entry_count update
    // optimistically reflect the post we just made. The /stream
    // subscription will push the entry back; that's where the
    // ceremony render happens. No optimistic insert — the ceremony is
    // 'verified, with proof,' not 'sent.'
    void this.loadThreads();
  }

  /** Refresh the thread list from the hub. Called on connect and after
   *  every post; can also be called from the UI as a manual refresh. */
  async loadThreads(): Promise<void> {
    if (this.client === null) return;
    try {
      this.threads = await this.client.fetchThreads();
    } catch (_err) {
      // Non-fatal — thread list staying stale is preferable to throwing
      // the connection away. The next refresh will heal it.
    }
  }

  /** Switch the active thread. Resets per-thread state, re-syncs, and
   *  re-subscribes. /stream is a global broadcast, so the actual
   *  WebSocket is torn down and re-opened to pick up the new
   *  thread-filter closure (per Client.subscribe semantics).
   *
   *  Threads are open-namespace — calling switchThread with a name no
   *  one has posted to yet just gives you an empty feed, and posting
   *  there will materialize the thread on the hub. That's by design. */
  async switchThread(name: string): Promise<void> {
    if (this.client === null) return;
    if (name === this.thread) return;
    this.thread = name;
    this.entries = [];
    this.seenIds = new Set();
    // Close any open reply panel — its parent belongs to the old thread.
    this.replyOpen = null;
    // Reset the main pane to the chronological feed — landing in 'files'
    // because the previous thread was on it would be disorienting.
    this.view = 'messages';
    this.teardown?.();
    this.teardown = null;
    await this.syncAndSubscribe();
  }
}
