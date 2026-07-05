<!--
  Keymaster admin panel — v0.4.0.

  Visible only when:
    - the caller is authenticated as a board-role member, AND
    - they're inside the Tauri shell (root key custody requires the
      OS keychain).

  Three states:
    1. Root keys not loaded → show root key import form (one-time setup).
    2. Root keys loaded, queue empty → idle state with refresh.
    3. Root keys loaded, queue non-empty → approve form per row.

  Approving a row signs a fresh Attestation + DirectoryManifest with
  root.priv (in Rust, via rootKeychain.signMessage) and POSTs to
  /admin/attest. The hub's attest hook fires WS /pending/watch, so
  the member's device unlocks instantly.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { authorColor, initials } from '$lib/cove/chat';
  import { hubLabel } from '$lib/cove/hubs';
  import { fingerprint } from '$lib/cove/pairing';
  import { sanitizeThreadName } from '$lib/cove/threadname';
  import type { AppState } from '$lib/cove/state.svelte';
  import type { Attestation, KeypairGroup } from '$lib/cove/types';
  import {
    CAPABILITIES, DEFAULT_CAPABILITIES_BY_ROLE, type Capability,
  } from '$lib/cove/types';

  interface Props {
    app: AppState;
  }
  let { app }: Props = $props();

  /** v0.4.73: active-hub label for the root-key custody UX. Reactively
   *  updates when the user switches hubs in the sidebar. Falls back to
   *  "this hub" if the URL is missing. */
  const activeHubLabel = $derived(
    app.activeHubUrl ? hubLabel(app.activeHubUrl) : 'this hub',
  );

  /** v0.4.23: per-row edit / revoke state for the membership editor.
   *  Only one row open at a time so the form doesn't compete with
   *  itself across rows. */
  type RowMode = { kind: 'edit' } | { kind: 'revoke' } | { kind: 'limits' };
  let openRow = $state<string | null>(null);   // pubkey of the open row
  let rowMode = $state<RowMode | null>(null);
  let editName = $state('');
  let editAffiliation = $state('');
  let editRole = $state<'member' | 'officer' | 'board'>('member');
  let editTitle = $state('');
  let revokeReason = $state('');
  let limitsTier = $state<'member' | 'officer' | 'board'>('member');

  function openEdit(att: Attestation) {
    openRow = att.member_pubkey;
    rowMode = { kind: 'edit' };
    editName = att.display_name;
    editAffiliation = att.affiliation;
    editRole = (att.role as 'member' | 'officer' | 'board');
    editTitle = att.title ?? '';
  }

  function openRevoke(att: Attestation) {
    openRow = att.member_pubkey;
    rowMode = { kind: 'revoke' };
    revokeReason = '';
  }

  function openLimits(att: Attestation) {
    openRow = att.member_pubkey;
    rowMode = { kind: 'limits' };
    // Default the dropdown to the member's role-derived tier so "Save"
    // without changing anything is a no-op rather than a downgrade.
    limitsTier = (att.role as 'member' | 'officer' | 'board');
  }

  function closeRow() {
    openRow = null;
    rowMode = null;
  }

  async function submitEdit(att: Attestation) {
    await app.updateMember({
      pubkey: att.member_pubkey,
      displayName: editName.trim(),
      affiliation: editAffiliation.trim(),
      role: editRole,
      title: editTitle.trim() || null,
    });
    if (app.adminStatus.kind === 'idle') closeRow();
  }

  async function submitRevoke(att: Attestation) {
    await app.revokeMember({
      pubkey: att.member_pubkey,
      reason: revokeReason.trim(),
    });
    if (app.adminStatus.kind === 'idle') closeRow();
  }

  async function submitLimits(att: Attestation) {
    await app.setMemberTier({ pubkey: att.member_pubkey, tier: limitsTier });
    if (app.adminStatus.kind === 'idle') closeRow();
  }

  function isSelf(att: Attestation): boolean {
    return att.member_pubkey === app.myAttestation?.member_pubkey;
  }

  /** Pretty-print an ISO timestamp for the revoked-tombstone row. */
  function formatRevokedAt(iso: string): string {
    try { return new Date(iso).toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
    }); } catch { return iso.slice(0, 10); }
  }

  /** v0.4.25: roles × capabilities editor. The matrix is derived from
   *  the current manifest (if it has an explicit map) OR the default
   *  fallback OR observed-roles-from-attestations. Edits go into a
   *  draft; Save root-signs a fresh manifest. */
  const observedRoles = $derived.by(() => {
    const set = new Set<string>();
    for (const att of app.members) set.add(att.role);
    const map = app.manifest?.capabilities_by_role ?? DEFAULT_CAPABILITIES_BY_ROLE;
    for (const role of Object.keys(map)) set.add(role);
    // Stable order: alpha, but with default LWCCOA-ish ordering on top
    // so newcomers always see the canonical roles first.
    return Array.from(set).sort((a, b) => {
      const priors: Record<string, number> = { board: 0, officer: 1, member: 2 };
      const pa = priors[a] ?? 99, pb = priors[b] ?? 99;
      if (pa !== pb) return pa - pb;
      return a.localeCompare(b);
    });
  });

  /** Current effective map = manifest's if set, else the default. */
  const effectiveMap = $derived(
    app.manifest?.capabilities_by_role ?? DEFAULT_CAPABILITIES_BY_ROLE,
  );

  /** Draft the user is editing. Initialized lazily on first open of
   *  the editor; null means "not editing right now" (closed/idle). */
  let rolesDraft = $state<Record<string, string[]> | null>(null);

  function openRolesEditor() {
    // Deep-clone so flips don't mutate the cached manifest.
    const next: Record<string, string[]> = {};
    for (const role of observedRoles) {
      next[role] = [...(effectiveMap[role] ?? [])];
    }
    rolesDraft = next;
  }

  function closeRolesEditor() {
    rolesDraft = null;
  }

  function toggleCap(role: string, cap: Capability) {
    if (rolesDraft === null) return;
    const current = new Set(rolesDraft[role] ?? []);
    if (current.has(cap)) current.delete(cap);
    else current.add(cap);
    rolesDraft = { ...rolesDraft, [role]: Array.from(current).sort() };
  }

  let newRole = $state('');
  /** v0.4.26: add a brand-new role to the draft so the keymaster can
   *  define caps for a role BEFORE anyone is attested with it (e.g.
   *  "sales" or "engineering" in a non-LWCCOA org). The role name is
   *  open-namespace, but we sanitize whitespace so a typo'd "Sales "
   *  doesn't end up distinct from "Sales". */
  function addRole() {
    if (rolesDraft === null) return;
    const name = newRole.trim();
    if (!name || name in rolesDraft) return;
    rolesDraft = { ...rolesDraft, [name]: [] };
    newRole = '';
  }

  /** v0.4.26: drop a role from the draft. Existing attestations with
   *  that role string remain valid (their attestation isn't touched —
   *  only the capability mapping is). They simply have no capabilities
   *  under the new manifest. The keymaster can still update those
   *  members' roles via the Membership editor. */
  function removeRole(role: string) {
    if (rolesDraft === null) return;
    const next = { ...rolesDraft };
    delete next[role];
    rolesDraft = next;
  }

  function roleIsObservedInAttestations(role: string): boolean {
    return app.members.some((m) => m.role === role);
  }

  function isDirty(): boolean {
    if (rolesDraft === null) return false;
    // Compare draft to the effective map normalized the same way.
    const allRoles = new Set([
      ...Object.keys(rolesDraft),
      ...Object.keys(effectiveMap),
    ]);
    for (const role of allRoles) {
      const a = [...(rolesDraft[role] ?? [])].sort().join(',');
      const b = [...(effectiveMap[role] ?? [])].sort().join(',');
      if (a !== b) return true;
    }
    return false;
  }

  async function submitRoles() {
    if (rolesDraft === null) return;
    // Drop roles whose caps list is empty AND wasn't explicitly in the
    // current map — keeps the wire form tight.
    const stripped: Record<string, string[]> = {};
    for (const [role, caps] of Object.entries(rolesDraft)) {
      if (caps.length > 0) stripped[role] = [...caps].sort();
    }
    await app.setCapabilitiesByRole(stripped);
    if (app.adminStatus.kind === 'idle') closeRolesEditor();
  }

  // ---- v0.4.64: keypair groups editor -----------------------------------
  // Same editing shape as roles: initialize a draft on open, mutate
  // locally, save re-signs a fresh manifest. Groups are display-name +
  // pubkey list; storage is a KeypairGroup[] on the manifest.
  let groupsDraft = $state<KeypairGroup[] | null>(null);
  const currentGroups = $derived<KeypairGroup[]>(app.manifest?.groups ?? []);

  function openGroupsEditor() {
    groupsDraft = currentGroups.map((g) => ({
      name: g.name,
      member_pubkeys: [...g.member_pubkeys],
    }));
  }
  function closeGroupsEditor() {
    groupsDraft = null;
  }
  function addGroup() {
    if (groupsDraft === null) return;
    groupsDraft = [...groupsDraft, { name: '', member_pubkeys: [] }];
  }
  function removeGroup(idx: number) {
    if (groupsDraft === null) return;
    groupsDraft = groupsDraft.filter((_, i) => i !== idx);
  }
  function updateGroupName(idx: number, name: string) {
    if (groupsDraft === null) return;
    groupsDraft = groupsDraft.map((g, i) => (i === idx ? { ...g, name } : g));
  }
  function toggleMemberInGroup(idx: number, pubkey: string) {
    if (groupsDraft === null) return;
    groupsDraft = groupsDraft.map((g, i) => {
      if (i !== idx) return g;
      const set = new Set(g.member_pubkeys);
      if (set.has(pubkey)) set.delete(pubkey);
      else set.add(pubkey);
      return { ...g, member_pubkeys: Array.from(set) };
    });
  }
  function groupsDirty(): boolean {
    if (groupsDraft === null) return false;
    // Canonical compare: sorted name + sorted pubkeys per group.
    const key = (gs: KeypairGroup[]) => gs
      .map((g) => `${g.name}\0${[...g.member_pubkeys].sort().join(',')}`)
      .sort()
      .join('|');
    return key(groupsDraft) !== key(currentGroups);
  }
  function groupsValid(): boolean {
    if (groupsDraft === null) return false;
    // Every draft group needs a name AND at least one pubkey. Duplicate
    // names would be confusing in the audience picker — reject those too.
    const names = new Set<string>();
    for (const g of groupsDraft) {
      const name = g.name.trim();
      if (name === '' || g.member_pubkeys.length === 0) return false;
      if (names.has(name)) return false;
      names.add(name);
    }
    return true;
  }
  async function submitGroups() {
    if (groupsDraft === null) return;
    // Normalize on the way out: trim names, sorted+deduped pubkeys.
    // Empty draft → null so the manifest omits the field entirely
    // (byte-identical-when-absent with pre-v0.4.64).
    const clean = groupsDraft.map((g) => ({
      name: g.name.trim(),
      member_pubkeys: [...new Set(g.member_pubkeys)].sort(),
    }));
    await app.saveGroups(clean.length === 0 ? null : clean);
    if (app.adminStatus.kind === 'idle') closeGroupsEditor();
  }

  let rootPriv = $state('');
  let rootPub = $state('');
  let rootImporting = $state(false);
  let rootImportError = $state<string | null>(null);

  // v0.4.13: org default_thread setter.
  // currentDefault is what the hub says the org's hint is right now —
  // loaded from /directory on mount and after each successful update.
  // newDefault is the textfield draft. Empty string means "clear it".
  let currentDefault = $state<string | null>(null);
  let newDefault = $state('');
  let defaultLoaded = $state(false);
  let defaultSubmitting = $state(false);
  let defaultError = $state<string | null>(null);

  async function loadDefaultThread() {
    try {
      const m = await app.client?.fetchDirectory();
      currentDefault = m?.default_thread ?? null;
      newDefault = currentDefault ?? '';
      defaultLoaded = true;
    } catch {
      defaultLoaded = true;
    }
  }

  async function submitDefaultThread() {
    if (defaultSubmitting) return;
    const sanitized = sanitizeThreadName(newDefault);
    // Sanitization may yield '' even from a non-empty input (all chars
    // stripped) — treat both as "clear it" to keep the rule simple.
    const value = sanitized === '' ? null : sanitized;
    if (value === currentDefault) return;  // no-op
    defaultError = null;
    defaultSubmitting = true;
    try {
      currentDefault = await app.setDefaultThread(value);
      newDefault = currentDefault ?? '';
    } catch (err) {
      defaultError = (err as Error).message;
    } finally {
      defaultSubmitting = false;
    }
  }

  async function clearDefaultThread() {
    newDefault = '';
    await submitDefaultThread();
  }

  /** Approve form is per-row: when the user picks one, this holds the
   *  selected pubkey. The form fields below render based on it. */
  let approvingPubkey = $state<string | null>(null);
  let displayName = $state('');
  let affiliation = $state('');
  let role = $state<'member' | 'officer' | 'board'>('member');
  let title = $state('');

  onMount(async () => {
    await app.refreshRootKeychain();
    await app.loadPendingQueue();
    await loadDefaultThread();
    // v0.4.33: invites are admin-cap-gated; load them so the panel
    // shows outstanding codes the moment it opens.
    if (app.hasCapability('admin')) {
      void app.loadInvites();
    }
  });

  // v0.4.33: invite mint dialog state.
  let mintDialog = $state<{
    ttlSeconds: number;
    nameHint: string;
    submitting: boolean;
  } | null>(null);
  let lastMinted = $state<{ code: string; expires_at: number } | null>(null);
  let copied = $state<string | null>(null);

  function openMintDialog() {
    mintDialog = { ttlSeconds: 86400, nameHint: '', submitting: false };
    lastMinted = null;
  }
  function closeMintDialog() {
    mintDialog = null;
  }
  async function submitMint() {
    if (!mintDialog) return;
    mintDialog = { ...mintDialog, submitting: true };
    const inv = await app.mintInvite({
      ttlSeconds: mintDialog.ttlSeconds,
      nameHint: mintDialog.nameHint,
    });
    if (inv) {
      lastMinted = { code: inv.code, expires_at: inv.expires_at };
      mintDialog = null;
    } else if (mintDialog) {
      mintDialog = { ...mintDialog, submitting: false };
    }
  }
  async function copyInviteCode(code: string) {
    try {
      await navigator.clipboard.writeText(code);
      copied = code;
      setTimeout(() => { if (copied === code) copied = null; }, 1500);
    } catch { /* clipboard unavailable; code is visible */ }
  }
  async function handleRevoke(code: string) {
    if (!confirm('Revoke this invite? The recipient will get an "invalid code" error if they try to use it.')) return;
    await app.revokeInvite(code);
    if (lastMinted?.code === code) lastMinted = null;
  }
  function formatExpiresIn(seconds: number): string {
    if (seconds <= 0) return 'expired';
    if (seconds < 60) return `expires in ${seconds}s`;
    if (seconds < 3600) return `expires in ${Math.round(seconds / 60)}m`;
    if (seconds < 86400) return `expires in ${Math.round(seconds / 3600)}h`;
    return `expires in ${Math.round(seconds / 86400)}d`;
  }

  function startApprove(row: { pubkey: string; name_hint: string }) {
    approvingPubkey = row.pubkey;
    displayName = row.name_hint;
    affiliation = '';
    role = 'member';
    title = '';
  }

  function cancelApprove() {
    approvingPubkey = null;
  }

  async function submitApprove() {
    if (!approvingPubkey) return;
    await app.approvePending({
      pubkey: approvingPubkey,
      displayName: displayName.trim(),
      affiliation: affiliation.trim(),
      role,
      title: title.trim() || null,
    });
    if (app.adminStatus.kind === 'idle') {
      approvingPubkey = null;
    }
  }

  // ---- v0.4.71: manual attest (no pending queue required) -------------
  // Federation use case: a member has an identity on another hub already
  // and wants THIS hub to attest the same pubkey. Their client's identity
  // chip shows the pubkey; they paste it into the field below and the
  // keymaster attests. Also useful for out-of-band member add without
  // running the invite/pending dance.
  let manualPubkey = $state('');
  let manualDisplayName = $state('');
  let manualAffiliation = $state('');
  let manualRole = $state<'member' | 'officer' | 'board'>('member');
  let manualTitle = $state('');

  function manualPubkeyValid(): boolean {
    const s = manualPubkey.trim().toLowerCase();
    return s.length === 64 && /^[0-9a-f]+$/.test(s);
  }
  function manualFormReady(): boolean {
    return manualPubkeyValid()
      && manualDisplayName.trim().length > 0
      && !app.members.some((m) => m.member_pubkey === manualPubkey.trim().toLowerCase());
  }
  const manualAlreadyMember = $derived(
    manualPubkeyValid()
      && app.members.some((m) => m.member_pubkey === manualPubkey.trim().toLowerCase()),
  );
  async function submitManualAttest() {
    if (!manualFormReady()) return;
    await app.attestPubkey({
      pubkey: manualPubkey.trim().toLowerCase(),
      displayName: manualDisplayName.trim(),
      affiliation: manualAffiliation.trim(),
      role: manualRole,
      title: manualTitle.trim() || null,
    });
    if (app.adminStatus.kind === 'idle') {
      manualPubkey = '';
      manualDisplayName = '';
      manualAffiliation = '';
      manualRole = 'member';
      manualTitle = '';
    }
  }

  async function importRoot() {
    rootImportError = null;
    rootImporting = true;
    try {
      await app.importRootKeys(rootPriv.trim(), rootPub.trim());
      rootPriv = '';
    } catch (err) {
      rootImportError = (err as Error).message;
    } finally {
      rootImporting = false;
    }
  }

  async function clearRoot() {
    await app.clearRootKeys();
  }

  // v0.4.76: identity-vault management. Mirrors the pattern for invites:
  // simple flag + inputs, handlers call into AppState which does the
  // vault-blob crypto and multi-hub push.
  let vaultBusy = $state(false);
  let vaultError = $state<string | null>(null);
  let addPassphraseOpen = $state(false);
  let newSlotLabel = $state('');
  let newPassphrase = $state('');

  function showAddPassphrase() {
    vaultError = null;
    newSlotLabel = '';
    newPassphrase = '';
    addPassphraseOpen = true;
  }

  async function confirmAddPassphrase() {
    if (newPassphrase.length < 12 || !newSlotLabel.trim()) return;
    vaultBusy = true;
    vaultError = null;
    try {
      await app.addPassphraseUnlock({
        passphrase: newPassphrase,
        label: newSlotLabel.trim(),
      });
      addPassphraseOpen = false;
      newPassphrase = '';
      newSlotLabel = '';
    } catch (err) {
      vaultError = (err as Error).message;
    } finally {
      vaultBusy = false;
    }
  }

  async function addPasskey() {
    vaultBusy = true;
    vaultError = null;
    try {
      const label = prompt("Label for this Passkey unlock:", 'Passkey') ?? '';
      if (!label.trim()) { vaultBusy = false; return; }
      await app.addPasskeyUnlock(label.trim());
    } catch (err) {
      vaultError = (err as Error).message;
    } finally {
      vaultBusy = false;
    }
  }

  async function removeSlot(slotId: string) {
    vaultBusy = true;
    vaultError = null;
    try {
      await app.removeUnlock(slotId);
    } catch (err) {
      vaultError = (err as Error).message;
    } finally {
      vaultBusy = false;
    }
  }

</script>

<section class="admin" aria-label="Keymaster admin panel">
  <header>
    <h1>Pending approvals</h1>
    <div class="header-actions">
      <button type="button" class="refresh" onclick={() => app.loadPendingQueue()}
        title="Refresh queue">↻</button>
    </div>
  </header>

  {#if !app.rootKeysPresent}
    <!-- Step 1: Import root keys (one-time per keymaster device PER HUB). -->
    <div class="root-setup">
      <h2>Set up root key custody for {activeHubLabel}</h2>
      <p class="muted">
        This device is the keymaster station for
        <code>{activeHubLabel}</code>. Import that hub's
        root keypair so you can attest members and edit the manifest.
        The private key goes straight to your OS keychain — a separate
        slot per hub — never returns to the app, and never reaches
        the hub. If you admin multiple hubs, import each one here after
        switching to it in the sidebar.
      </p>
      <label>
        <span>Root private key (hex)</span>
        <textarea bind:value={rootPriv} rows="2" autocomplete="off"
          spellcheck="false" placeholder="64-char hex"></textarea>
      </label>
      <label>
        <span>Root public key (hex)</span>
        <textarea bind:value={rootPub} rows="2" autocomplete="off"
          spellcheck="false" placeholder="64-char hex"></textarea>
      </label>
      {#if rootImportError}
        <p class="failure" role="alert">{rootImportError}</p>
      {/if}
      <div class="actions">
        <button type="button" onclick={importRoot}
          disabled={rootImporting || !rootPriv.trim() || !rootPub.trim()}>
          {rootImporting ? 'Importing…' : `Import root key for ${activeHubLabel}`}
        </button>
      </div>
    </div>

  {:else if app.pendingQueue.length === 0}
    <div class="empty">
      <p>No one's waiting. New requests will appear here automatically.</p>
    </div>

  {:else}
    <ul class="queue">
      {#each app.pendingQueue as row (row.pubkey)}
        <li>
          <div class="row-summary">
            <div class="row-meta">
              <span class="row-name">{row.name_hint}</span>
              <code class="row-fp" title="Full pubkey fingerprint — compare against what the member's device shows">{fingerprint(row.pubkey)}</code>
            </div>
            <span class="row-time">{row.requested_at.slice(0, 16)}</span>
          </div>

          {#if approvingPubkey === row.pubkey}
            <div class="approve-form">
              <label>
                <span>Display name</span>
                <input type="text" bind:value={displayName}
                  placeholder="As it should appear in the directory" />
              </label>
              <label>
                <span>Affiliation</span>
                <input type="text" bind:value={affiliation}
                  placeholder="Lot 27 / Engineering / etc." />
              </label>
              <div class="row-fields">
                <label>
                  <span>Role</span>
                  <select bind:value={role}>
                    <option value="member">member</option>
                    <option value="officer">officer</option>
                    <option value="board">board</option>
                  </select>
                </label>
                <label class="grow">
                  <span>Title (optional)</span>
                  <input type="text" bind:value={title}
                    placeholder="President, Treasurer, …" />
                </label>
              </div>

              {#if app.adminStatus.kind === 'error'}
                <p class="failure" role="alert">{app.adminStatus.message}</p>
              {/if}

              <div class="row-actions">
                <button type="button" class="ghost" onclick={cancelApprove}
                  disabled={app.adminStatus.kind === 'submitting'}>
                  Cancel
                </button>
                <button type="button" class="danger"
                  onclick={() => app.rejectPending(row.pubkey)}
                  disabled={app.adminStatus.kind === 'submitting'}>
                  Reject
                </button>
                <button type="button" onclick={submitApprove}
                  disabled={app.adminStatus.kind === 'submitting'
                    || !displayName.trim() || !affiliation.trim()}>
                  {app.adminStatus.kind === 'submitting' ? 'Signing…' : 'Approve & attest'}
                </button>
              </div>
            </div>
          {:else}
            <div class="row-actions">
              <button type="button" class="ghost"
                onclick={() => app.rejectPending(row.pubkey)}>
                Reject
              </button>
              <button type="button" onclick={() => startApprove(row)}>
                Review
              </button>
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}

  {#if app.rootKeysPresent}
    <!-- v0.4.71: manual attest. A member with an existing identity on
         another Cove hub can paste their pubkey here to join THIS hub
         under the same keypair. The identity chip in the sidebar
         footer shows the full hex; a keymaster on the target hub
         pastes it here + fills in the display name/role and this hub
         signs an attestation over it. Same admin/attest endpoint as
         approve-pending; the pending queue just isn't involved. -->
    <section class="manual-attest">
      <h2>Attest a public key</h2>
      <p class="muted">
        Paste a public key that already exists on another hub (or a new
        keypair generated out-of-band). The board's root signs an
        attestation binding the key to a name and role. Members with
        identities on multiple hubs use this to federate.
      </p>
      <div class="row-form">
        <label>
          <span>Public key (64-char hex)</span>
          <textarea bind:value={manualPubkey} rows="2"
            spellcheck="false" autocomplete="off"
            placeholder="e.g. d0c2dde31e14d1d4625b7fa3a69b1db937b86032d0ed03283519f3a3f2950a0a"></textarea>
        </label>
        {#if manualPubkey.trim().length > 0 && !manualPubkeyValid()}
          <p class="small warning">Must be exactly 64 hex chars (Ed25519 pubkey).</p>
        {/if}
        {#if manualAlreadyMember}
          <p class="small warning">This pubkey is already an attested member on this hub.</p>
        {/if}
        <label>
          <span>Display name</span>
          <input type="text" bind:value={manualDisplayName}
            placeholder="e.g. Kevin Brooks (phone)" />
        </label>
        <label>
          <span>Affiliation</span>
          <input type="text" bind:value={manualAffiliation}
            placeholder="e.g. LWCCOA / Board / Lot 42 — freeform" />
        </label>
        <label>
          <span>Role</span>
          <select bind:value={manualRole}>
            <option value="member">member</option>
            <option value="officer">officer</option>
            <option value="board">board</option>
          </select>
        </label>
        <label>
          <span>Title (optional)</span>
          <input type="text" bind:value={manualTitle}
            placeholder="e.g. President, VP Engineering" />
        </label>
        <div class="row-actions">
          <button type="button" onclick={submitManualAttest}
            disabled={!manualFormReady() || app.adminStatus.kind === 'submitting'}>
            {app.adminStatus.kind === 'submitting' ? 'Signing…' : 'Attest'}
          </button>
        </div>
        {#if app.adminStatus.kind === 'error'}
          <p class="small error">{app.adminStatus.message}</p>
        {/if}
      </div>
    </section>
  {/if}

  {#if app.liveVault}
    <!-- v0.4.76: identity-vault management. The vault stores the current
         session's canonical priv wrapped for N unlock methods (passphrase,
         Passkey PRF, ...). Adding/removing a method rewrites only that
         method's slot; the priv itself never re-encrypts. Vault storage
         is hub-side + opaque — the hub never sees plaintext key material. -->
    <section class="identity-vault">
      <h2>Identity vault</h2>
      <p class="muted">
        Ways you can sign in on any device that reaches this hub. Adding
        a Passkey lets you sign in with FaceID / TouchID / Windows Hello;
        adding a passphrase gives you a cross-ecosystem fallback that
        works on any device including Android.
      </p>

      {#if app.vaultPushFailures.length > 0}
        <p class="failure" role="alert">
          Vault didn't sync to {app.vaultPushFailures.length} hub(s):
          {app.vaultPushFailures.join(', ')}. Next successful save will
          retry.
        </p>
      {/if}

      <ul class="slots">
        {#each app.liveVault.method_slots as slot (slot.id)}
          <li>
            <div class="slot-icon" aria-hidden="true">
              {slot.type === 'passkey' ? '🔑' : '🔒'}
            </div>
            <div class="slot-meta">
              <div class="slot-label">{slot.label}</div>
              <div class="slot-sub muted">
                {slot.type === 'passkey' ? 'Passkey' : 'Passphrase'}
                • added {new Date(slot.created_at).toLocaleDateString()}
              </div>
            </div>
            <button type="button" class="ghost small"
              disabled={vaultBusy || (app.liveVault?.method_slots.length ?? 0) <= 1}
              title={(app.liveVault?.method_slots.length ?? 0) <= 1
                ? 'Removing the last method would lock you out' : ''}
              onclick={() => removeSlot(slot.id)}>Remove</button>
          </li>
        {/each}
      </ul>

      <div class="add-actions">
        <button type="button" onclick={showAddPassphrase}
          disabled={vaultBusy}>Add passphrase</button>
        <button type="button" onclick={addPasskey}
          disabled={vaultBusy || !app.passkeySupported}>Add Passkey</button>
      </div>

      {#if addPassphraseOpen}
        <div class="add-form">
          <label>
            <span>Label</span>
            <input type="text" bind:value={newSlotLabel}
              placeholder="e.g. Emergency backup" />
          </label>
          <label>
            <span>Passphrase (≥ 12 chars)</span>
            <input type="password" bind:value={newPassphrase} />
          </label>
          <div class="row-actions">
            <button type="button" class="ghost"
              onclick={() => { addPassphraseOpen = false; }}>Cancel</button>
            <button type="button" onclick={confirmAddPassphrase}
              disabled={vaultBusy || newPassphrase.length < 12
                        || !newSlotLabel.trim()}>Add</button>
          </div>
        </div>
      {/if}

      {#if vaultError}
        <p class="failure" role="alert">{vaultError}</p>
      {/if}
    </section>
  {/if}

  {#if app.rootKeysPresent && app.members.length > 0}
    <!-- v0.4.23: membership editor. Roles change over time (members
         elected to the board, officers stepping down) and pubkeys
         sometimes need to come out of the directory (key compromise,
         departure). Both flows root-sign a fresh manifest and POST
         to /admin/{attest,revoke}; from the hub's perspective they're
         the same well-trodden admin endpoint, but the UI keeps them
         distinct so a keymaster never confuses 'fix a name' with
         'permanently revoke this key.' -->
    <section class="members">
      <h2>Members</h2>
      <p class="muted">
        {app.members.length} active member{app.members.length === 1 ? '' : 's'}.
        Edit changes a member's name, role, or title — the old attestation
        is preserved in the manifest chain. Revoke is permanent.
      </p>
      <ul>
        {#each app.members as att (att.member_pubkey)}
          {@const isOpen = openRow === att.member_pubkey}
          {@const isBoard = att.role === 'board'}
          <li class:open={isOpen}>
            <div class="member-summary">
              <div class="avatar" aria-hidden="true"
                style="background-color: {authorColor(att.member_pubkey)};">
                {initials(att.display_name)}
              </div>
              <div class="member-meta">
                <div class="member-name-row">
                  <span class="member-name" class:board={isBoard}>{att.display_name}</span>
                  <span class="role-tag" class:board={isBoard}>{att.role}</span>
                  {#if att.title}<span class="member-title">· {att.title}</span>{/if}
                </div>
                <div class="member-sub">
                  {att.affiliation || '—'}
                  {#if isSelf(att)}<span class="self-marker">· you</span>{/if}
                </div>
              </div>
              {#if !isOpen}
                <div class="row-actions">
                  <button type="button" class="ghost" onclick={() => openEdit(att)}>Edit</button>
                  <button type="button" class="ghost" onclick={() => openLimits(att)}>Limits</button>
                  <button type="button" class="danger"
                    onclick={() => openRevoke(att)}
                    disabled={isSelf(att)}
                    title={isSelf(att) ? "You can't revoke your own key from inside the app — use an out-of-band action." : ''}>
                    Revoke
                  </button>
                </div>
              {/if}
            </div>

            {#if isOpen && rowMode?.kind === 'edit'}
              <div class="row-form">
                <label>
                  <span>Display name</span>
                  <input type="text" bind:value={editName} />
                </label>
                <label>
                  <span>Affiliation</span>
                  <input type="text" bind:value={editAffiliation} />
                </label>
                <div class="row-fields">
                  <label>
                    <span>Role</span>
                    <select bind:value={editRole}>
                      <option value="member">member</option>
                      <option value="officer">officer</option>
                      <option value="board">board</option>
                    </select>
                  </label>
                  <label class="grow">
                    <span>Title (optional)</span>
                    <input type="text" bind:value={editTitle}
                      placeholder="President, Treasurer, …" />
                  </label>
                </div>
                {#if app.adminStatus.kind === 'error'}
                  <p class="failure" role="alert">{app.adminStatus.message}</p>
                {/if}
                <p class="muted small">
                  Pubkey <code>{fingerprint(att.member_pubkey)}</code> — immutable.
                </p>
                <div class="row-actions">
                  <button type="button" class="ghost" onclick={closeRow}
                    disabled={app.adminStatus.kind === 'submitting'}>Cancel</button>
                  <button type="button" onclick={() => submitEdit(att)}
                    disabled={app.adminStatus.kind === 'submitting'
                      || !editName.trim() || !editAffiliation.trim()}>
                    {app.adminStatus.kind === 'submitting' ? 'Signing…' : 'Save changes'}
                  </button>
                </div>
              </div>
            {/if}

            {#if isOpen && rowMode?.kind === 'limits'}
              <div class="row-form">
                <p class="muted small">
                  Throttle override applies to {att.display_name}'s
                  per-identity rate, volume, and storage caps. Default
                  is the tier matching their role ({att.role}); pick a
                  different tier to lift or lower the ceiling. Overrides
                  are process-local on the hub — they reset on hub
                  restart, by design (§7.2.2).
                </p>
                <label>
                  <span>Tier</span>
                  <select bind:value={limitsTier}>
                    <option value="member">member (default)</option>
                    <option value="officer">officer (3× member)</option>
                    <option value="board">board (6× member)</option>
                  </select>
                </label>
                {#if app.adminStatus.kind === 'error'}
                  <p class="failure" role="alert">{app.adminStatus.message}</p>
                {/if}
                <div class="row-actions">
                  <button type="button" class="ghost" onclick={closeRow}
                    disabled={app.adminStatus.kind === 'submitting'}>Cancel</button>
                  <button type="button" onclick={() => submitLimits(att)}
                    disabled={app.adminStatus.kind === 'submitting'}>
                    {app.adminStatus.kind === 'submitting' ? 'Signing…' : 'Apply tier'}
                  </button>
                </div>
              </div>
            {/if}

            {#if isOpen && rowMode?.kind === 'revoke'}
              <div class="row-form danger-form">
                <p class="warn">
                  ⚠ Revocation is permanent. {att.display_name}'s key can
                  never be un-revoked — entries they sign after this moment
                  will be rejected by every client. Their historical entries
                  remain valid (the as-of rule from §2.3).
                </p>
                <label>
                  <span>Reason (becomes part of the audit record)</span>
                  <textarea bind:value={revokeReason} rows="2"
                    placeholder="Key compromise / left the org / lost device …"></textarea>
                </label>
                {#if app.adminStatus.kind === 'error'}
                  <p class="failure" role="alert">{app.adminStatus.message}</p>
                {/if}
                <div class="row-actions">
                  <button type="button" class="ghost" onclick={closeRow}
                    disabled={app.adminStatus.kind === 'submitting'}>Cancel</button>
                  <button type="button" class="danger-filled"
                    onclick={() => submitRevoke(att)}
                    disabled={app.adminStatus.kind === 'submitting' || !revokeReason.trim()}>
                    {app.adminStatus.kind === 'submitting' ? 'Signing…' : `Revoke ${att.display_name}`}
                  </button>
                </div>
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    </section>
  {/if}

  {#if app.rootKeysPresent && app.revoked.length > 0}
    <!-- v0.4.24: tombstones for revoked keys. No actions — once revoked,
         always revoked (§2.3). Surfacing them keeps the audit story
         visible: when the board acts, the receipt of that action is
         right here, not buried in a manifest chain. -->
    <section class="revoked">
      <h2>Recently revoked</h2>
      <p class="muted">
        These keys are permanently out of the directory. Historical
        entries they signed before the revocation timestamp remain
        valid (the as-of rule).
      </p>
      <ul>
        {#each app.revoked as r (r.revocation.pubkey)}
          <li>
            <div class="revoked-summary">
              <div class="revoked-meta">
                <div class="revoked-name-row">
                  <span class="revoked-name">
                    {r.attestation?.display_name ?? '(unattested key)'}
                  </span>
                  {#if r.attestation?.role}
                    <span class="role-tag">{r.attestation.role}</span>
                  {/if}
                </div>
                <div class="revoked-reason">
                  {r.revocation.reason || 'no reason given'}
                </div>
                <code class="revoked-fp">{fingerprint(r.revocation.pubkey)}</code>
              </div>
              <span class="revoked-at">{formatRevokedAt(r.revocation.revoked_at)}</span>
            </div>
          </li>
        {/each}
      </ul>
    </section>
  {/if}

  {#if app.rootKeysPresent}
    <!-- v0.4.13: org default-thread setter. Visible whenever the
         keymaster has root.priv loaded, regardless of queue state.
         Sits below the queue so the day-to-day approval task stays
         the top of the panel. -->
    <section class="org-settings">
      <h2>Org settings</h2>
      <p class="muted">
        Signed into the directory manifest. Applies to new members
        running v0.4.13 or later — older clients fall back to their
        local default ("general").
      </p>
      <label>
        <span>Default landing thread for new members</span>
        <input type="text" bind:value={newDefault}
          placeholder={defaultLoaded ? (currentDefault ?? 'not set') : 'loading…'}
          autocapitalize="off" autocorrect="off" spellcheck="false"
          disabled={defaultSubmitting || !defaultLoaded} />
      </label>
      {#if defaultError}
        <p class="failure" role="alert">{defaultError}</p>
      {/if}
      <div class="actions">
        {#if currentDefault !== null}
          <button type="button" class="ghost" onclick={clearDefaultThread}
            disabled={defaultSubmitting}>Clear</button>
        {/if}
        <button type="button" onclick={submitDefaultThread}
          disabled={defaultSubmitting || !defaultLoaded
            || (sanitizeThreadName(newDefault) === (currentDefault ?? ''))}>
          {defaultSubmitting ? 'Signing…' : 'Save'}
        </button>
      </div>
    </section>

    <!-- v0.4.33: invite codes — admission gate for POST /pending.
         The keymaster mints time-limited single-use codes here and
         delivers them out-of-band (text / Signal / paper). Without
         a valid code, /pending submissions get a 401 and never reach
         the queue. See cove/invites.py for the rationale. -->
    <section class="invites">
      <header class="invites-header">
        <div>
          <h2>Invites</h2>
          <p class="muted">
            Mint a single-use code; share it out-of-band with the new
            member. Without a code, /pending submissions are rejected
            — keeps the queue spam-free.
          </p>
        </div>
        <button type="button" onclick={openMintDialog}>+ Mint invite</button>
      </header>

      {#if lastMinted}
        <div class="last-minted">
          <p class="muted small">
            Fresh code — share with the new member by text or in person.
            They enter it in their "Get started" screen.
          </p>
          <div class="code-row">
            <code class="big-code">{lastMinted.code}</code>
            <button type="button" class="ghost"
              onclick={() => copyInviteCode(lastMinted!.code)}>
              {copied === lastMinted.code ? 'Copied!' : 'Copy'}
            </button>
          </div>
        </div>
      {/if}

      {#if app.invitesStatus.kind === 'loading'}
        <p class="muted small">Loading invites…</p>
      {:else if app.invitesStatus.kind === 'error'}
        <p class="failure" role="alert">⚠ {app.invitesStatus.message}</p>
      {:else if app.invites.length === 0}
        <p class="muted small">No active invites. Mint one to admit a new member.</p>
      {:else}
        <ul class="invite-list">
          {#each app.invites as inv (inv.code)}
            <li>
              <div class="invite-row">
                <code class="invite-code">{inv.code}</code>
                <div class="invite-meta">
                  {#if inv.name_hint}
                    <span class="hint">for {inv.name_hint}</span>
                  {/if}
                  <span class="ttl">{formatExpiresIn(inv.expires_in_seconds)}</span>
                </div>
                <div class="invite-actions">
                  <button type="button" class="ghost"
                    onclick={() => copyInviteCode(inv.code)}>
                    {copied === inv.code ? 'Copied' : 'Copy'}
                  </button>
                  <button type="button" class="danger"
                    onclick={() => handleRevoke(inv.code)}>Revoke</button>
                </div>
              </div>
            </li>
          {/each}
        </ul>
        <p class="muted small">
          Codes are process-local on the hub — they evaporate on
          restart. Active outstanding codes from a prior process are
          unrecoverable; mint fresh ones if needed.
        </p>
      {/if}
    </section>

    {#if mintDialog}
      <div class="modal-backdrop" onclick={closeMintDialog} role="presentation"></div>
      <div class="modal" role="dialog" aria-label="Mint invite">
        <h3>Mint invite</h3>
        <p class="muted small">
          Pick a validity window. Shorter is safer (less time for a
          leaked code to be used by the wrong person).
        </p>
        <label>
          <span>Expires in</span>
          <select bind:value={mintDialog.ttlSeconds}
            disabled={mintDialog.submitting}>
            <option value={3600}>1 hour</option>
            <option value={86400}>24 hours</option>
            <option value={604800}>7 days</option>
          </select>
        </label>
        <label>
          <span>For whom? (optional — your own notes)</span>
          <input type="text" bind:value={mintDialog.nameHint}
            placeholder="Carol's daughter"
            disabled={mintDialog.submitting} />
        </label>
        {#if app.adminStatus.kind === 'error'}
          <p class="failure" role="alert">{app.adminStatus.message}</p>
        {/if}
        <div class="modal-actions">
          <button type="button" class="ghost" onclick={closeMintDialog}
            disabled={mintDialog.submitting}>Cancel</button>
          <button type="button" onclick={submitMint}
            disabled={mintDialog.submitting}>
            {mintDialog.submitting ? 'Minting…' : 'Mint'}
          </button>
        </div>
      </div>
    {/if}

    <!-- v0.4.25: org-defined role → capability map. The default
         mapping (board → admin + archive) is what kicks in when this
         field is absent — that matches pre-v0.4.25 behavior, including
         the LWCCOA pilot's current manifest. Set it explicitly to
         grant capabilities to other roles (an "officer" who can see
         admin views; a "lead" who can archive threads; whatever the
         org needs). -->
    <section class="roles">
      <header class="roles-header">
        <div>
          <h2>Roles & permissions</h2>
          <p class="muted">
            {#if app.manifest?.capabilities_by_role}
              Org-defined. {Object.keys(app.manifest.capabilities_by_role).length}
              role{Object.keys(app.manifest.capabilities_by_role).length === 1 ? '' : 's'} mapped.
            {:else}
              Using the default mapping (board → admin + archive). Other
              roles have no capabilities until you map them.
            {/if}
          </p>
        </div>
        {#if rolesDraft === null}
          <button type="button" class="ghost" onclick={openRolesEditor}>Edit</button>
        {/if}
      </header>

      {#if rolesDraft !== null}
        <div class="role-matrix">
          <table>
            <thead>
              <tr>
                <th class="role-col">Role</th>
                {#each CAPABILITIES as cap}
                  <th>{cap}</th>
                {/each}
                <th class="remove-col"></th>
              </tr>
            </thead>
            <tbody>
              {#each Object.keys(rolesDraft).sort((a, b) => {
                const priors: Record<string, number> = { board: 0, officer: 1, member: 2 };
                const pa = priors[a] ?? 99, pb = priors[b] ?? 99;
                if (pa !== pb) return pa - pb;
                return a.localeCompare(b);
              }) as role}
                <tr>
                  <td class="role-col">
                    <code>{role}</code>
                    {#if !roleIsObservedInAttestations(role)}
                      <span class="role-tag muted" title="No attested members have this role yet">
                        new
                      </span>
                    {/if}
                  </td>
                  {#each CAPABILITIES as cap}
                    <td>
                      <input type="checkbox"
                        checked={(rolesDraft[role] ?? []).includes(cap)}
                        onchange={() => toggleCap(role, cap)} />
                    </td>
                  {/each}
                  <td class="remove-col">
                    <button type="button" class="role-remove"
                      title={roleIsObservedInAttestations(role)
                        ? 'Remove from cap map. Existing attestations keep their role string but lose all caps under the new manifest.'
                        : 'Remove this role from the draft.'}
                      onclick={() => removeRole(role)}>×</button>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>

          <form class="add-role" onsubmit={(e) => { e.preventDefault(); addRole(); }}>
            <input type="text" bind:value={newRole}
              placeholder="Add role… (e.g. sales, engineering)"
              autocapitalize="off" autocorrect="off" spellcheck="false" />
            <button type="submit" class="ghost"
              disabled={!newRole.trim() || (rolesDraft !== null && newRole.trim() in rolesDraft)}>
              Add
            </button>
          </form>

          <p class="muted small">
            Capabilities are protocol-defined.
            <code>admin</code> sees the admin panel + pending queue.
            <code>archive</code> archives or reopens threads.
            Roles themselves are org-namespaced — add the ones your
            org actually uses. Saving root-signs an updated directory
            manifest; every connected client refreshes via /stream.
          </p>
          {#if app.adminStatus.kind === 'error'}
            <p class="failure" role="alert">{app.adminStatus.message}</p>
          {/if}
          <div class="actions">
            <button type="button" class="ghost" onclick={closeRolesEditor}
              disabled={app.adminStatus.kind === 'submitting'}>Cancel</button>
            <button type="button" onclick={submitRoles}
              disabled={app.adminStatus.kind === 'submitting' || !isDirty()}>
              {app.adminStatus.kind === 'submitting' ? 'Signing…' : 'Save mapping'}
            </button>
          </div>
        </div>
      {/if}
    </section>

    <!-- v0.4.64: Keypair groups. Ergonomic shortcut for the audience
         picker — a group bundles several member pubkeys (e.g. "Kevin
         + Kevin's Phone") under one display name so an admin can add
         them all with one click. Storage is a root-signed field on the
         DirectoryManifest, so all admins across all devices see the
         same groups after the next manifest fetch. -->
    <section class="groups">
      <header class="groups-header">
        <div>
          <h2>Keypair groups</h2>
          <p class="muted">
            Bundle a person's device keypairs under one name so
            audiences can be picked with a single click.
            {#if currentGroups.length === 0}
              No groups defined yet.
            {:else}
              {currentGroups.length} group{currentGroups.length === 1 ? '' : 's'} defined.
            {/if}
          </p>
        </div>
        {#if groupsDraft === null}
          <button type="button" class="ghost" onclick={openGroupsEditor}>Edit</button>
        {/if}
      </header>

      {#if groupsDraft === null}
        {#if currentGroups.length > 0}
          <ul class="groups-list">
            {#each currentGroups as g (g.name)}
              <li>
                <span class="group-name">{g.name}</span>
                <span class="group-count muted">
                  {g.member_pubkeys.length} keypair{g.member_pubkeys.length === 1 ? '' : 's'}
                </span>
              </li>
            {/each}
          </ul>
        {/if}
      {:else}
        <div class="groups-editor">
          {#if groupsDraft.length === 0}
            <p class="muted small">
              No groups yet. Click <strong>+ New group</strong> to add
              one.
            </p>
          {/if}
          {#each groupsDraft as g, i (i)}
            <div class="group-card">
              <div class="group-card-head">
                <input type="text" class="group-name-input"
                  placeholder="Group name (e.g. Kevin)"
                  value={g.name}
                  oninput={(e) => updateGroupName(i, (e.currentTarget as HTMLInputElement).value)}
                  maxlength="64"
                  autocapitalize="on" spellcheck="false" />
                <button type="button" class="ghost small"
                  onclick={() => removeGroup(i)}
                  title="Delete this group">Remove</button>
              </div>
              <p class="muted small">
                Select the keypairs to bundle. Revoked members can still
                be in a group; they're filtered at delivery time.
              </p>
              <ul class="group-member-list">
                {#each app.members as m (m.member_pubkey)}
                  <li>
                    <label>
                      <input type="checkbox"
                        checked={g.member_pubkeys.includes(m.member_pubkey)}
                        onchange={() => toggleMemberInGroup(i, m.member_pubkey)} />
                      <span class="name">{m.display_name}</span>
                      {#if m.role !== 'member'}
                        <span class="role-tag">{m.role}</span>
                      {/if}
                    </label>
                  </li>
                {/each}
              </ul>
              {#if g.name.trim() === ''}
                <p class="small warning">Group needs a name.</p>
              {:else if g.member_pubkeys.length === 0}
                <p class="small warning">Group needs at least one keypair.</p>
              {/if}
            </div>
          {/each}
          <button type="button" class="ghost" onclick={addGroup}>
            + New group
          </button>
          <div class="groups-editor-actions">
            <button type="button" class="ghost"
              onclick={closeGroupsEditor}
              disabled={app.adminStatus.kind === 'submitting'}>Cancel</button>
            <button type="button"
              onclick={submitGroups}
              disabled={!groupsDirty() || !groupsValid()
                || app.adminStatus.kind === 'submitting'}>
              {app.adminStatus.kind === 'submitting' ? 'Signing…' : 'Save groups'}
            </button>
          </div>
          {#if app.adminStatus.kind === 'error'}
            <p class="small error">{app.adminStatus.message}</p>
          {/if}
        </div>
      {/if}
    </section>

    <section class="danger-zone">
      <button type="button" class="ghost" onclick={clearRoot}>
        Forget {activeHubLabel}'s root key on this device
      </button>
    </section>
  {/if}
</section>

<style>
  .admin {
    flex: 1;
    overflow-y: auto;
    padding: 1.5rem;
  }
  .admin > :global(*) {
    max-width: 720px;
    margin-left: auto;
    margin-right: auto;
  }
  header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.5rem;
  }
  h1 {
    margin: 0 0 0.25rem;
    font-size: 1.4rem;
    font-weight: 600;
  }
  h2 {
    margin: 0 0 0.5rem;
    font-weight: 600;
    font-size: 1.05rem;
  }
  .refresh {
    background: transparent; border: none; color: var(--muted);
    font-size: 1.2em; cursor: pointer; padding: 0.1em 0.4em;
    border-radius: 4px;
  }
  .refresh:hover {
    background: rgba(255, 255, 255, 0.04); color: var(--fg);
  }
  .root-setup {
    border: 1px dashed var(--border); border-radius: 12px;
    padding: 1.6rem; background: var(--panel);
  }
  .root-setup .muted {
    color: var(--muted); margin: 0 0 1rem; font-size: 0.9rem;
  }
  label {
    display: block; margin: 0.7rem 0;
  }
  label > span {
    display: block; font-size: 0.84rem; color: var(--muted);
    margin-bottom: 0.3rem;
  }
  textarea, input[type="text"], select {
    width: 100%; box-sizing: border-box;
    background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 0.5rem 0.7rem; font: inherit; font-size: 0.92rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  textarea:focus, input:focus, select:focus {
    outline: none; border-color: rgba(212, 175, 55, 0.5);
  }
  input[type="text"] { font-family: inherit; }
  select { font-family: inherit; }
  .row-fields {
    display: flex; gap: 0.6rem; align-items: stretch;
  }
  .row-fields > label { flex: 0 0 9rem; margin: 0; }
  .row-fields > label.grow { flex: 1; }
  .empty {
    text-align: center; color: var(--muted); padding: 3rem 1rem;
  }
  .queue {
    list-style: none; margin: 0; padding: 0;
  }
  .queue > li {
    margin: 0 0 1rem;
    border: 1px solid var(--border); border-radius: 12px;
    background: var(--panel); padding: 1rem 1.2rem;
  }
  .row-summary {
    display: flex; justify-content: space-between; align-items: baseline;
    gap: 1rem;
  }
  .row-meta { display: flex; flex-direction: column; gap: 0.25rem; min-width: 0; }
  .row-name { font-weight: 600; font-size: 1rem; }
  .row-fp {
    color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.78rem;
    background: transparent;
    word-spacing: 0.05em;
    /* Fingerprint is 32 hex chars + 7 spaces (8x4 blocks). Let it wrap
       on narrow panels rather than truncate — partial matching is the
       whole problem we fixed by switching to the full fingerprint. */
    overflow-wrap: anywhere;
  }
  .row-time { color: var(--muted); font-size: 0.82rem; }
  .row-actions, .actions {
    display: flex; justify-content: flex-end; gap: 0.5rem;
    margin-top: 0.85rem;
  }
  .approve-form { margin-top: 0.85rem; }
  button {
    background: #d4af37; color: #0a0a0a; border: none;
    border-radius: 999px; padding: 0.5rem 1.2rem; font: inherit;
    font-weight: 600; cursor: pointer;
    transition: transform 120ms, background 200ms;
  }
  button:hover:not(:disabled) {
    transform: translateY(-1px); background: #e2bf4e;
  }
  button:disabled {
    background: var(--border); color: var(--muted); cursor: not-allowed;
  }
  button.ghost {
    background: transparent; border: 1px solid var(--border); color: var(--muted);
  }
  button.ghost:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.04); color: var(--fg);
  }
  button.danger {
    background: transparent; border: 1px solid rgba(220, 38, 38, 0.4);
    color: #fca5a5;
  }
  button.danger:hover:not(:disabled) {
    background: rgba(220, 38, 38, 0.08);
  }
  .failure {
    margin: 0.6rem 0; padding: 0.6rem 0.8rem; border-radius: 8px;
    background: rgba(220, 38, 38, 0.08);
    border: 1px solid rgba(220, 38, 38, 0.4);
    color: #fca5a5; font-size: 0.88rem;
  }
  /* v0.4.71: manual attest section. Same top-border + spacing rhythm
     as the .members section so the panel reads as a stack of same-
     shaped org-admin blocks. */
  .manual-attest {
    margin-top: 2rem;
    border-top: 1px solid var(--border);
    padding-top: 1.4rem;
  }
  .manual-attest h2 { margin: 0 0 0.3rem; }
  .manual-attest > .muted {
    color: var(--muted); margin: 0 0 0.9rem; font-size: 0.9rem; line-height: 1.5;
  }
  .manual-attest .row-form {
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1rem 1.2rem;
    background: var(--panel);
  }
  .manual-attest textarea {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.84rem;
  }
  .manual-attest .row-actions {
    display: flex; justify-content: flex-end; margin-top: 0.6rem;
  }
  .manual-attest .small.warning {
    color: rgba(212, 175, 55, 0.9);
    margin: -0.3rem 0 0.4rem;
  }
  .manual-attest .small.error {
    color: #fca5a5; margin: 0.6rem 0 0;
  }

  /* v0.4.76: identity-vault section. Same visual weight as .members and
     .invites — a slot list + inline forms. */
  .identity-vault {
    margin-top: 2rem; border-top: 1px solid var(--border);
    padding-top: 1.4rem;
  }
  .identity-vault > .muted {
    color: var(--muted); margin: 0 0 0.8rem; font-size: 0.85rem;
  }
  .identity-vault ul.slots {
    list-style: none; margin: 0 0 1rem; padding: 0;
  }
  .identity-vault ul.slots > li {
    display: flex; align-items: center; gap: 0.85rem;
    border: 1px solid var(--border); border-radius: 10px;
    background: var(--panel); padding: 0.7rem 1rem;
    margin: 0 0 0.5rem;
  }
  .identity-vault .slot-icon { font-size: 1.4rem; }
  .identity-vault .slot-meta { flex: 1; min-width: 0; }
  .identity-vault .slot-label { font-weight: 600; font-size: 0.95rem; }
  .identity-vault .slot-sub { font-size: 0.82rem; }
  .identity-vault .add-actions {
    display: flex; gap: 0.6rem; margin-top: 0.6rem;
  }
  .identity-vault .add-form {
    margin-top: 0.8rem; padding: 0.9rem 1rem;
    border: 1px dashed var(--border); border-radius: 10px;
    background: rgba(255,255,255,0.02);
  }
  .identity-vault .add-form label { display: block; margin: 0.4rem 0; }
  .identity-vault .add-form label span {
    display: block; font-size: 0.82rem; color: var(--muted);
    margin-bottom: 0.25rem;
  }
  .identity-vault .add-form input {
    width: 100%; box-sizing: border-box;
    padding: 0.5rem 0.7rem; border: 1px solid var(--border);
    border-radius: 8px; background: var(--bg); color: var(--fg);
    font: inherit;
  }

  /* v0.4.23: membership editor styles. Sits between the pending queue
     and Org settings; rows are slightly tighter than queue rows so the
     panel scales to a real org's roster without dominating the view. */
  .members {
    margin-top: 2rem;
    border-top: 1px solid var(--border);
    padding-top: 1.4rem;
  }
  .members > .muted {
    color: var(--muted); margin: 0 0 0.8rem; font-size: 0.85rem;
  }
  .members ul {
    list-style: none; margin: 0; padding: 0;
  }
  .members > ul > li {
    border: 1px solid var(--border); border-radius: 10px;
    background: var(--panel); padding: 0.85rem 1rem;
    margin: 0 0 0.6rem;
  }
  .members > ul > li.open {
    border-color: rgba(212, 175, 55, 0.35);
  }
  .member-summary {
    display: flex; align-items: center; gap: 0.85rem;
  }
  .avatar {
    width: 2.4rem; height: 2.4rem; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    color: rgba(0,0,0,0.85); font-size: 0.82rem; font-weight: 600;
    flex-shrink: 0;
  }
  .member-meta {
    flex: 1; min-width: 0;
  }
  .member-name-row {
    display: flex; align-items: baseline; gap: 0.45rem;
    flex-wrap: wrap;
  }
  .member-name {
    font-weight: 600; font-size: 0.96rem;
  }
  .member-name.board {
    color: #e8c96b;
  }
  .role-tag {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    background: rgba(255,255,255,0.05);
    border: 1px solid var(--border);
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
  }
  .role-tag.board {
    color: #e8c96b;
    background: rgba(212, 175, 55, 0.08);
    border-color: rgba(212, 175, 55, 0.35);
  }
  .member-title {
    color: var(--muted); font-size: 0.85rem;
  }
  .member-sub {
    color: var(--muted); font-size: 0.83rem; margin-top: 0.1rem;
    overflow-wrap: anywhere;
  }
  .self-marker {
    color: rgb(120, 200, 140);
    font-weight: 500;
  }
  .row-form {
    margin-top: 0.85rem;
    padding-top: 0.85rem;
    border-top: 1px solid var(--border);
  }
  .row-form .small { font-size: 0.78rem; }
  .danger-form .warn {
    background: rgba(220, 38, 38, 0.06);
    border: 1px solid rgba(220, 38, 38, 0.3);
    color: #fca5a5;
    padding: 0.7rem 0.85rem;
    border-radius: 8px;
    font-size: 0.88rem;
    margin: 0 0 0.85rem;
    line-height: 1.45;
  }
  button.danger-filled {
    background: rgba(220, 38, 38, 0.85);
    color: #fff; border: none;
  }
  button.danger-filled:hover:not(:disabled) {
    background: rgb(220, 38, 38);
  }

  /* v0.4.24: revoked tombstones. Muted styling so they read as past
     events, not active members. Reasons get equal billing because
     'why did we revoke them?' is the audit question this section is
     here to answer. */
  .revoked {
    margin-top: 1.6rem;
    border-top: 1px solid var(--border);
    padding-top: 1.2rem;
  }
  .revoked > .muted {
    color: var(--muted); margin: 0 0 0.8rem; font-size: 0.85rem;
  }
  .revoked ul {
    list-style: none; margin: 0; padding: 0;
  }
  .revoked > ul > li {
    border: 1px solid var(--border); border-radius: 10px;
    background: rgba(255, 255, 255, 0.015);
    padding: 0.7rem 0.9rem;
    margin: 0 0 0.5rem;
    opacity: 0.85;
  }
  .revoked-summary {
    display: flex; gap: 1rem; align-items: flex-start;
    justify-content: space-between;
  }
  .revoked-meta {
    flex: 1; min-width: 0; display: flex; flex-direction: column;
    gap: 0.25rem;
  }
  .revoked-name-row {
    display: flex; align-items: baseline; gap: 0.45rem; flex-wrap: wrap;
  }
  .revoked-name {
    font-weight: 600; color: var(--muted);
    text-decoration: line-through;
    text-decoration-color: rgba(220, 38, 38, 0.5);
  }
  .revoked-reason {
    font-size: 0.88rem;
    color: var(--fg);
  }
  .revoked-fp {
    color: var(--muted);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.76rem;
    overflow-wrap: anywhere;
  }
  .revoked-at {
    color: var(--muted); font-size: 0.82rem; flex-shrink: 0;
  }

  .org-settings {
    margin-top: 2rem;
    border-top: 1px solid var(--border);
    padding-top: 1.4rem;
  }

  /* v0.4.25: roles × capabilities matrix. Small enough that a flat
     table reads better than a grid of cards. */
  /* v0.4.33: invites section. Sits between Org settings and Roles
     so the keymaster's day-to-day operations (mint invite, attest)
     live in adjacent UI surfaces. */
  .invites {
    margin-top: 2rem;
    border-top: 1px solid var(--border);
    padding-top: 1.4rem;
  }
  .invites-header {
    display: flex; justify-content: space-between; align-items: flex-start;
    gap: 1rem; margin-bottom: 0.85rem;
  }
  .invites-header h2 { margin: 0 0 0.3rem; }
  .invites-header p { margin: 0; font-size: 0.85rem; }
  .last-minted {
    border: 1px solid rgba(212, 175, 55, 0.4);
    background: rgba(212, 175, 55, 0.08);
    border-radius: 10px;
    padding: 0.9rem 1.1rem;
    margin: 0 0 1rem;
  }
  .code-row {
    display: flex; align-items: center; gap: 0.7rem;
    margin-top: 0.5rem;
  }
  .big-code {
    flex: 1;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 1.1rem;
    background: var(--bg);
    color: #e8c96b;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.55rem 0.75rem;
    letter-spacing: 0.04em;
    overflow-x: auto;
    white-space: nowrap;
  }
  .invite-list {
    list-style: none; margin: 0; padding: 0;
  }
  .invite-list li {
    border: 1px solid var(--border); border-radius: 10px;
    background: var(--panel); padding: 0.7rem 0.9rem;
    margin: 0 0 0.5rem;
  }
  .invite-row {
    display: flex; align-items: center; gap: 0.85rem; flex-wrap: wrap;
  }
  .invite-code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.84rem;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.25rem 0.55rem;
    overflow-x: auto;
    max-width: 100%;
  }
  .invite-meta {
    display: flex; gap: 0.5rem; align-items: center;
    color: var(--muted); font-size: 0.82rem;
    flex: 1; min-width: 0;
  }
  .invite-meta .hint { color: var(--fg); font-weight: 500; }
  .invite-actions { display: flex; gap: 0.4rem; }

  .roles {
    margin-top: 2rem;
    border-top: 1px solid var(--border);
    padding-top: 1.4rem;
  }
  .roles-header {
    display: flex; justify-content: space-between; align-items: flex-start;
    gap: 1rem;
  }
  .roles-header h2 { margin: 0 0 0.3rem; }
  .roles-header p { margin: 0; }
  .role-matrix {
    margin-top: 1rem;
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1rem 1.2rem;
    background: var(--panel);
  }
  .role-matrix table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9rem;
  }
  .role-matrix th, .role-matrix td {
    text-align: center;
    padding: 0.4rem 0.6rem;
  }
  .role-matrix th.role-col, .role-matrix td.role-col {
    text-align: left;
  }
  .role-matrix th {
    font-weight: 600;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-size: 0.74rem;
    border-bottom: 1px solid var(--border);
  }
  .role-matrix td {
    border-bottom: 1px solid rgba(255,255,255,0.03);
  }
  .role-matrix tr:last-child td { border-bottom: none; }
  .role-matrix input[type="checkbox"] {
    width: 1rem; height: 1rem; cursor: pointer; margin: 0;
  }
  .role-matrix code {
    background: rgba(255,255,255,0.04);
    padding: 0.08rem 0.4rem;
    border-radius: 4px;
    font-size: 0.85em;
  }
  .role-matrix .muted.small {
    font-size: 0.78rem; margin: 0.85rem 0 0;
    line-height: 1.5;
  }
  /* v0.4.26: add-role + remove affordances. */
  .role-matrix .add-role {
    display: flex;
    gap: 0.5rem;
    margin: 0.85rem 0 0;
  }
  .role-matrix .add-role input {
    flex: 1;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.4rem 0.6rem;
    font: inherit;
    font-size: 0.88rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .role-matrix .add-role input:focus {
    outline: none; border-color: rgba(212, 175, 55, 0.5);
  }
  .role-matrix .add-role button {
    padding: 0.4rem 1rem;
    font-size: 0.86rem;
  }
  .role-matrix th.remove-col,
  .role-matrix td.remove-col {
    width: 1.6rem;
    text-align: center;
  }
  .role-matrix .role-remove {
    background: transparent;
    border: 1px solid transparent;
    color: var(--muted);
    width: 1.3rem; height: 1.3rem;
    border-radius: 50%;
    cursor: pointer;
    font-size: 0.95rem;
    line-height: 1;
    padding: 0;
  }
  .role-matrix .role-remove:hover {
    background: rgba(220, 38, 38, 0.08);
    border-color: rgba(220, 38, 38, 0.4);
    color: #fca5a5;
  }
  .role-matrix .role-tag.muted {
    margin-left: 0.5rem;
    color: var(--muted);
    background: transparent;
    border: 1px dashed var(--border);
    font-size: 0.62rem;
  }
  .org-settings .muted {
    color: var(--muted); margin: 0 0 0.8rem; font-size: 0.85rem;
  }
  .danger-zone {
    margin-top: 1.6rem;
    display: flex;
    justify-content: flex-end;
  }

  /* v0.4.64: keypair groups section. Mirrors the roles section shape
     — same top border + spacing, same header layout — so the admin
     panel reads as a stack of same-shaped org-settings sections. */
  .groups {
    margin-top: 2rem;
    border-top: 1px solid var(--border);
    padding-top: 1.4rem;
  }
  .groups-header {
    display: flex; justify-content: space-between; align-items: flex-start;
    gap: 1rem;
  }
  .groups-header h2 { margin: 0 0 0.3rem; }
  .groups-header p { margin: 0; }
  .groups-list {
    list-style: none;
    margin: 1rem 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .groups-list li {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    padding: 0.5rem 0.8rem;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 0.9rem;
  }
  .group-name { font-weight: 600; }
  .group-count { font-size: 0.82rem; }
  .groups-editor {
    margin-top: 1rem;
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1rem 1.2rem;
    background: var(--panel);
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
  }
  .group-card {
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 0.9rem 1rem;
    background: rgba(255, 255, 255, 0.015);
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .group-card-head {
    display: flex;
    gap: 0.6rem;
    align-items: center;
  }
  .group-name-input {
    flex: 1;
    min-width: 0;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.4rem 0.6rem;
    font: inherit;
    font-size: 0.92rem;
  }
  .ghost.small { padding: 0.28rem 0.7rem; font-size: 0.82rem; }
  .group-member-list {
    list-style: none;
    margin: 0.35rem 0 0;
    padding: 0.4rem 0.6rem;
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    max-height: 12rem;
    overflow-y: auto;
    border: 1px solid rgba(255, 255, 255, 0.04);
    border-radius: 8px;
    background: var(--bg);
  }
  .group-member-list label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0;
    font-size: 0.88rem;
  }
  .group-member-list input[type="checkbox"] {
    width: 1rem; height: 1rem; cursor: pointer; margin: 0;
  }
  .groups-editor-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.6rem;
    margin-top: 0.2rem;
  }
  .warning { color: rgba(212, 175, 55, 0.9); margin: 0; }
</style>
