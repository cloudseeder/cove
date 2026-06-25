/**
 * Reactive app state — Svelte 5 runes wrapped in a small class.
 *
 * The Client class itself is not reactive; this wrapper is. UI reads
 * `state.entries`, `state.client`, etc. and re-renders when they change.
 * One mutation point per concern keeps it analyzable.
 */
import { Client, TauriKeychainSigner, type VerifiedEntry } from './client';
import {
  ensureNotificationPermission, isTauri, keychain, stream,
} from './tauri';

type AuthStatus =
  | { kind: 'unauthenticated' }
  | { kind: 'connecting' }
  | { kind: 'authenticated'; pubkey: string }
  | { kind: 'failed'; reason: string };

type ThreadStatus =
  | { kind: 'idle' }
  | { kind: 'syncing' }
  | { kind: 'error'; message: string };

export class AppState {
  authStatus = $state<AuthStatus>({ kind: 'unauthenticated' });
  thread = $state<string>('annual-meeting');
  threadStatus = $state<ThreadStatus>({ kind: 'idle' });
  entries = $state<VerifiedEntry[]>([]);
  /** True iff running inside the Tauri shell — drives the keychain
   *  vs paste-box branch in the auth panel. */
  inTauri = $state<boolean>(isTauri());
  /** Public key stored in the OS keychain (Tauri only). When set,
   *  AuthPanel shows 'Unlock' rather than the import form. */
  storedPublicKey = $state<string | null>(null);
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
   *  in Tauri. The private key goes to Rust and never comes back. */
  async importKeysToKeychain(privateKey: string, publicKey: string): Promise<void> {
    if (!this.inTauri) throw new Error('keychain custody requires the Tauri shell');
    await keychain.import(privateKey, publicKey);
    await this.refreshKeychain();
  }

  /** Wipe the keychain. Used for 'switch identity' / 'this device left
   *  the org' cleanup. */
  async clearKeychain(): Promise<void> {
    if (!this.inTauri) return;
    await keychain.clear();
    await this.refreshKeychain();
  }

  /** Reset all per-session state. Used on disconnect / re-auth. */
  reset() {
    this.entries = [];
    this.seenIds = new Set();
    this.threadStatus = { kind: 'idle' };
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

  async post(body: string): Promise<void> {
    if (this.client === null || this.authStatus.kind !== 'authenticated') return;
    const ev = {
      thread: this.thread,
      author: this.authStatus.pubkey,
      kind: 'post' as const,
      created_at: new Date().toISOString(),
      parents: [],
      body,
      blobs: [],
      supersedes: null,
      receipt: null,
      id: null,
      sig: null,
    };
    await this.client.post(ev);
    // The /stream subscription will push the entry back; that's where the
    // ceremony render happens. No optimistic insert — the ceremony is
    // 'verified, with proof,' not 'sent.'
  }
}
