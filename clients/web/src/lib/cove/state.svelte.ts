/**
 * Reactive app state — Svelte 5 runes wrapped in a small class.
 *
 * The Client class itself is not reactive; this wrapper is. UI reads
 * `state.entries`, `state.client`, etc. and re-renders when they change.
 * One mutation point per concern keeps it analyzable.
 */
import { Client, TauriKeychainSigner, type VerifiedEntry } from './client';
import { issueAttestation, issueDirectory, type RootSigner } from './identity';
import { encodePairingLink, fingerprint as fingerprintOf } from './pairing';
import {
  appVersion,
  ensureNotificationPermission, isTauri, keychain, rootKeychain, stream, updater,
  type AvailableUpdate,
} from './tauri';
import type { Attestation, InboxRow, ThreadSummary } from './types';
import { hashManifest } from './verify';

// Tauri's invoke() rejects with a raw string when the Rust side returns
// Err(String) (which all our #[tauri::command] handlers do). Casting that
// to Error and reading .message yields undefined — the v0.4.2–v0.4.6
// "Key generation failed: undefined" symptom. errMsg handles all three
// shapes: Tauri string rejection, JS Error object, anything else.
function errMsg(e: unknown): string {
  if (typeof e === 'string') return e;
  if (e instanceof Error) return e.message;
  return String(e);
}

type AuthStatus =
  | { kind: 'unauthenticated' }
  | { kind: 'connecting' }
  | { kind: 'authenticated'; pubkey: string }
  | { kind: 'failed'; reason: string };

type ThreadStatus =
  | { kind: 'idle' }
  | { kind: 'syncing' }
  | { kind: 'error'; message: string };

/** v0.4.0 onboarding state machine — drives OnboardingPanel.svelte.
 *
 *   idle      → user hasn't started yet
 *   generating → calling keys_generate / hashing
 *   waiting   → keys live in keychain, pending registered, WS open
 *   attested  → push received; transitioning to authenticated flow
 *   error     → any step blew up; show the message and offer retry */
type OnboardStatus =
  | { kind: 'idle' }
  | { kind: 'generating' }
  | { kind: 'waiting'; pubkey: string; pairingLink: string; fingerprint: string }
  | { kind: 'attested'; pubkey: string }
  | { kind: 'error'; message: string };

/** v0.1.10: main pane faces per thread — chronological feed or
 *  per-thread files list. Reset to 'messages' on every thread switch
 *  so navigation doesn't trap the user in Files.
 *
 *  v0.4.0: 'admin' — global view (not per-thread) for the keymaster's
 *  pending-queue UI. Visible only to board-role members. Setting this
 *  doesn't change app.thread; switching threads resets back to 'messages'. */
type View = 'messages' | 'files' | 'admin';

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
  /** v0.4.19: top-level navigation. After Unlock the user lands on the
   *  email-style InboxPanel; clicking a row switches to 'thread'. The
   *  sidebar's "Inbox" link returns. */
  route = $state<'inbox' | 'thread'>('inbox');
  /** v0.4.19: rows powering InboxPanel. Loaded by loadInbox(); refreshed
   *  on directory_changed and on goToInbox(). */
  inboxRows = $state<InboxRow[]>([]);
  inboxStatus = $state<{ kind: 'idle' } | { kind: 'loading' }
    | { kind: 'error'; message: string }>({ kind: 'idle' });
  /** v0.4.19: per-thread seq of the last receipt this session has posted
   *  (or pulled in from /inbox). markThreadRead consults this before
   *  posting a fresh one so we don't loop on re-entering a thread. */
  private myReceiptSeq: Map<string, number> = new Map();
  /** All observed threads on the hub. Populated by loadThreads();
   *  refreshed after post and on subscribe push. Used by ThreadList. */
  threads = $state<ThreadSummary[]>([]);
  /** True iff running inside the Tauri shell — drives the keychain
   *  vs paste-box branch in the auth panel. */
  inTauri = $state<boolean>(isTauri());
  /** v0.4.16: bundle version exposed in the UI so users can read off
   *  which build they're on without digging into the OS-level About
   *  dialog. Populated asynchronously after construct because
   *  getVersion is a Tauri IPC call; null while it's still resolving
   *  AND in browser-only mode (no IPC at all). */
  appVersion = $state<string | null>(null);
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
  /** v0.4.11: chronological-feed visual mode. 'cards' = EntryCard
   *  layout, per-entry verification seal, audit feel. 'chat' =
   *  ChatMessage layout, grouped by author, ambient verification
   *  (thread-header indicator only), messaging feel. Persisted to
   *  localStorage. Default 'cards' so existing installs see no
   *  behavior change until they flip the toggle. */
  viewMode = $state<'cards' | 'chat'>(
    typeof localStorage !== 'undefined'
      && localStorage.getItem('cove.viewMode') === 'chat' ? 'chat' : 'cards',
  );
  /** v0.4.0: state of the on-device-keygen onboarding flow. The
   *  OnboardingPanel reads this directly; AuthPanel uses kind !== 'idle'
   *  to swap itself out for the onboarding view. */
  onboardStatus = $state<OnboardStatus>({ kind: 'idle' });
  /** Cancel handle for the WS /pending/watch — calling it tears the
   *  socket down without rejecting (used when the user clicks "back"
   *  from the waiting screen). */
  private watchCancel: (() => void) | null = null;
  /** v0.4.0: keymaster mode. True when the second keychain slot
   *  (ROOT_PRIV_SLOT) has a root key — gates the in-app admin UI. */
  rootKeysPresent = $state<boolean>(false);

  constructor() {
    // Resolve the bundle version asynchronously. The webview can render
    // before this lands — the version line is just blank until then.
    appVersion().then((v) => { this.appVersion = v; });
  }
  /** v0.4.0: cached pending queue for AdminPanel. Refreshed by
   *  loadPendingQueue() — also re-fetched after every approve/reject. */
  pendingQueue = $state<Array<{
    pubkey: string; name_hint: string; requested_at: string;
  }>>([]);
  /** v0.4.0: status of an in-flight approve action — drives the
   *  spinner + error display in the admin form. */
  adminStatus = $state<{ kind: 'idle' } | { kind: 'submitting' }
    | { kind: 'error'; message: string }>({ kind: 'idle' });
  /** v0.4.0: caller's own attestation, resolved at connect-time.
   *  Drives AdminPanel visibility (role==='board'). Null until
   *  fetchDirectory has run, which happens during connect(). */
  myAttestation = $state<Attestation | null>(null);
  /** v0.4.23: snapshot of currently-attested, non-revoked members
   *  for the AdminPanel membership editor. Refreshed alongside
   *  myAttestation on directory fetches and after admin mutations. */
  members = $state<Attestation[]>([]);
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
    // v0.4.19: setView is the gesture used to enter Admin / Files from
    // anywhere — it implies "leave Inbox if I'm on it." Without this,
    // clicking Admin from Inbox would update view but route would stay
    // 'inbox' and InboxPanel would keep rendering.
    if (this.route !== 'thread') this.route = 'thread';
  }

  // ---- v0.4.0: admin (keymaster) flow ----------------------------------

  /** Re-read the root keychain slot. Call when AdminPanel mounts. */
  async refreshRootKeychain(): Promise<void> {
    if (!this.inTauri) { this.rootKeysPresent = false; return; }
    const st = await rootKeychain.status();
    this.rootKeysPresent = st.has_keys;
  }

  /** Import the org root keypair into the dedicated keychain slot.
   *  One-time setup for the keymaster station. */
  async importRootKeys(privateKey: string, publicKey: string): Promise<void> {
    if (!this.inTauri) throw new Error('root key custody requires the Tauri shell');
    await rootKeychain.import(privateKey, publicKey);
    await this.refreshRootKeychain();
    if (!this.rootKeysPresent) {
      throw new Error(
        'Root key import did not persist. The OS keychain returned OK '
        + 'but a subsequent read returned no entry. Check Console.app '
        + '(macOS) or the keyring logs for details.',
      );
    }
  }

  /** Wipe the root slot. */
  async clearRootKeys(): Promise<void> {
    if (!this.inTauri) return;
    await rootKeychain.clear();
    await this.refreshRootKeychain();
  }

  /** Refresh the pending-queue snapshot. Board-auth required; if the
   *  caller isn't board-tier the hub returns 403 and we surface an
   *  empty queue so the UI just shows "nothing pending." */
  async loadPendingQueue(): Promise<void> {
    if (this.client === null) return;
    try {
      this.pendingQueue = await this.client.listPending();
    } catch {
      this.pendingQueue = [];
    }
  }

  /** Reject a pending registration (typo, suspected impostor, dup).
   *  Idempotent on the hub; we still refresh after for the UI. */
  async rejectPending(pubkey: string): Promise<void> {
    if (this.client === null) return;
    try { await this.client.clearPending(pubkey); } catch { /* tolerate */ }
    await this.loadPendingQueue();
  }

  /** Approve a pending registration: issue an Attestation root-signed
   *  via the keychain, build a fresh DirectoryManifest chained off the
   *  current head, POST to /admin/attest. The hub's attest hook fires
   *  the WS /pending/watch for this pubkey, so the member's device
   *  unlocks within the same tick. */
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
    if (!this.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: 'Root key not loaded. Import root.priv before approving.',
      };
      return;
    }
    this.adminStatus = { kind: 'submitting' };
    const signer: RootSigner = {
      sign: (m) => rootKeychain.signMessage(m),
      pubkey: async () => (await rootKeychain.status()).public_key!,
    };
    try {
      const current = await this.client.fetchDirectory();
      // Sanity: the root key on this device must derive to the
      // hub's org pubkey, otherwise the sig fails the hub's check.
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
        // v0.4.13: forward the org's default_thread hint so attesting
        // a new member doesn't silently strip it. If it wasn't set,
        // this stays undefined and the canonical payload is unchanged.
        defaultThread: current.default_thread,
      });
      await this.client.submitAttestation(newManifest);
      this.adminStatus = { kind: 'idle' };
      await this.loadPendingQueue();
      // v0.4.23: surface the freshly-attested member in the membership
      // list immediately rather than waiting for the directory_changed
      // push round-trip.
      await this.client.fetchDirectory();
      this.members = this.client.currentMembers();
    } catch (err) {
      this.adminStatus = {
        kind: 'error', message: errMsg(err),
      };
    }
  }

  /** v0.4.23: re-attest an existing member with updated fields
   *  (displayName / affiliation / role / title). The hub keeps the
   *  latest issued_at per pubkey, so appending a fresh attestation
   *  effectively replaces the old one in directory.resolve. The full
   *  attestation history stays in the manifest chain — auditable.
   *
   *  pubkey is immutable (it IS the identity); to change keys, the
   *  member onboards anew and the old pubkey is revoked. */
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
    if (!this.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: 'Root key not loaded. Import root.priv before editing members.',
      };
      return;
    }
    this.adminStatus = { kind: 'submitting' };
    const signer: RootSigner = {
      sign: (m) => rootKeychain.signMessage(m),
      pubkey: async () => (await rootKeychain.status()).public_key!,
    };
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
      });
      await this.client.submitAttestation(newManifest);
      this.adminStatus = { kind: 'idle' };
      // The hub broadcasts directory_changed → handlePushedRaw refreshes
      // the local snapshot for us, but explicitly refresh here too so
      // the panel doesn't briefly show stale rows between the POST
      // response and the WS push arrival.
      await this.client.fetchDirectory();
      this.myAttestation = this.client.myAttestation();
      this.members = this.client.currentMembers();
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.23: revoke a member's pubkey. Appends a Revocation to the
   *  manifest and POSTs to /admin/revoke. Per spec §2.3 revocations
   *  carry their own revoked_at and the directory enforces a
   *  revocation-superset rule on subsequent updates — once revoked,
   *  always revoked. The member's historical entries (signed BEFORE
   *  revoked_at) remain valid; entries signed AFTER are rejected.
   *
   *  Caller is responsible for the "you can't revoke yourself"
   *  guard (UI-side) — this method does not enforce it. */
  async revokeMember(opts: {
    pubkey: string;
    reason: string;
  }): Promise<void> {
    if (this.client === null) {
      this.adminStatus = { kind: 'error', message: 'Not connected.' };
      return;
    }
    if (!this.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: 'Root key not loaded. Import root.priv before revoking.',
      };
      return;
    }
    this.adminStatus = { kind: 'submitting' };
    const signer: RootSigner = {
      sign: (m) => rootKeychain.signMessage(m),
      pubkey: async () => (await rootKeychain.status()).public_key!,
    };
    try {
      const current = await this.client.fetchDirectory();
      const rootPub = await signer.pubkey();
      if (rootPub !== current.org) {
        throw new Error(
          'Root key on this device does not match the hub org pubkey.',
        );
      }
      // Idempotency: if there's already a revocation for this pubkey,
      // don't pile on another with a later timestamp — earliest wins
      // server-side anyway, and a second entry is just noise.
      if (current.revocations.some((r) => r.pubkey === opts.pubkey)) {
        throw new Error('This member is already revoked.');
      }
      const newRev = {
        pubkey: opts.pubkey,
        revoked_at: new Date().toISOString(),
        reason: opts.reason.trim() || 'revoked by keymaster',
      };
      const newManifest = await issueDirectory(signer, {
        org: current.org,
        attestations: [...current.attestations],
        revocations: [...current.revocations, newRev],
        prevManifestHash: hashManifest(current),
        defaultThread: current.default_thread,
      });
      await this.client.submitRevocation(newManifest);
      this.adminStatus = { kind: 'idle' };
      await this.client.fetchDirectory();
      this.myAttestation = this.client.myAttestation();
      this.members = this.client.currentMembers();
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.13: keymaster sets (or clears) the org's default landing
   *  thread for new members. Re-issues the directory manifest with the
   *  same attestations + revocations but an updated default_thread,
   *  signs via root.priv, POSTs to /admin/attest. Pass `null` to
   *  clear the hint entirely (manifest omits the field, byte-identical
   *  to pre-v0.4.13 manifests).
   *
   *  Returns the new manifest's default_thread value so the caller can
   *  update its local UI without a re-fetch round-trip.
   */
  async setDefaultThread(newDefault: string | null): Promise<string | null> {
    if (this.client === null) {
      this.adminStatus = { kind: 'error', message: 'Not connected.' };
      throw new Error('not connected');
    }
    if (!this.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: 'Root key not loaded. Import root.priv before changing org settings.',
      };
      throw new Error('root key not loaded');
    }
    this.adminStatus = { kind: 'submitting' };
    const signer: RootSigner = {
      sign: (m) => rootKeychain.signMessage(m),
      pubkey: async () => (await rootKeychain.status()).public_key!,
    };
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
      });
      await this.client.submitAttestation(newManifest);
      this.adminStatus = { kind: 'idle' };
      return newDefault;
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
      throw err;
    }
  }

  /** Helper: is the caller in board tier on this hub? Drives the
   *  AdminPanel tab visibility. The hub enforces actual access via
   *  the require_board gate on /pending — this is purely a UI hint. */
  get isBoardMember(): boolean {
    return this.myAttestation?.role === 'board';
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
      this.updateStatus = { kind: 'error', message: errMsg(err) };
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
      this.updateStatus = { kind: 'error', message: errMsg(err) };
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
    this.client?.dispose();
    this.client = null;
    this.authStatus = { kind: 'unauthenticated' };
  }

  /**
   * v0.4.0 onboarding entry point. Generates a fresh keypair on-device,
   * registers it as pending on the hub, and holds a WebSocket open
   * until the keymaster issues the attestation. On 'attested' push,
   * automatically transitions into the normal connect() flow.
   *
   * Tauri-only — the whole point is OS-keychain custody from the moment
   * the priv exists. In browser mode the user falls back to paste.
   */
  async generateAndPair(opts: {
    hubUrl: string;
    nameHint: string;
    thread: string;
  }): Promise<void> {
    if (!this.inTauri) {
      this.onboardStatus = {
        kind: 'error',
        message: 'Onboarding requires the Tauri shell — use paste mode in the browser.',
      };
      return;
    }
    this.onboardStatus = { kind: 'generating' };
    let pubkey: string;
    try {
      pubkey = await keychain.generate();
    } catch (err) {
      this.onboardStatus = {
        kind: 'error',
        message: `Key generation failed: ${errMsg(err)}`,
      };
      return;
    }
    await this.refreshKeychain();

    // Stand up a transient Client (no auth yet — registerPending is
    // public, watchPending is public) to talk to the hub.
    const client = new Client({
      hubUrl: opts.hubUrl, publicKey: pubkey,
      signer: new TauriKeychainSigner(),
    });
    try {
      await client.registerPending({ pubkey, nameHint: opts.nameHint });
    } catch (err) {
      // 409 already_attested → fast-forward to the normal connect flow.
      // The pubkey is already in the directory; no waiting required.
      if (errMsg(err) === 'already_attested') {
        this.onboardStatus = { kind: 'attested', pubkey };
        await this.connect({
          hubUrl: opts.hubUrl, publicKey: pubkey,
          thread: opts.thread, mode: 'keychain',
        });
        return;
      }
      this.onboardStatus = {
        kind: 'error',
        message: `Could not register with the hub: ${errMsg(err)}`,
      };
      return;
    }

    const pairingLink = encodePairingLink({
      hub: opts.hubUrl, pubkey, name: opts.nameHint,
    });
    this.onboardStatus = {
      kind: 'waiting', pubkey, pairingLink,
      fingerprint: fingerprintOf(pubkey),
    };

    const { promise, cancel } = client.watchPending(pubkey);
    this.watchCancel = cancel;
    try {
      await promise;
      // The hub confirmed our pubkey is in the directory. Transition.
      this.watchCancel = null;
      this.onboardStatus = { kind: 'attested', pubkey };
      await this.connect({
        hubUrl: opts.hubUrl, publicKey: pubkey,
        thread: opts.thread, mode: 'keychain',
      });
    } catch (err) {
      // Only surface as error if not cancelled by the user.
      if (this.watchCancel !== null) {
        this.onboardStatus = {
          kind: 'error',
          message: `Watch failed: ${errMsg(err)}`,
        };
      }
      this.watchCancel = null;
    }
  }

  /** User backed out of the waiting screen. Tear down the WS and
   *  reset the onboarding state. Keeps the generated keys in the
   *  keychain so a re-attempt picks up where they left off (the
   *  registered pending entry on the hub is still there too — the
   *  same key + name_hint will just upsert idempotently). */
  cancelOnboarding(): void {
    if (this.watchCancel) {
      this.watchCancel();
      this.watchCancel = null;
    }
    this.onboardStatus = { kind: 'idle' };
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
        // v0.4.17: transparent session refresh. When the 1h hub session
        // is renewed (by the pre-emptive timer or by a 401-retry), swap
        // the token into the long-lived WS subscriber. Without this the
        // Rust subscriber reconnects forever with the dead token and the
        // user sees a silently-stale feed after the first hour.
        onSessionRefreshed: (token) => { void this.handleSessionRefreshed(token); },
      });
      const sessionToken = await this.client.authenticate();
      this.sessionToken = sessionToken;
      this.authStatus = { kind: 'authenticated', pubkey: opts.publicKey };
      // Notifications: ask once, here — the user just authenticated, so
      // the OS prompt arrives WITH context (they consciously connected).
      if (this.inTauri) {
        void ensureNotificationPermission();
      }
      // v0.4.19: land on the email-style Inbox by default. Threads
      // open on click. Fetch the directory once up-front so myAttestation
      // resolves (AdminPanel visibility) without waiting for the first
      // thread-view's sync to populate the directoryView.
      this.route = 'inbox';
      await this.client.fetchDirectory();
      this.myAttestation = this.client.myAttestation();
      this.members = this.client.currentMembers();
      await this.loadInbox();
      // Sidebar thread list — non-blocking so a slow /threads doesn't
      // gate the inbox render.
      void this.loadThreads();
      // And on Tauri keymaster stations, surface the root keychain
      // state so AdminPanel can show "import root keys" if absent.
      if (this.inTauri) void this.refreshRootKeychain();
    } catch (err) {
      this.authStatus = { kind: 'failed', reason: errMsg(err) };
      this.client?.dispose();
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
            this.threadStatus = { kind: 'error', message: `stream: ${errMsg(err)}` };
          },
        );
      }
      // 2. Then sync from last-known seq.
      const initial = await client.sync(this.thread);
      for (const ve of initial) this.appendIfNew(ve);
      this.threadStatus = { kind: 'idle' };
    } catch (err) {
      this.threadStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.17: the Client signalled a session refresh. The HTTP path
   *  already picked up the new token internally; we just need to point
   *  the long-lived WS subscriber at it. Tearing down + re-subscribing
   *  is the simplest correct thing — the next sync() closes any gap
   *  while the new socket was opening. */
  private async handleSessionRefreshed(token: string): Promise<void> {
    this.sessionToken = token;
    if (this.client === null) return;
    this.teardown?.();
    this.teardown = null;
    await this.syncAndSubscribe();
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
      // v0.4.18: hub pushed a directory mutation (attest/revoke). Refetch
      // /directory so the next entry from a freshly-attested member doesn't
      // render as 'not attested', and refresh the cached own-attestation
      // so AdminPanel visibility flips on as soon as the keymaster acts.
      if (msg.type === 'directory_changed') {
        await this.client.fetchDirectory();
        this.myAttestation = this.client.myAttestation();
        this.members = this.client.currentMembers();
        // v0.4.19: inbox row previews carry server-resolved display_name
        // + role. A directory change can rename people / promote / demote,
        // so the preview can go stale. Refetch if we're currently
        // showing the inbox (cheap; otherwise it'll repopulate on the
        // next goToInbox).
        if (this.route === 'inbox') void this.loadInbox();
        return;
      }
      if (msg.type !== 'entry') return;
      const ve = await this.client.verify(msg.entry, msg.seq);
      if (ve.entry.thread !== this.thread) return;
      this.appendIfNew(ve);
    } catch (err) {
      // VerificationError lands here — DO NOT render. A failed verify on
      // a pushed entry is exactly the case the spec calls for refusing.
      this.threadStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  private appendIfNew(ve: VerifiedEntry) {
    if (!ve.entry.id || this.seenIds.has(ve.entry.id)) return;
    this.seenIds.add(ve.entry.id);
    this.entries = [...this.entries, ve].sort((a, b) => a.seq - b.seq);
  }

  /** v0.2: branch off a sub-thread from the current thread.
   *  Posts a kind='branch' entry in the current thread that names the
   *  new sub-thread, then switches the active thread to it. The body
   *  is the rationale ("Let's continue the budget here…") — it appears
   *  in the parent thread feed as the link card. */
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
    // Switch to the new sub-thread once the branch entry is accepted. The
    // sub-thread materializes when its first entry posts — until then it's
    // an empty feed pointed at by the branch link.
    await this.switchThread(newThread);
    // loadThreads refreshes parent_thread bookkeeping in the sidebar.
    void this.loadThreads();
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
      branch_thread: null,
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
  /** v0.4.11: flip between Cards and Chat rendering for the
   *  chronological feed. Persisted so the choice carries across
   *  launches. */
  setViewMode(mode: 'cards' | 'chat'): void {
    this.viewMode = mode;
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('cove.viewMode', mode);
    }
  }

  /** v0.4.19: pull the landing-view bundle from /inbox and prime the
   *  receipt-tracker so re-entering a thread the user already acked in
   *  a prior session doesn't trigger a redundant receipt. */
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

  /** v0.4.19: return to the email-style landing view. Tears down the
   *  per-thread WS subscription (we'll re-open it the next time a
   *  thread is opened) and re-loads the inbox so unread state reflects
   *  any receipts posted during the just-finished thread session. */
  async goToInbox(): Promise<void> {
    this.teardown?.();
    this.teardown = null;
    this.entries = [];
    this.seenIds = new Set();
    this.replyOpen = null;
    this.route = 'inbox';
    await this.loadInbox();
  }

  /** v0.4.19: post a kind='receipt' entry acking the latest non-receipt
   *  seq this client has loaded for the thread, IF that seq exceeds the
   *  last receipt we (or any prior session of ours) posted. One receipt
   *  per session per thread per new high-water — explicit choice made
   *  during the design conversation ("every read is provable").
   *
   *  Receipts are entries: they pass through the throttle/quota layer
   *  like any other post (§7.2) and get fanned out via /stream. They
   *  carry the observed STH so the audit record commits to which tree
   *  state the member acked.
   *
   *  Filtering kind='receipt' out of the chronological feed happens in
   *  ThreadView (a receipt is not a message to a reader); they're still
   *  in this.entries for the high-water computation here.
   */
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
      // Prefer the STH the client already cached during the just-completed
      // sync. fetchSth as a fallback so the receipt is never built on a
      // stale tree (or null).
      const sth = this.client.latestSth() ?? await this.client.fetchSth();
      const receiptSeq = await this.client.postReceipt({
        thread, highWaterSeq: latestUserSeq, observedSth: sth,
      });
      this.myReceiptSeq.set(thread, receiptSeq);
    } catch {
      // Receipt-posting is best-effort. A throttle/network failure here
      // should not break the thread view; the user has already SEEN the
      // entries, and we'll try again on the next thread enter.
    }
  }

  async switchThread(name: string): Promise<void> {
    if (this.client === null) return;
    // v0.4.19: also covers the inbox→thread transition. If we're
    // already showing the named thread AND we're in thread route, this
    // is a no-op; otherwise we're either switching threads or returning
    // from the inbox view and need to set up the subscription anew.
    if (name === this.thread && this.route === 'thread') return;
    this.route = 'thread';
    this.thread = name;
    // v0.4.9: persist last-viewed thread so the auth panel's thread
    // field pre-fills with where the user actually was on next launch,
    // not the AuthPanel default. Shares the same key as the AuthPanel
    // input — one round-tripped value, not two competing concepts.
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('cove.thread', name);
    }
    this.entries = [];
    this.seenIds = new Set();
    // The Client's per-thread delta-sync cursor is paired with our
    // in-memory entries: clearing the entries means the next sync must
    // replay from the start, not from the high-water we set last time
    // we were on this thread. Without this, switching parent→branch→
    // parent comes back to an empty feed because /sync?since=N returns
    // nothing new.
    this.client.resetHighWater(name);
    // Close any open reply panel — its parent belongs to the old thread.
    this.replyOpen = null;
    // Reset the main pane to the chronological feed — landing in 'files'
    // because the previous thread was on it would be disorienting.
    this.view = 'messages';
    this.teardown?.();
    this.teardown = null;
    await this.syncAndSubscribe();
    // v0.4.19: after sync brings entries in, post a receipt if there
    // are new non-receipt entries past our last ack. Fire-and-forget
    // so the user is never waiting on the receipt round-trip; failures
    // are non-fatal (see markThreadRead).
    void this.markThreadRead(name);
  }
}
