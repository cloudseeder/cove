/**
 * Reactive app state — Svelte 5 runes wrapped in a small class.
 *
 * v0.4.68 (Federation UI, Phase 1): everything per-hub lives on the
 * `HubConnection` class in ./hub.svelte.ts. `AppState.hub` holds one
 * connection today; Phase 2 grows this to a Map<HubUrl, HubConnection>
 * with a hub switcher UI. Every existing `app.xxx` per-hub surface is
 * preserved via delegating getters/methods so consumer .svelte files
 * are unchanged. See /home/brooks/.claude/plans/glimmering-fluttering-boole.md.
 */
import { Client, TauriKeychainSigner, type VerifiedEntry } from './client';
import {
  HubConnection,
  type AuthStatus, type ThreadStatus, type View, type ConnectOpts,
} from './hub.svelte';
import {
  loadActiveHubUrl, loadHubUrls, loadThreadFor, migrateLegacyKeys,
  removeHubUrl, saveActiveHubUrl, saveHubUrls,
} from './hubs';
import { encodePairingLink, fingerprint as fingerprintOf } from './pairing';
import {
  appVersion, isPWA, isTauri, keychain, rootKeychain, updater,
  type AvailableUpdate,
} from './tauri';
import type {
  Attestation, DirectoryManifest, InboxRow, Invite, KeypairGroup, ThreadSummary,
} from './types';
import type { RevokedEntry } from './client';
import {
  clearVault, requestPersistentStorage, storeKey as storeVaultKey,
  unlockKey as unlockVaultKey, vaultStatus as readVaultStatus,
  type VaultStatus,
} from './vault';
import {
  clearPasskeyStorage,
  passkeyStatus as readPasskeyStatus,
  passkeySupported as detectPasskeySupported,
  registerPasskey,
  unlockWithPasskey,
  type PasskeyStatus,
} from './passkey';
import {
  addPasskeySlot as vaultAddPasskeySlot,
  addPassphraseSlot as vaultAddPassphraseSlot,
  createVault as vaultCreate,
  removeSlot as vaultRemoveSlot,
  unlockWithPasskey as vaultUnlockPasskey,
  unlockWithPassphrase as vaultUnlockPassphrase,
  type FirstUnlockChoice, type MethodSlot, type VaultRecord,
} from './vault-blob';
import { StaleVaultError } from './errors';

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

type UpdateStatus =
  | { kind: 'idle' }
  | { kind: 'checking' }
  | { kind: 'available'; update: AvailableUpdate }
  | { kind: 'installing'; downloaded: number; total: number | null }
  | { kind: 'error'; message: string };

// Re-export the per-hub union types so existing consumers that import
// from './state.svelte' keep working.
export type { AuthStatus, ThreadStatus, View } from './hub.svelte';

export class AppState {
  // ---------------------------------------------------------------------
  // Global state (stays on AppState — device/build/UI-prefs/onboarding)
  // ---------------------------------------------------------------------

  /** v0.4.19: top-level navigation. After Unlock the user lands on the
   *  email-style InboxPanel; clicking a row switches to 'thread'. */
  route = $state<'inbox' | 'thread'>('inbox');
  /** True iff running inside the Tauri shell. */
  inTauri = $state<boolean>(isTauri());
  /** v0.4.29: installed PWA mode. */
  inPWA = $state<boolean>(isPWA());
  /** v0.4.16: bundle version exposed in the UI. */
  appVersion = $state<string | null>(null);
  /** Public key stored in the OS keychain (Tauri only). */
  storedPublicKey = $state<string | null>(null);
  /** v0.4.34: passphrase-encrypted vault status (PWA / browser-only). */
  vaultStatus = $state<VaultStatus>({ exists: false });
  /** v0.4.74: WebAuthn Passkey status. Same shape as vault: does the
   *  client have a registered Passkey identity on this device? */
  passkeyStatus = $state<PasskeyStatus>({ exists: false });
  /** v0.4.74: whether this browser can DO Passkey with PRF at all.
   *  Feature-detected in the constructor. False → the UI hides the
   *  Passkey affordance silently and falls through to vault/paste. */
  passkeySupported = $state<boolean>(false);
  /** Updater status — drives the quiet 'Update available' affordance. */
  updateStatus = $state<UpdateStatus>({ kind: 'idle' });
  /** v0.4.11: chronological-feed visual mode. */
  viewMode = $state<'cards' | 'chat'>(
    typeof localStorage !== 'undefined'
      && localStorage.getItem('cove.viewMode') === 'chat' ? 'chat' : 'cards',
  );
  /** v0.4.45: whether the ThreadList sidebar is visible. */
  sidebarOpen = $state<boolean>(
    (() => {
      if (typeof localStorage !== 'undefined') {
        const saved = localStorage.getItem('cove.sidebarOpen');
        if (saved === 'true') return true;
        if (saved === 'false') return false;
      }
      if (typeof window !== 'undefined' && window.matchMedia) {
        return window.matchMedia('(min-width: 640px)').matches;
      }
      return true;
    })(),
  );
  /** v0.4.0: state of the on-device-keygen onboarding flow. */
  onboardStatus = $state<OnboardStatus>({ kind: 'idle' });
  /** v0.4.0: keymaster mode. True when the ROOT_PRIV_SLOT has a root key. */
  rootKeysPresent = $state<boolean>(false);

  /** Cancel handle for the WS /pending/watch — calling it tears the socket
   *  down without rejecting. */
  private watchCancel: (() => void) | null = null;
  /** v0.4.33: held transiently when the PWA generates a keypair in-JS
   *  during onboarding. Carries the priv from generateAndPair through to
   *  the post-attestation connect() call. */
  private pwaTransientPriv: string | null = null;
  /** v0.4.69: PWA/paste-mode session priv, kept in memory so the
   *  add-hub flow can construct a second Client without asking the user
   *  to re-paste. Same threat-model exposure as today's paste-mode
   *  session priv — just a controlled reference point. Cleared on
   *  logoutAll(). null for Tauri (OS keychain owns the priv). */
  livePriv = $state<string | null>(null);

  /** v0.4.76: content-encryption key for the identity vault. Held with
   *  livePriv so slot mutations (add/remove passphrase or Passkey) can
   *  rewrap the CEK without asking the user to re-unlock. Cleared on
   *  logoutAll(). null when the current session started from paste mode
   *  or Tauri keychain (no vault involved). */
  liveCek = $state<Uint8Array | null>(null);
  /** v0.4.76: last-known vault record for the current identity. Drives
   *  the AdminPanel "Identity vault" section's slot list. Refreshed on
   *  vault write and on manual refresh. */
  liveVault = $state<VaultRecord | null>(null);
  /** v0.4.76: hubs that a vault push failed to reach. Surfaced as an
   *  admin banner with a retry button. Cleared on next successful
   *  saveVault to those hubs. */
  vaultPushFailures = $state<string[]>([]);

  // ---------------------------------------------------------------------
  // Multi-hub state (Phase 2). Phase 1 held a single `hub`; Phase 2
  // grows this into a Map keyed by hub URL with an activeHubUrl pointer.
  // All the backward-compat delegator getters/methods below route through
  // `activeHub` so the 67 consumer .svelte sites don't need changes.
  // ---------------------------------------------------------------------

  hubs = $state<Map<string, HubConnection>>(new Map());
  activeHubUrl = $state<string | null>(null);
  /** v0.4.69: when true, +page.svelte renders AddHubPanel as a modal
   *  overlay over ThreadView. Toggled by the sidebar switcher's
   *  "+ Add another hub" button and by AddHubPanel's own close/submit. */
  addHubOpen = $state<boolean>(false);

  /** The currently-focused HubConnection. Derived so consumers can
   *  reactively track hub-switching. */
  get activeHub(): HubConnection | null {
    if (this.activeHubUrl === null) return null;
    return this.hubs.get(this.activeHubUrl) ?? null;
  }
  /** Back-compat alias. Phase 1 test code uses `app.hub`; keep it
   *  pointing at the same instance as `activeHub`. */
  get hub(): HubConnection | null { return this.activeHub; }
  set hub(v: HubConnection | null) {
    // Used only by the Phase 1 state.test.ts smoke. In production the
    // Map machinery is the source of truth. Kept behavior-compatible:
    // assigning null disposes the active hub; assigning a HubConnection
    // registers it under whatever URL it already carries (or a synthetic
    // key if it doesn't have one — the tests build a hub without an
    // authenticate() call).
    if (v === null) {
      this.reset();
      return;
    }
    const url = v.hubUrl || '__unattached__';
    this.hubs.set(url, v);
    this.activeHubUrl = url;
  }

  constructor() {
    // Resolve the bundle version asynchronously.
    appVersion().then((v) => { this.appVersion = v; });
    if (!this.inTauri) {
      void this.refreshVaultStatus();
      void requestPersistentStorage();
      // v0.4.74: PWA-only Passkey path. Feature-detect + read the
      // persisted status. If the browser doesn't support PRF, the
      // status field stays exists:false and the UI treats it like
      // no Passkey.
      void this.refreshPasskeySupport();
      void this.refreshPasskeyStatus();
    }
    // v0.4.69: migrate legacy single-hub localStorage keys and restore
    // the persisted hub list as unauthenticated placeholders. The user
    // unlocks per-hub through AuthPanel; the placeholders just tell the
    // sidebar switcher which hubs the user has ever joined.
    this.restoreHubsFromStorage();
  }

  /** v0.4.69: idempotent boot-time restore. Safe to call multiple times;
   *  won't clobber a hub that's already been authenticated in this
   *  session. */
  private restoreHubsFromStorage(): void {
    migrateLegacyKeys();
    for (const url of loadHubUrls()) {
      if (!this.hubs.has(url)) {
        const hub = new HubConnection(this, url);
        hub.thread = loadThreadFor(url) ?? hub.thread;
        this.hubs.set(url, hub);
      }
    }
    const stored = loadActiveHubUrl();
    if (stored && this.hubs.has(stored)) {
      this.activeHubUrl = stored;
    } else if (this.activeHubUrl === null && this.hubs.size > 0) {
      // Pick any restored hub as active if we don't have a stored pointer.
      this.activeHubUrl = this.hubs.keys().next().value ?? null;
    }
  }

  // ---------------------------------------------------------------------
  // Delegating getters — per-hub state proxied through the active hub.
  // `this.hub` is a backward-compat alias for `this.activeHub`; both
  // point at the same instance. The 67 consumer .svelte sites keep
  // working unchanged.
  // ---------------------------------------------------------------------

  get authStatus(): AuthStatus {
    return this.hub?.authStatus ?? { kind: 'unauthenticated' };
  }
  get thread(): string { return this.hub?.thread ?? 'annual-meeting'; }
  get threadStatus(): ThreadStatus {
    return this.hub?.threadStatus ?? { kind: 'idle' };
  }
  get entries(): VerifiedEntry[] { return this.hub?.entries ?? []; }
  get inboxRows(): InboxRow[] { return this.hub?.inboxRows ?? []; }
  get inboxStatus(): { kind: 'idle' } | { kind: 'loading' }
    | { kind: 'error'; message: string } {
    return this.hub?.inboxStatus ?? { kind: 'idle' };
  }
  get threads(): ThreadSummary[] { return this.hub?.threads ?? []; }
  get replyOpen(): VerifiedEntry | null { return this.hub?.replyOpen ?? null; }
  get view(): View { return this.hub?.view ?? 'messages'; }
  get pendingQueue(): Array<{
    pubkey: string; name_hint: string; requested_at: string;
  }> {
    return this.hub?.pendingQueue ?? [];
  }
  get adminStatus(): { kind: 'idle' } | { kind: 'submitting' }
    | { kind: 'error'; message: string } {
    return this.hub?.adminStatus ?? { kind: 'idle' };
  }
  get myAttestation(): Attestation | null { return this.hub?.myAttestation ?? null; }
  get manifest(): DirectoryManifest | null { return this.hub?.manifest ?? null; }
  get members(): Attestation[] { return this.hub?.members ?? []; }
  get revoked(): RevokedEntry[] { return this.hub?.revoked ?? []; }
  get invites(): Invite[] { return this.hub?.invites ?? []; }
  get invitesStatus(): { kind: 'idle' } | { kind: 'loading' }
    | { kind: 'error'; message: string } {
    return this.hub?.invitesStatus ?? { kind: 'idle' };
  }
  /** newThreadDialog is the one field consumer code MUTATES via nested
   *  field write (`app.newThreadDialog.name = name` in ThreadList.svelte).
   *  Svelte 5's $state recurses into object literals so mutations to the
   *  returned object DO propagate reactively through this getter. */
  get newThreadDialog() { return this.hub?.newThreadDialog ?? null; }
  set newThreadDialog(v) {
    if (this.hub) this.hub.newThreadDialog = v;
  }
  get client(): Client | null { return this.hub?.client ?? null; }
  get isBoardMember(): boolean { return this.hub?.isBoardMember ?? false; }

  // ---------------------------------------------------------------------
  // Delegating methods — per-hub ops proxied through the active hub
  // ---------------------------------------------------------------------

  openReplyPanel(ve: VerifiedEntry): void { this.hub?.openReplyPanel(ve); }
  closeReplyPanel(): void { this.hub?.closeReplyPanel(); }
  setView(v: View): void { this.hub?.setView(v); }

  async loadPendingQueue(): Promise<void> { await this.hub?.loadPendingQueue(); }
  async rejectPending(pubkey: string): Promise<void> {
    await this.hub?.rejectPending(pubkey);
  }
  async approvePending(opts: {
    pubkey: string;
    displayName: string;
    affiliation: string;
    role: 'member' | 'officer' | 'board' | string;
    title?: string | null;
  }): Promise<void> {
    await this.hub?.approvePending(opts);
  }
  /** v0.4.71: attest an arbitrary pubkey. Same shape as
   *  approvePending but doesn't require the pubkey to be in the
   *  pending queue. Used for federation and manual member add. */
  async attestPubkey(opts: {
    pubkey: string;
    displayName: string;
    affiliation: string;
    role: 'member' | 'officer' | 'board' | string;
    title?: string | null;
  }): Promise<void> {
    await this.hub?.attestPubkey(opts);
  }
  async updateMember(opts: {
    pubkey: string;
    displayName: string;
    affiliation: string;
    role: 'member' | 'officer' | 'board' | string;
    title?: string | null;
  }): Promise<void> {
    await this.hub?.updateMember(opts);
  }
  async revokeMember(opts: { pubkey: string; reason: string }): Promise<void> {
    await this.hub?.revokeMember(opts);
  }
  async setCapabilitiesByRole(next: Record<string, string[]> | null): Promise<void> {
    await this.hub?.setCapabilitiesByRole(next);
  }
  async saveGroups(next: KeypairGroup[] | null): Promise<void> {
    await this.hub?.saveGroups(next);
  }
  async setMemberTier(opts: {
    pubkey: string;
    tier: 'member' | 'officer' | 'board';
  }): Promise<void> {
    await this.hub?.setMemberTier(opts);
  }
  async loadInvites(): Promise<void> { await this.hub?.loadInvites(); }
  async mintInvite(opts: {
    ttlSeconds: number;
    nameHint?: string;
  }): Promise<Invite | null> {
    return (await this.hub?.mintInvite(opts)) ?? null;
  }
  async revokeInvite(code: string): Promise<void> { await this.hub?.revokeInvite(code); }
  async setDefaultThread(newDefault: string | null): Promise<string | null> {
    if (!this.hub) throw new Error('not connected');
    return await this.hub.setDefaultThread(newDefault);
  }
  hasCapability(cap: string): boolean { return this.hub?.hasCapability(cap) ?? false; }
  openNewThreadDialog(): void { this.hub?.openNewThreadDialog(); }
  closeNewThreadDialog(): void { this.hub?.closeNewThreadDialog(); }
  toggleNewThreadMember(pubkey: string): void {
    this.hub?.toggleNewThreadMember(pubkey);
  }
  addGroupToNewThread(pubkeys: readonly string[]): void {
    this.hub?.addGroupToNewThread(pubkeys);
  }
  async submitNewThread(): Promise<void> { await this.hub?.submitNewThread(); }
  isThreadArchived(name: string): boolean { return this.hub?.isThreadArchived(name) ?? false; }
  threadAudience(name: string): { pubkeys: string[] } | null {
    return this.hub?.threadAudience(name) ?? null;
  }
  async syncAndSubscribe(): Promise<void> { await this.hub?.syncAndSubscribe(); }
  async branchOff(newThread: string, body: string, files: File[] = []): Promise<void> {
    await this.hub?.branchOff(newThread, body, files);
  }
  async setThreadArchived(thread: string, archived: boolean,
                          rationale: string): Promise<void> {
    await this.hub?.setThreadArchived(thread, archived, rationale);
  }
  async setThreadAudience(thread: string, pubkeys: string[]): Promise<void> {
    await this.hub?.setThreadAudience(thread, pubkeys);
  }
  async createDirectThread(opts: {
    thread: string;
    pubkeys: string[];
    message: string;
  }): Promise<void> {
    await this.hub?.createDirectThread(opts);
  }
  async post(body: string, files: File[] = [],
             replyTo: VerifiedEntry | null = null): Promise<void> {
    await this.hub?.post(body, files, replyTo);
  }
  async loadThreads(): Promise<void> { await this.hub?.loadThreads(); }
  async loadInbox(): Promise<void> { await this.hub?.loadInbox(); }
  async goToInbox(): Promise<void> { await this.hub?.goToInbox(); }
  async switchThread(name: string): Promise<void> { await this.hub?.switchThread(name); }

  // ---------------------------------------------------------------------
  // Global methods (stay on AppState — device custody, updater, UI prefs,
  // onboarding, and the connect factory that constructs a HubConnection)
  // ---------------------------------------------------------------------

  /** v0.4.34: read the IndexedDB vault status into reactive state. */
  async refreshVaultStatus(): Promise<void> {
    this.vaultStatus = await readVaultStatus();
  }

  /** v0.4.74: read the persisted Passkey record. */
  async refreshPasskeyStatus(): Promise<void> {
    this.passkeyStatus = await readPasskeyStatus();
  }

  /** v0.4.74: feature-detect Passkey + PRF support. */
  async refreshPasskeySupport(): Promise<void> {
    this.passkeySupported = await detectPasskeySupported();
  }

  /** Re-read the keychain status. Call on app load. No-op outside Tauri. */
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
   *  unsigned-macOS-app silent-no-op), throw loud. */
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

  /** Wipe the keychain. */
  async clearKeychain(): Promise<void> {
    if (!this.inTauri) return;
    await keychain.clear();
    await this.refreshKeychain();
  }

  /** v0.4.73: current active hub's org pubkey, used to scope root-key
   *  operations to the correct hub. Null when no active hub is loaded
   *  or the manifest hasn't come back yet — root ops fall back to the
   *  legacy un-suffixed keychain slot in that case. */
  private activeOrgKey(): string | undefined {
    return this.activeHub?.manifest?.org;
  }

  /** Re-read the root keychain slot for the active hub. Sets
   *  `rootKeysPresent` to reflect whether *the currently-active hub's*
   *  root key is loaded. */
  async refreshRootKeychain(): Promise<void> {
    if (!this.inTauri) { this.rootKeysPresent = false; return; }
    const st = await rootKeychain.status(this.activeOrgKey());
    this.rootKeysPresent = st.has_keys;
  }

  /** Import the org root keypair into the active hub's dedicated slot.
   *  Multiple hubs each get their own slot keyed by org pubkey. */
  async importRootKeys(privateKey: string, publicKey: string): Promise<void> {
    if (!this.inTauri) throw new Error('root key custody requires the Tauri shell');
    await rootKeychain.import(privateKey, publicKey, this.activeOrgKey());
    await this.refreshRootKeychain();
    if (!this.rootKeysPresent) {
      throw new Error(
        'Root key import did not persist. The OS keychain returned OK '
        + 'but a subsequent read returned no entry. Check Console.app '
        + '(macOS) or the keyring logs for details.',
      );
    }
  }

  /** Wipe the root slot for the active hub. Other hubs' root keys are
   *  untouched. */
  async clearRootKeys(): Promise<void> {
    if (!this.inTauri) return;
    await rootKeychain.clear(this.activeOrgKey());
    await this.refreshRootKeychain();
  }

  /**
   * Quietly check the updater feed. Call once on app load; the feed
   * URL + pubkey live in tauri.conf.json. A signature-verification
   * failure is surfaced as an error — the rest of the chain (no
   * network, no update available) is silent so the UI doesn't shout
   * about the everyday case.
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
    } catch (err) {
      this.updateStatus = { kind: 'error', message: errMsg(err) };
    }
  }

  /** v0.4.11: flip Cards ↔ Chat rendering mode. */
  setViewMode(mode: 'cards' | 'chat'): void {
    this.viewMode = mode;
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('cove.viewMode', mode);
    }
  }

  /** v0.4.45: toggle the sidebar. */
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
  /** v0.4.58: complements closeSidebar. */
  openSidebar(): void {
    if (this.sidebarOpen) return;
    this.sidebarOpen = true;
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('cove.sidebarOpen', 'true');
    }
  }

  /** v0.4.34: persist a freshly-generated keypair under a passphrase. */
  async storeKeyInVault(opts: {
    priv: string;
    pub: string;
    passphrase: string;
  }): Promise<void> {
    await storeVaultKey(opts);
    await this.refreshVaultStatus();
  }

  /** v0.4.34: wipe the vault. */
  async forgetVault(): Promise<void> {
    await clearVault();
    await this.refreshVaultStatus();
  }

  // ---------------------------------------------------------------------
  // Reset — dispose all HubConnections
  // ---------------------------------------------------------------------

  /** v0.4.69: dispose every joined hub, clear the Map, clear the
   *  in-memory priv material, wipe the persisted hub list. Semantically
   *  a "log out of everything". */
  logoutAll() {
    for (const hub of this.hubs.values()) hub.dispose();
    this.hubs = new Map();
    this.activeHubUrl = null;
    this.livePriv = null;
    // v0.4.76: wipe vault-derived material alongside livePriv. CEK is the
    // key that unlocks the identity priv from any Cove vault the user is
    // signed into; leaking it past logout would defeat the point.
    this.liveCek = null;
    this.liveVault = null;
    this.vaultPushFailures = [];
    saveHubUrls([]);
    saveActiveHubUrl(null);
  }
  /** Backward-compat alias. Phase 1 tests + any existing caller use
   *  `reset()`; behavior is now "log out of everything." */
  reset() { this.logoutAll(); }

  // ---------------------------------------------------------------------
  // Multi-hub ops (Phase 2)
  // ---------------------------------------------------------------------

  /** v0.4.69: get-or-create the HubConnection for `hubUrl`. Idempotent
   *  — calling with a URL that's already in the Map returns the existing
   *  instance without disturbing its state. New URLs are added to
   *  localStorage so the placeholder shows up in the switcher across
   *  reloads even before the user authenticates. */
  addHub(hubUrl: string): HubConnection {
    const existing = this.hubs.get(hubUrl);
    if (existing) return existing;
    const hub = new HubConnection(this, hubUrl);
    hub.thread = loadThreadFor(hubUrl) ?? hub.thread;
    this.hubs.set(hubUrl, hub);
    saveHubUrls([...this.hubs.keys()]);
    return hub;
  }

  /** v0.4.69: focus the switcher on the given hub. Persists the choice
   *  so the next launch restores the same active hub. No-op if `hubUrl`
   *  isn't in the Map (caller should addHub first).
   *
   *  v0.4.70: if the target hub isn't authenticated this session AND
   *  we have a live signer on some other hub, silently attempt to
   *  authenticate the target before switching. Prevents the "click
   *  unauth hub → kicked to AuthPanel and lose the working session"
   *  UX trap. Only swaps activeHubUrl on success; on failure the
   *  current authenticated hub stays active and the caller sees the
   *  target hub's authStatus flip to 'failed' for surfacing. */
  async switchToHub(hubUrl: string): Promise<void> {
    const hub = this.hubs.get(hubUrl);
    if (!hub) return;
    if (this.activeHubUrl === hubUrl) return;

    // Fast path: hub is already authenticated → plain switch.
    if (hub.authStatus.kind === 'authenticated') {
      this.activeHubUrl = hubUrl;
      saveActiveHubUrl(hubUrl);
      // v0.4.73: rootKeysPresent is per-hub; refresh so admin UI
      // shows the correct import/clear state for the newly-active hub.
      void this.refreshRootKeychain();
      return;
    }

    // Slow path: target is unauth. Only silent re-auth if we HAVE a
    // live signer (either Tauri keychain or PWA/paste livePriv AND a
    // currently-authenticated activeHub to source the pubkey from).
    const current = this.activeHub;
    const canReuseSigner =
      current !== null
      && current.authStatus.kind === 'authenticated'
      && (this.inTauri || this.livePriv !== null);
    if (!canReuseSigner) {
      // No live session anywhere — swap and let AuthPanel drive auth.
      this.activeHubUrl = hubUrl;
      saveActiveHubUrl(hubUrl);
      void this.refreshRootKeychain();
      return;
    }

    const currentPubkey = (current.authStatus as { pubkey: string }).pubkey;
    await hub.authenticate({
      hubUrl,
      publicKey: currentPubkey,
      thread: hub.thread || 'annual-meeting',
      mode: this.inTauri ? 'keychain' : 'paste',
      privateKey: this.inTauri ? undefined : this.livePriv ?? undefined,
    });
    // The awaited authenticate() call above mutated hub.authStatus in
    // place — re-read it after the fact. TS's narrowing from the
    // earlier `!== 'authenticated'` guard is stale here.
    const postAuthKind = (hub.authStatus as AuthStatus).kind;
    if (postAuthKind === 'authenticated') {
      this.activeHubUrl = hubUrl;
      saveActiveHubUrl(hubUrl);
      void this.refreshRootKeychain();
    }
    // On failure, activeHubUrl stays put — the user keeps their
    // working session. The target hub's authStatus flipped to
    // 'failed' with the hub's error message; the sidebar row will
    // reflect that on the next paint.
  }

  /** v0.4.69: dispose the given hub and drop it from the Map + storage.
   *  If the removed hub was active, fall back to any remaining hub
   *  (arbitrary pick) or leave the session unauthenticated. */
  removeHub(hubUrl: string): void {
    const hub = this.hubs.get(hubUrl);
    if (!hub) return;
    hub.dispose();
    this.hubs.delete(hubUrl);
    removeHubUrl(hubUrl);
    if (this.activeHubUrl === hubUrl) {
      const fallback = this.hubs.keys().next().value ?? null;
      this.activeHubUrl = fallback;
      saveActiveHubUrl(fallback);
    }
  }

  // ---------------------------------------------------------------------
  // Onboarding (top-level; precedes any HubConnection)
  // ---------------------------------------------------------------------

  /**
   * v0.4.0 onboarding entry point. Generates a fresh keypair on-device,
   * registers it as pending on the hub, and holds a WebSocket open until
   * the keymaster issues the attestation. On 'attested' push, connects
   * to the hub.
   */
  async generateAndPair(opts: {
    hubUrl: string;
    nameHint: string;
    thread: string;
    invite: string;
    /** v0.4.34: PWA / browser path. When provided, the generated priv is
     *  encrypted under this passphrase and stored in IndexedDB BEFORE
     *  the /pending POST — so a reload during the waiting screen still
     *  recovers the keys via the unlock flow. */
    passphrase?: string;
  }): Promise<void> {
    this.onboardStatus = { kind: 'generating' };
    let pubkey: string;
    let privForPaste: string | null = null;

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
      // 409 already_attested → fast-forward.
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
      this.watchCancel = null;
      this.onboardStatus = { kind: 'attested', pubkey };
      await this.connect({
        hubUrl: opts.hubUrl, publicKey: pubkey,
        thread: opts.thread,
        mode: this.inTauri ? 'keychain' : 'paste',
        privateKey: privForPaste ?? undefined,
      });
      // v0.4.76: if this was a PWA passphrase onboard, mint a hub-stored
      // vault so device #2 can sign in without a fresh invite. Best-
      // effort — a failure to push the vault doesn't undo the successful
      // onboard. Any failure surfaces via vaultPushFailures.
      if (!this.inTauri && opts.passphrase && privForPaste) {
        try {
          await this.createIdentityVault({
            firstUnlock: {
              kind: 'passphrase', passphrase: opts.passphrase,
              label: 'Onboarding passphrase',
            },
          });
        } catch (err) {
          console.warn('vault mint after onboard failed:', errMsg(err));
        }
      }
      this.pwaTransientPriv = null;
    } catch (err) {
      if (this.watchCancel !== null) {
        this.onboardStatus = {
          kind: 'error',
          message: `Watch failed: ${errMsg(err)}`,
        };
      }
      this.watchCancel = null;
    }
  }

  /** v0.4.74: onboarding via a fresh Passkey. Registers a WebAuthn
   *  credential with the PRF extension, derives a deterministic
   *  Ed25519 keypair from the PRF output, then runs the standard
   *  registerPending → watchPending → connect flow. Cross-device: the
   *  Passkey syncs via iCloud Keychain / Google Password Manager, and
   *  every device with the same Passkey derives the same keypair here.
   *
   *  The Passkey ceremony's biometric/PIN prompt is user-triggered
   *  (this method is called from the OnboardingPanel's Passkey card
   *  submit button), so the browser accepts the create() call. */
  async generateAndPairWithPasskey(opts: {
    hubUrl: string;
    nameHint: string;
    thread: string;
    invite: string;
  }): Promise<void> {
    this.onboardStatus = { kind: 'generating' };
    let pubkey: string;
    let priv: string;
    try {
      const registered = await registerPasskey();
      priv = registered.priv;
      pubkey = registered.pub;
      this.pwaTransientPriv = priv;
      await this.refreshPasskeyStatus();
    } catch (err) {
      this.onboardStatus = {
        kind: 'error',
        message: `Passkey creation failed: ${errMsg(err)}`,
      };
      return;
    }

    // From here on the flow mirrors generateAndPair()'s tail —
    // client construction, registerPending, watchPending, connect. Kept
    // as duplicated code (not extracted into a shared helper) because
    // the Passkey path is simple enough to read straight through, and
    // the two flows will likely diverge over time (deep-link handling,
    // Passkey-specific error reporting, etc.).
    const client = new Client({
      hubUrl: opts.hubUrl, publicKey: pubkey,
      privateKey: priv,
    });
    try {
      await client.registerPending({
        pubkey, nameHint: opts.nameHint, invite: opts.invite,
      });
    } catch (err) {
      if (errMsg(err) === 'already_attested') {
        this.onboardStatus = { kind: 'attested', pubkey };
        await this.connect({
          hubUrl: opts.hubUrl, publicKey: pubkey,
          thread: opts.thread, mode: 'paste', privateKey: priv,
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
      this.watchCancel = null;
      this.onboardStatus = { kind: 'attested', pubkey };
      await this.connect({
        hubUrl: opts.hubUrl, publicKey: pubkey,
        thread: opts.thread, mode: 'paste', privateKey: priv,
      });
      this.pwaTransientPriv = null;
    } catch (err) {
      if (this.watchCancel !== null) {
        this.onboardStatus = {
          kind: 'error',
          message: `Watch failed: ${errMsg(err)}`,
        };
      }
      this.watchCancel = null;
    }
  }

  /** User backed out of the waiting screen. */
  cancelOnboarding(): void {
    if (this.watchCancel) {
      this.watchCancel();
      this.watchCancel = null;
    }
    this.onboardStatus = { kind: 'idle' };
  }

  /** v0.4.74: PWA / browser-only — sign in with a Passkey. Derives the
   *  Ed25519 keypair from the Passkey's PRF output (deterministic per
   *  Passkey), then hands off to the same `connect` path as vault
   *  unlock. Cross-device: any device with the same synced Passkey
   *  derives the same priv here. */
  async unlockFromPasskey(opts: {
    hubUrl: string;
    thread: string;
  }): Promise<void> {
    if (!this.passkeyStatus.exists) {
      throw new Error('no Passkey on this device — create one first');
    }
    const { priv, pub } = await unlockWithPasskey();
    this.pwaTransientPriv = priv;
    await this.connect({
      hubUrl: opts.hubUrl, privateKey: priv, publicKey: pub,
      thread: opts.thread, mode: 'paste',
    });
    this.pwaTransientPriv = null;
  }

  /** v0.4.74: wipe the Passkey record from this device's IndexedDB.
   *  Does NOT delete the actual Passkey on the platform (WebAuthn
   *  doesn't expose that API). Users who want the Passkey gone
   *  entirely need to delete it via OS Settings > Passwords/Passkeys. */
  async clearPasskey(): Promise<void> {
    await clearPasskeyStorage();
    await this.refreshPasskeyStatus();
  }

  /** v0.4.34: PWA / browser-only — decrypt the vault with the user's
   *  passphrase and connect. */
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

  // ---------------------------------------------------------------------
  // Cove identity vault (v0.4.76): hub-stored, multi-recipient encrypted
  // ---------------------------------------------------------------------

  /** Mint a NEW hub-stored vault around the current session's priv/pub.
   *  Requires `livePriv` (must already be signed in). Pushes to every
   *  joined authenticated hub. On success, stores liveCek + liveVault.
   *
   *  Called from OnboardingPanel after registerPending + attestation
   *  succeed — the priv is generated locally in-JS, this method wraps
   *  it into a vault so device #2 can sign in later. */
  async createIdentityVault(opts: {
    firstUnlock: FirstUnlockChoice;
    note?: string;
  }): Promise<void> {
    if (this.livePriv === null || this.activeHub === null
        || this.activeHub.authStatus.kind !== 'authenticated') {
      throw new Error('createIdentityVault requires an authenticated session');
    }
    const pub = (this.activeHub.authStatus as { pubkey: string }).pubkey;
    const vault = await vaultCreate({
      priv: this.livePriv, pub,
      firstUnlock: opts.firstUnlock,
      note: opts.note,
    });
    // Unwrap the CEK we just used so slot mutations don't need a
    // second unlock ceremony.
    const cek = opts.firstUnlock.kind === 'passphrase'
      ? (await vaultUnlockPassphrase({ vault, passphrase: opts.firstUnlock.passphrase })).cek
      : (await vaultUnlockPasskey({ vault })).cek;
    this.liveVault = vault;
    this.liveCek = cek;
    await this.saveVault(vault);
  }

  /** Fetch the vault for `pubkey` from any joined hub. Tries active hub
   *  first, then the rest. Divergence resolution: if two hubs return
   *  different heads, chain-follows-chain wins (a hub whose head chains
   *  from another's head is strictly later). Falls back to
   *  highest-`updated_at` when chains are unrelated. */
  async loadIdentityVault(pubkey: string): Promise<VaultRecord | null> {
    const candidates: VaultRecord[] = [];
    const active = this.activeHub;
    const ordered = active
      ? [active, ...[...this.hubs.values()].filter((h) => h !== active)]
      : [...this.hubs.values()];
    for (const hub of ordered) {
      if (!hub.client) continue;
      try {
        const v = await hub.client.fetchVault(pubkey);
        if (v) candidates.push(v);
      } catch {
        // Skip this hub; another may return a valid vault.
      }
    }
    if (candidates.length === 0) return null;
    if (candidates.length === 1) return candidates[0];
    // Chain-follows-chain: does any candidate's prev_vault_hash equal
    // another's hash? If so, the descendant wins regardless of clock.
    const { hashVault } = await import('./vault-blob');
    const hashes = await Promise.all(candidates.map(hashVault));
    for (let i = 0; i < candidates.length; i++) {
      if (candidates.some((c, j) => j !== i && hashes[j] === c.prev_vault_hash)) {
        return candidates[i];
      }
    }
    // Fall back to clock. Client-authored `updated_at` is untrusted but
    // signed under the vault-owner priv, so an evil-clock attack still
    // needs the priv.
    candidates.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    return candidates[0];
  }

  /** Push a vault to every authenticated hub. Per-hub retries on
   *  StaleVaultError (pull-merge-retry, capped at 3 attempts). Partial
   *  failures are surfaced via vaultPushFailures; total failure throws. */
  async saveVault(vault: VaultRecord): Promise<void> {
    const authedHubs = [...this.hubs.values()]
      .filter((h) => h.client !== null
                     && h.authStatus.kind === 'authenticated');
    if (authedHubs.length === 0) {
      throw new Error('no authenticated hubs to push vault to');
    }
    const results = await Promise.allSettled(
      authedHubs.map((h) => this._pushVaultToHubWithRetry(h, vault)),
    );
    const failed: string[] = [];
    for (let i = 0; i < authedHubs.length; i++) {
      if (results[i].status === 'rejected') {
        failed.push(authedHubs[i].hubUrl);
      }
    }
    this.vaultPushFailures = failed;
    if (failed.length === authedHubs.length) {
      throw new Error(`vault push failed on every hub (${failed.length})`);
    }
    // Stash the winning vault as the current head. On a partial failure
    // the local liveVault reflects the version that landed on at least
    // one hub; a subsequent successful saveVault re-syncs the stragglers.
    this.liveVault = vault;
  }

  private async _pushVaultToHubWithRetry(
    hub: HubConnection,
    vault: VaultRecord,
  ): Promise<void> {
    if (hub.client === null) throw new Error('hub has no client');
    let current = vault;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        await hub.client.pushVault(current);
        return;
      } catch (err) {
        if (err instanceof StaleVaultError) {
          // Someone else wrote in the meantime. Fetch the fresh head from
          // this hub, replay OUR slot delta onto it, re-sign, retry.
          const remote = await hub.client.fetchVault(current.pubkey);
          if (!remote || this.livePriv === null) throw err;
          const { signVault: sign } = await import('./vault-blob');
          const { hashVault: hash } = await import('./vault-blob');
          const remoteHash = await hash(remote);
          const { sig: _oldSig, ...rest } = current;
          current = await sign(
            {
              ...rest,
              prev_vault_hash: remoteHash,
              updated_at: new Date().toISOString(),
            },
            this.livePriv,
          );
          continue;
        }
        throw err;
      }
    }
    throw new Error(`vault push to ${hub.hubUrl} kept losing CAS after 3 attempts`);
  }

  /** Add a passphrase unlock method to the current identity's vault.
   *  Requires liveVault + liveCek + livePriv (i.e. currently signed in
   *  via SOME unlock method). Rewraps the existing CEK with a KEK derived
   *  from the new passphrase — content ciphertext untouched. */
  async addPassphraseUnlock(opts: {
    passphrase: string;
    label: string;
  }): Promise<void> {
    if (this.liveVault === null || this.liveCek === null
        || this.livePriv === null) {
      throw new Error('need a signed-in vault to add a passphrase unlock');
    }
    const next = await vaultAddPassphraseSlot({
      vault: this.liveVault,
      cek: this.liveCek,
      ownerPriv: this.livePriv,
      passphrase: opts.passphrase,
      label: opts.label,
    });
    await this.saveVault(next);
  }

  /** Add a Passkey unlock method to the current identity's vault. Runs
   *  a fresh WebAuthn ceremony (user gets a "Save Passkey" prompt), then
   *  wraps the existing CEK under the PRF-derived KEK. */
  async addPasskeyUnlock(label: string): Promise<void> {
    if (this.liveVault === null || this.liveCek === null
        || this.livePriv === null) {
      throw new Error('need a signed-in vault to add a Passkey unlock');
    }
    const next = await vaultAddPasskeySlot({
      vault: this.liveVault,
      cek: this.liveCek,
      ownerPriv: this.livePriv,
      label,
    });
    await this.saveVault(next);
  }

  /** Remove an unlock method by slot id. Refuses to drop the last slot
   *  (vault-blob enforces it too — belt-and-suspenders). */
  async removeUnlock(slotId: string): Promise<void> {
    if (this.liveVault === null || this.livePriv === null) {
      throw new Error('need a signed-in vault to remove an unlock method');
    }
    const next = await vaultRemoveSlot({
      vault: this.liveVault,
      slotId,
      ownerPriv: this.livePriv,
    });
    await this.saveVault(next);
  }

  /** Sign in on a fresh device via passphrase unlock of a hub-stored
   *  vault. Fetches the vault from `hubUrl`, decrypts with `passphrase`,
   *  then routes through the normal connect() flow. */
  async unlockFromIdentityVaultPassphrase(opts: {
    hubUrl: string;
    pubkey: string;
    passphrase: string;
    thread: string;
  }): Promise<void> {
    // Fetch the vault directly (public GET, no auth needed).
    const { Client } = await import('./client');
    const client = new Client({
      hubUrl: opts.hubUrl, publicKey: opts.pubkey, privateKey: '00'.repeat(32),
    });
    const vault = await client.fetchVault(opts.pubkey);
    if (!vault) throw new Error(`no vault at ${opts.hubUrl} for that pubkey`);
    const unlocked = await vaultUnlockPassphrase({
      vault, passphrase: opts.passphrase,
    });
    this.livePriv = unlocked.priv;
    this.liveCek = unlocked.cek;
    this.liveVault = vault;
    await this.connect({
      hubUrl: opts.hubUrl, privateKey: unlocked.priv, publicKey: unlocked.pub,
      thread: opts.thread, mode: 'paste',
    });
  }

  /** Sign in on a fresh device via Passkey unlock of a hub-stored vault.
   *  Passkey ceremony runs against the vault's stored credential IDs
   *  (all Passkey slots offered to the OS picker at once). */
  async unlockFromIdentityVaultPasskey(opts: {
    hubUrl: string;
    pubkey: string;
    thread: string;
  }): Promise<void> {
    const { Client } = await import('./client');
    const client = new Client({
      hubUrl: opts.hubUrl, publicKey: opts.pubkey, privateKey: '00'.repeat(32),
    });
    const vault = await client.fetchVault(opts.pubkey);
    if (!vault) throw new Error(`no vault at ${opts.hubUrl} for that pubkey`);
    const unlocked = await vaultUnlockPasskey({ vault });
    this.livePriv = unlocked.priv;
    this.liveCek = unlocked.cek;
    this.liveVault = vault;
    await this.connect({
      hubUrl: opts.hubUrl, privateKey: unlocked.priv, publicKey: unlocked.pub,
      thread: opts.thread, mode: 'paste',
    });
  }

  // ---------------------------------------------------------------------
  // Connect — the single HubConnection factory point
  // ---------------------------------------------------------------------

  /**
   * Connect to a hub. Two paths:
   *
   *   - browser / paste mode: caller provides privateKey, wrapped as
   *     InJSSigner inside Client.
   *   - Tauri / keychain mode: the private key is already in the OS
   *     keychain; caller passes mode='keychain' and the publicKey. Signing
   *     roundtrips through Rust.
   */
  async connect(opts: ConnectOpts): Promise<void> {
    // v0.4.69: route through addHub so the URL lands in the switcher
    // list + localStorage; then switch to it and authenticate. If the
    // user is re-authenticating an existing hub, addHub returns the
    // existing instance and we auth into it in place.
    if (opts.mode === 'paste' && opts.privateKey) {
      // Remember the priv for a subsequent add-hub-mode flow. Same
      // threat-model as today's paste-mode session.
      this.livePriv = opts.privateKey;
    }
    const hub = this.addHub(opts.hubUrl);
    this.switchToHub(opts.hubUrl);
    await hub.authenticate(opts);
  }
}
