/**
 * Reactive app state — Svelte 5 runes wrapped in a small class.
 *
 * The Client class itself is not reactive; this wrapper is. UI reads
 * `state.entries`, `state.client`, etc. and re-renders when they change.
 * One mutation point per concern keeps it analyzable.
 */
import { Client, TauriKeychainSigner, type VerifiedEntry } from './client';
import { canonicalize } from './crypto';
import { issueAttestation, issueDirectory, type RootSigner } from './identity';
import { encodePairingLink, fingerprint as fingerprintOf } from './pairing';
import {
  appVersion,
  ensureNotificationPermission, isPWA, isTauri, keychain, rootKeychain, stream, updater,
  type AvailableUpdate,
} from './tauri';
import type {
  Attestation, DirectoryManifest, InboxRow, Invite, ThreadSummary,
} from './types';
import { DEFAULT_CAPABILITIES_BY_ROLE } from './types';
import type { RevokedEntry } from './client';
import { hashManifest } from './verify';
import {
  clearVault, requestPersistentStorage, storeKey as storeVaultKey,
  unlockKey as unlockVaultKey, vaultStatus as readVaultStatus,
  type VaultStatus,
} from './vault';

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
  /** v0.4.29: installed PWA mode. Browser-only-mode + installed-to-
   *  home-screen. Auth UI treats this like Tauri's paste flow (no
   *  hardware keychain) but tunes the messaging — the user is on
   *  a mobile device that just opened the app from its icon. */
  inPWA = $state<boolean>(isPWA());
  /** v0.4.16: bundle version exposed in the UI so users can read off
   *  which build they're on without digging into the OS-level About
   *  dialog. Populated asynchronously after construct because
   *  getVersion is a Tauri IPC call; null while it's still resolving
   *  AND in browser-only mode (no IPC at all). */
  appVersion = $state<string | null>(null);
  /** Public key stored in the OS keychain (Tauri only). When set,
   *  AuthPanel shows 'Unlock' rather than the import form. */
  storedPublicKey = $state<string | null>(null);
  /** v0.4.34: passphrase-encrypted vault status (PWA / browser-only
   *  path). When .exists, AuthPanel shows the pwa-unlock form
   *  ("Welcome back, 9add…2c1, enter passphrase"). Populated from
   *  IndexedDB by refreshVaultStatus() on construct + after vault
   *  mutations. */
  vaultStatus = $state<VaultStatus>({ exists: false });
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

  /** v0.4.45: whether the ThreadList sidebar is visible. Persisted to
   *  localStorage. Defaults to closed on narrow (mobile) viewports and
   *  open on wide (desktop) ones so a phone doesn't lose 240px of screen
   *  real estate on first paint. The user's explicit choice — if any —
   *  wins over the media-query default. */
  sidebarOpen = $state<boolean>(
    (() => {
      if (typeof localStorage !== 'undefined') {
        const saved = localStorage.getItem('cove.sidebarOpen');
        if (saved === 'true') return true;
        if (saved === 'false') return false;
      }
      // No stored preference — default by viewport width.
      if (typeof window !== 'undefined' && window.matchMedia) {
        return window.matchMedia('(min-width: 640px)').matches;
      }
      return true;
    })(),
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
    // v0.4.34: vault is PWA / browser only. Read its status so
    // AuthPanel can pick pwa-unlock vs Get started immediately. Also
    // request persistent storage so the OS doesn't evict the vault
    // under storage pressure.
    if (!this.inTauri) {
      void this.refreshVaultStatus();
      void requestPersistentStorage();
    }
  }

  /** v0.4.34: read the IndexedDB vault status into reactive state. */
  async refreshVaultStatus(): Promise<void> {
    this.vaultStatus = await readVaultStatus();
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
   *  Drives AdminPanel visibility (via hasCapability + isBoardMember).
   *  Null until fetchDirectory has run, which happens during connect(). */
  myAttestation = $state<Attestation | null>(null);
  /** v0.4.25: the current directory manifest, cached so hasCapability
   *  can read capabilities_by_role without a re-fetch. Refreshed
   *  alongside myAttestation. */
  manifest = $state<DirectoryManifest | null>(null);
  /** v0.4.23: snapshot of currently-attested, non-revoked members
   *  for the AdminPanel membership editor. Refreshed alongside
   *  myAttestation on directory fetches and after admin mutations. */
  members = $state<Attestation[]>([]);
  /** v0.4.24: revocations + their last-known attestation, newest
   *  first. Drives the "Recently revoked" subsection in AdminPanel. */
  revoked = $state<RevokedEntry[]>([]);
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
        // v0.4.25: forward the existing role → caps map so admin
        // mutations to attestations/revocations don't silently strip
        // it. If the manifest doesn't carry one, this stays undefined.
        capabilitiesByRole: current.capabilities_by_role ?? null,
      });
      await this.client.submitAttestation(newManifest);
      this.adminStatus = { kind: 'idle' };
      await this.loadPendingQueue();
      // v0.4.23: surface the freshly-attested member in the membership
      // list immediately rather than waiting for the directory_changed
      // push round-trip.
      await this.client.fetchDirectory();
      this.members = this.client.currentMembers();
      this.revoked = this.client.recentlyRevoked();
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
        // v0.4.25: forward the existing role → caps map so admin
        // mutations to attestations/revocations don't silently strip
        // it. If the manifest doesn't carry one, this stays undefined.
        capabilitiesByRole: current.capabilities_by_role ?? null,
      });
      await this.client.submitAttestation(newManifest);
      this.adminStatus = { kind: 'idle' };
      // The hub broadcasts directory_changed → handlePushedRaw refreshes
      // the local snapshot for us, but explicitly refresh here too so
      // the panel doesn't briefly show stale rows between the POST
      // response and the WS push arrival.
      await this.client.fetchDirectory();
      this.myAttestation = this.client.myAttestation();
      this.manifest = this.client.currentManifest();
      this.members = this.client.currentMembers();
      this.revoked = this.client.recentlyRevoked();
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
        // v0.4.25: forward the existing role → caps map so admin
        // mutations to attestations/revocations don't silently strip
        // it. If the manifest doesn't carry one, this stays undefined.
        capabilitiesByRole: current.capabilities_by_role ?? null,
      });
      await this.client.submitRevocation(newManifest);
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

  /** v0.4.25: re-issue the directory manifest with a new role → caps
   *  map. Like setDefaultThread, this is a root-signed manifest update
   *  posted via /admin/attest. Pass an empty object to express "all
   *  roles have no caps"; pass null to CLEAR the field entirely
   *  (manifest omits it; clients fall back to default mapping).
   */
  async setCapabilitiesByRole(
    next: Record<string, string[]> | null,
  ): Promise<void> {
    if (this.client === null) {
      this.adminStatus = { kind: 'error', message: 'Not connected.' };
      return;
    }
    if (!this.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: 'Root key not loaded. Import root.priv before changing roles.',
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
      const newManifest = await issueDirectory(signer, {
        org: current.org,
        attestations: [...current.attestations],
        revocations: [...current.revocations],
        prevManifestHash: hashManifest(current),
        defaultThread: current.default_thread,
        capabilitiesByRole: next,
      });
      await this.client.submitAttestation(newManifest);
      this.adminStatus = { kind: 'idle' };
      // Refresh local snapshot so the AdminPanel + every hasCapability
      // consumer reflects the new map immediately.
      await this.client.fetchDirectory();
      this.manifest = this.client.currentManifest();
      this.myAttestation = this.client.myAttestation();
      this.members = this.client.currentMembers();
      this.revoked = this.client.recentlyRevoked();
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.24: set a per-identity throttle tier override (§7.2.2).
   *  The hub's /admin/limits is process-local — overrides EVAPORATE on
   *  hub restart by design. Use this for "raise so-and-so's tier for
   *  the annual mailing" rather than as a durable role tweak (durable
   *  changes go through updateMember).
   *
   *  Payload is root-signed off-hub via rootKeychain (the hub holds
   *  NO root.priv, per CLAUDE.md non-negotiable #1). */
  async setMemberTier(opts: {
    pubkey: string;
    tier: 'member' | 'officer' | 'board';
  }): Promise<void> {
    if (this.client === null) {
      this.adminStatus = { kind: 'error', message: 'Not connected.' };
      return;
    }
    if (!this.rootKeysPresent) {
      this.adminStatus = {
        kind: 'error',
        message: 'Root key not loaded. Import root.priv before changing limits.',
      };
      return;
    }
    this.adminStatus = { kind: 'submitting' };
    try {
      const payload = { pubkey: opts.pubkey, tier: opts.tier };
      const sig = await rootKeychain.signMessage(canonicalize(payload));
      await this.client.submitTierOverride({ payload, sig });
      this.adminStatus = { kind: 'idle' };
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.33: invites — admin operations + loaded list. The keymaster
   *  mints a code, delivers it out-of-band, the member uses it on
   *  /pending. The list state is what AdminPanel renders. */
  invites = $state<Invite[]>([]);
  invitesStatus = $state<{ kind: 'idle' } | { kind: 'loading' }
    | { kind: 'error'; message: string }>({ kind: 'idle' });

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

  /** Mint a single invite. Returns the minted code so the UI can
   *  immediately show "Copy" / "Share" without waiting for a refetch. */
  async mintInvite(opts: {
    ttlSeconds: number;
    nameHint?: string;
  }): Promise<Invite | null> {
    if (this.client === null || !this.rootKeysPresent) {
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
      const sig = await rootKeychain.signMessage(canonicalize(payload));
      const inv = await this.client.submitInviteMint({ payload, sig });
      this.adminStatus = { kind: 'idle' };
      // Optimistic insert so the panel updates immediately.
      this.invites = [...this.invites, inv];
      return inv;
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
      return null;
    }
  }

  async revokeInvite(code: string): Promise<void> {
    if (this.client === null || !this.rootKeysPresent) return;
    this.adminStatus = { kind: 'submitting' };
    try {
      const payload = { code };
      const sig = await rootKeychain.signMessage(canonicalize(payload));
      await this.client.submitInviteRevoke({ code, payload, sig });
      this.adminStatus = { kind: 'idle' };
      this.invites = this.invites.filter((i) => i.code !== code);
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
        // v0.4.25: changing default_thread shouldn't strip the org's
        // role → caps map either.
        capabilitiesByRole: current.capabilities_by_role ?? null,
      });
      await this.client.submitAttestation(newManifest);
      this.adminStatus = { kind: 'idle' };
      return newDefault;
    } catch (err) {
      this.adminStatus = { kind: 'error', message: errMsg(err) };
      throw err;
    }
  }

  /** v0.4.25: is the caller granted `cap` under the current manifest?
   *  Reads manifest.capabilities_by_role when set; otherwise falls
   *  back to DEFAULT_CAPABILITIES_BY_ROLE (board → admin + archive).
   *
   *  The hub enforces the real check via require_capability(cap) on
   *  protected endpoints — this is the client-side UI hint that
   *  decides whether to even SHOW the affordance. */
  hasCapability(cap: string): boolean {
    const role = this.myAttestation?.role;
    if (!role) return false;
    const map = this.manifest?.capabilities_by_role ?? DEFAULT_CAPABILITIES_BY_ROLE;
    return (map[role] ?? []).includes(cap);
  }

  /** Back-compat: pre-v0.4.25 callers used isBoardMember to gate the
   *  AdminPanel. The semantics are now "has the 'admin' capability,"
   *  which under the default mapping is exactly board role — but an
   *  org can remap so that some other role gets admin instead. */
  get isBoardMember(): boolean {
    return this.hasCapability('admin');
  }

  /** v0.4.30: + New thread dialog state. Lifted from InboxPanel so
   *  the sidebar (and any other entry point later) can open the same
   *  dialog without duplicating its state machine. Null when closed. */
  newThreadDialog = $state<{
    name: string;
    scope: 'public' | 'private';
    selected: Set<string>;
    message: string;
    submitting: boolean;
    error: string | null;
    /** v0.4.38: opt in to an ephemeral thread with a TTL. When true,
     *  submitNewThread routes through openEphemeralThread instead of
     *  createDirectThread / switchThread.
     *  v0.4.48: ephemeral + private is now supported — after
     *  openEphemeralThread we post an audience entry into the newly
     *  opened thread (the audience entry lives in the ephemeral log
     *  and dies with the thread). */
    ephemeral: boolean;
    ttlDays: number;
  } | null>(null);

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

  /** Run the new-thread submit through createDirectThread (private)
   *  or switchThread+post (public). Sets/clears submitting + error
   *  on the dialog state so the UI can render spinner + failure. */
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
        // v0.4.38: ephemeral thread creation. TTL in days → seconds.
        // Backend clamps to [1d, 365d]; the dialog UX presets to
        // 7/30/90 with a free-form field for anything else.
        // v0.4.48: ephemeral + private is now supported. After the
        // hub opens the thread, post an audience entry into it (also
        // ephemeral, dies with the thread). Order matters: audience
        // must land before the first post so non-audience subscribers
        // never see the post pushed.
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

  /** v0.4.25: is a named thread currently archived? Consults the
   *  cached inbox rows first (most authoritative — server-computed
   *  under the current manifest) then the sidebar thread list. A
   *  freshly-typed name the hub has never seen returns false. */
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
  /** v0.4.33: held transiently when the PWA generates a keypair
   *  in-JS during onboarding. Carries the priv from generateAndPair
   *  through to the post-attestation connect() call so the new identity
   *  works for the rest of the session. localStorage / IndexedDB
   *  persistence is the v0.4.31 vault slice (deferred). */
  private pwaTransientPriv: string | null = null;

  async generateAndPair(opts: {
    hubUrl: string;
    nameHint: string;
    thread: string;
    invite: string;       // v0.4.33: required for /pending
    /** v0.4.34: PWA / browser path. When provided, the generated priv
     *  is encrypted under this passphrase and stored in IndexedDB
     *  BEFORE the /pending POST — so a reload during the waiting
     *  screen still recovers the keys via the unlock flow. Tauri
     *  path ignores this (OS keychain owns the priv). */
    passphrase?: string;
  }): Promise<void> {
    this.onboardStatus = { kind: 'generating' };
    let pubkey: string;
    let privForPaste: string | null = null;

    // v0.4.33: two key-generation paths.
    //  - Tauri: keychain.generate() puts the priv straight into
    //    Mac/Windows/Linux keychain; pubkey returned, priv never
    //    crosses the JS boundary.
    //  - Browser / PWA: generate via @noble/curves in JS heap and
    //    hand the priv to the post-attestation connect() via the
    //    transient field. Loses persistence (clear on tab close)
    //    until the IndexedDB+passphrase vault slice ships.
    if (this.inTauri) {
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
    } else {
      try {
        const { ed25519 } = await import('@noble/curves/ed25519');
        const { bytesToHex } = await import('@noble/hashes/utils');
        const privBytes = ed25519.utils.randomPrivateKey();
        const pubBytes = ed25519.getPublicKey(privBytes);
        privForPaste = bytesToHex(privBytes);
        pubkey = bytesToHex(pubBytes);
        this.pwaTransientPriv = privForPaste;
      } catch (err) {
        this.onboardStatus = {
          kind: 'error',
          message: `Key generation failed: ${errMsg(err)}`,
        };
        return;
      }

      // v0.4.34: encrypt + persist the priv BEFORE the /pending POST.
      // A reload during the waiting screen would otherwise lose the
      // keys (they'd live only in pwaTransientPriv). With the vault,
      // the user just re-unlocks with their passphrase on next launch.
      if (opts.passphrase && privForPaste) {
        try {
          await this.storeKeyInVault({
            priv: privForPaste, pub: pubkey,
            passphrase: opts.passphrase,
          });
        } catch (err) {
          this.onboardStatus = {
            kind: 'error',
            message: `Could not store key in this browser: ${errMsg(err)}`,
          };
          return;
        }
      }
    }

    // Stand up a transient Client (no auth yet — registerPending is
    // public, watchPending is public) to talk to the hub.
    const client = new Client({
      hubUrl: opts.hubUrl, publicKey: pubkey,
      signer: this.inTauri
        ? new TauriKeychainSigner()
        : undefined,
      privateKey: this.inTauri ? undefined : privForPaste ?? undefined,
    });
    try {
      await client.registerPending({
        pubkey, nameHint: opts.nameHint, invite: opts.invite,
      });
    } catch (err) {
      // 409 already_attested → fast-forward to the normal connect flow.
      // The pubkey is already in the directory; no waiting required.
      if (errMsg(err) === 'already_attested') {
        this.onboardStatus = { kind: 'attested', pubkey };
        await this.connect({
          hubUrl: opts.hubUrl, publicKey: pubkey,
          thread: opts.thread,
          mode: this.inTauri ? 'keychain' : 'paste',
          privateKey: privForPaste ?? undefined,
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
        thread: opts.thread,
        mode: this.inTauri ? 'keychain' : 'paste',
        privateKey: privForPaste ?? undefined,
      });
      this.pwaTransientPriv = null;
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
  /** v0.4.34: PWA / browser-only — decrypt the vault with the user's
   *  passphrase and connect. Wrong passphrase throws (surfaced
   *  inline in AuthPanel). After successful unlock the priv lives in
   *  JS heap (same lifetime as paste mode) until tab close. */
  async unlockFromVault(opts: {
    passphrase: string;
    hubUrl: string;
    thread: string;
  }): Promise<void> {
    if (!this.vaultStatus.exists) {
      throw new Error('no vault on this device');
    }
    const { priv, pub } = await unlockVaultKey(opts.passphrase);
    await this.connect({
      hubUrl: opts.hubUrl, privateKey: priv, publicKey: pub,
      thread: opts.thread, mode: 'paste',
    });
  }

  /** v0.4.34: persist a freshly-generated keypair under a passphrase.
   *  Called by OnboardingPanel in PWA mode AFTER key generation but
   *  BEFORE the /pending POST — so a reload during the
   *  "Waiting for approval" wait doesn't lose the keys. */
  async storeKeyInVault(opts: {
    priv: string;
    pub: string;
    passphrase: string;
  }): Promise<void> {
    await storeVaultKey(opts);
    await this.refreshVaultStatus();
  }

  /** v0.4.34: wipe the vault. AuthPanel's "Use a different key" calls
   *  this when the user wants to start over with a fresh keypair
   *  (forgot passphrase / really left the org / etc.). */
  async forgetVault(): Promise<void> {
    await clearVault();
    await this.refreshVaultStatus();
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
      this.manifest = this.client.currentManifest();
      this.members = this.client.currentMembers();
      this.revoked = this.client.recentlyRevoked();
      await this.loadInbox();
      // Sidebar thread list — non-blocking so a slow /threads doesn't
      // gate the inbox render.
      void this.loadThreads();
      // v0.4.53: open the WebSocket subscription eagerly so pushes
      // (new-thread announcements, audience updates, receipts) reach
      // the client from the moment the user authenticates — not just
      // after they've opened a thread once. Without this, someone
      // being added to an audience while they're sitting on Inbox
      // wouldn't see the new thread appear in the sidebar until
      // they manually refreshed.
      void this.ensureSubscribed();
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
      // 1. Subscribe FIRST — tearing down any prior subscription so it
      // picks up the new this.thread (used by the Rust notification
      // title + the browser-mode thread filter).
      await this.ensureSubscribed({ restart: true });
      // 2. Then sync from last-known seq.
      const initial = await client.sync(this.thread);
      for (const ve of initial) this.appendIfNew(ve);
      this.threadStatus = { kind: 'idle' };
    } catch (err) {
      this.threadStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.53: bring up the WS subscription if it isn't running, without
   *  requiring a thread-view context. Used by (a) post-auth boot so
   *  Inbox users get pushes from the moment they log in, and (b)
   *  syncAndSubscribe with restart:true to refresh the subscription's
   *  bound thread when the user switches threads. */
  private async ensureSubscribed(opts: { restart?: boolean } = {}): Promise<void> {
    if (this.client === null) return;
    if (this.teardown !== null && !opts.restart) return;
    if (this.teardown !== null && opts.restart) {
      this.teardown();
      this.teardown = null;
    }
    if (this.inTauri) {
      const teardown = await stream.start(
        { hubUrl: this.hubUrl, token: this.sessionToken, thread: this.thread },
        (raw) => { void this.handlePushedRaw(raw); },
      );
      this.teardown = () => { void teardown(); };
    } else {
      // v0.4.55: browser subscribe path previously used
      // client.subscribe with a callback that filtered by thread and
      // appended matching entries. It missed the v0.4.48 unknown-
      // thread detection that lives in handlePushedRaw — so on the
      // PWA, a push announcing a brand-new audience-scoped thread
      // (audience entry or first post) would arrive on the WS,
      // client.subscribe's thread filter would drop it, and the
      // sidebar never refreshed. The Tauri path was fine because
      // stream.start forwards raw payloads to handlePushedRaw
      // regardless of thread. Now the browser path does the same:
      // hook the raw WebSocket directly and route both channels
      // through the same handler.
      this.teardown = this.subscribeRawWs();
    }
  }

  /** v0.4.55: minimal WS subscriber for browser mode that forwards
   *  raw payloads to handlePushedRaw. Mirrors the Tauri stream's
   *  behavior — no per-thread filtering here, the JS-side handler
   *  decides what to do with each push (refresh listings, verify +
   *  append to current feed, etc.). */
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
        this.manifest = this.client.currentManifest();
        this.members = this.client.currentMembers();
      this.revoked = this.client.recentlyRevoked();
        // v0.4.19: inbox row previews carry server-resolved display_name
        // + role. A directory change can rename people / promote / demote,
        // so the preview can go stale. Refetch if we're currently
        // showing the inbox (cheap; otherwise it'll repopulate on the
        // next goToInbox).
        if (this.route === 'inbox') void this.loadInbox();
        return;
      }
      // v0.4.48 → v0.4.54: this handler used to receive a global
      // thread_opened broadcast from the hub. The hub no longer emits
      // that message — it leaked the existence of newly-created
      // audience-scoped threads to non-audience members between the
      // "open" call and the subsequent audience entry landing. Audience
      // members now learn about a new thread the correct way: the
      // audience entry lands via WS and the unknown-thread detection
      // below triggers loadThreads. Handler kept as a no-op for
      // forward-compat with any hub still emitting the old event
      // (older self-hosted deployments during rollout).
      if (msg.type === 'thread_opened') {
        return;
      }
      // v0.4.38: an ephemeral thread was sealed. Purge any local
      // in-memory entries for that thread (the hub deleted them
      // durably) and refresh /threads + /inbox so the UI reflects
      // the new tombstoned state.
      if (msg.type === 'thread_tombstoned') {
        const t = msg.thread as string;
        if (this.thread === t) {
          this.entries = [];
          this.seenIds.clear();
        }
        void this.loadThreads();
        if (this.route === 'inbox') void this.loadInbox();
        return;
      }
      if (msg.type !== 'entry') return;
      const ve = await this.client.verify(msg.entry, msg.seq);
      // v0.4.48: a pushed entry can announce a thread we haven't seen
      // yet (someone else just created it). Refresh /threads so the
      // sidebar picks it up; still don't append to the visible feed
      // unless it's for the currently-viewed thread.
      if (!this.threads.some((t) => t.thread === ve.entry.thread)) {
        void this.loadThreads();
      }
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

  /** v0.4.25: post a kind='archive' or kind='reopen' entry on the
   *  given thread. Body carries the rationale. Caller checks
   *  hasCapability('archive') first; this is the wire op. */
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
    // Refresh the inbox + thread list so the archived flag flips
    // immediately for the keymaster who acted. Other connected
    // clients pick it up on the /stream entry push (their inboxRows
    // refresh in loadInbox; sidebars refresh in loadThreads via the
    // post round-trip).
    await this.loadInbox();
    void this.loadThreads();
  }

  /** v0.4.27: post a kind='audience' entry on `thread` scoping it to
   *  `pubkeys`. The hub enforces the "must currently be in the
   *  audience to update" rule (or "anyone for the first entry").
   *  Refreshes inbox + threads so the new audience reflects locally. */
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

  /** v0.4.27: create a new thread with an audience scope and an
   *  initial message in one user action. Posts:
   *    1. kind='audience' establishing the scope
   *    2. kind='post' with the first message
   *  The hub assigns sequential seqs; clients see the audience
   *  before the post arrives, so the entry is filtered correctly
   *  for non-audience subscribers.
   *
   *  `pubkeys` MUST include the caller — otherwise they'd lock
   *  themselves out of their own thread. UI enforces this; we also
   *  guard here. */
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
    // Then the first post. switchThread sets up the WS subscription,
    // syncs, and posts the receipt; we use it so the user lands inside
    // the new thread after creation rather than back on Inbox.
    await this.switchThread(opts.thread);
    if (opts.message.trim()) {
      await this.post(opts.message);
    }
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

  /** v0.4.45: toggle the sidebar. Persists the explicit choice so a
   *  user who opened it on their phone keeps it open next time even
   *  though the viewport-default would have closed it. */
  toggleSidebar(): void {
    this.sidebarOpen = !this.sidebarOpen;
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('cove.sidebarOpen', String(this.sidebarOpen));
    }
  }
  closeSidebar(): void {
    if (!this.sidebarOpen) return;
    this.sidebarOpen = false;
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('cove.sidebarOpen', 'false');
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
    // v0.4.53: do NOT tear down the WS subscription on the Inbox route.
    // handlePushedRaw already ignores entries whose thread doesn't
    // match this.thread for feed appending; keeping the socket open
    // lets the unknown-thread + thread_opened + thread_tombstoned
    // handlers refresh /threads and /inbox in response to pushes that
    // would otherwise silently miss us (e.g., someone else adds this
    // caller to an audience-scoped thread — Inbox previously would
    // only surface it after a manual refresh).
    // (If we haven't opened a thread yet in this session, no WS was
    // running anyway — this is a no-op in that case.)
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
