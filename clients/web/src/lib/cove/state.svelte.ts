/**
 * Reactive app state — Svelte 5 runes wrapped in a small class.
 *
 * The Client class itself is not reactive; this wrapper is. UI reads
 * `state.entries`, `state.client`, etc. and re-renders when they change.
 * One mutation point per concern keeps it analyzable.
 */
import { Client, TauriKeychainSigner, type VerifiedEntry } from './client';
import { isTauri, keychain } from './tauri';

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
      this.client = new Client({
        hubUrl: opts.hubUrl,
        publicKey: opts.publicKey,
        signer: opts.mode === 'keychain' ? new TauriKeychainSigner() : undefined,
        privateKey: opts.mode === 'keychain' ? undefined : opts.privateKey,
      });
      await this.client.authenticate();
      this.authStatus = { kind: 'authenticated', pubkey: opts.publicKey };
      await this.syncAndSubscribe();
    } catch (err) {
      this.authStatus = { kind: 'failed', reason: (err as Error).message };
      this.client = null;
    }
  }

  /**
   * client-spec §4.1: subscribe FIRST, then sync. Anything that lands in
   * the window between subscribe and sync arrives on both channels and is
   * deduped via seenIds.
   */
  async syncAndSubscribe(): Promise<void> {
    if (this.client === null) return;
    this.threadStatus = { kind: 'syncing' };
    try {
      // 1. Subscribe FIRST — register on the fan-out before asking for
      // catch-up so we don't lose entries that land in the gap.
      this.teardown = this.client.subscribe(
        this.thread,
        (ve) => this.appendIfNew(ve),
        (err) => {
          this.threadStatus = { kind: 'error', message: `stream: ${err.message}` };
        },
      );
      // 2. Then sync from last-known seq to catch up.
      const initial = await this.client.sync(this.thread);
      for (const ve of initial) this.appendIfNew(ve);
      this.threadStatus = { kind: 'idle' };
    } catch (err) {
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
