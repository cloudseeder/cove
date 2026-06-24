/**
 * Reactive app state — Svelte 5 runes wrapped in a small class.
 *
 * The Client class itself is not reactive; this wrapper is. UI reads
 * `state.entries`, `state.client`, etc. and re-renders when they change.
 * One mutation point per concern keeps it analyzable.
 */
import { Client, type VerifiedEntry } from './client';

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
  /** Track ids we've already shown so we never double-render after dedup. */
  private seenIds = new Set<string>();

  client: Client | null = null;
  private teardown: (() => void) | null = null;

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

  async connect(opts: {
    hubUrl: string;
    privateKey: string;
    publicKey: string;
    thread: string;
  }): Promise<void> {
    this.authStatus = { kind: 'connecting' };
    try {
      this.thread = opts.thread;
      this.client = new Client({
        hubUrl: opts.hubUrl,
        privateKey: opts.privateKey,
        publicKey: opts.publicKey,
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
