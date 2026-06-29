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
  import { fingerprint } from '$lib/cove/pairing';
  import { sanitizeThreadName } from '$lib/cove/threadname';
  import type { AppState } from '$lib/cove/state.svelte';
  import type { Attestation } from '$lib/cove/types';
  import {
    CAPABILITIES, DEFAULT_CAPABILITIES_BY_ROLE, type Capability,
  } from '$lib/cove/types';

  interface Props {
    app: AppState;
  }
  let { app }: Props = $props();

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
  });

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
    <!-- Step 1: Import root keys (one-time setup per keymaster device). -->
    <div class="root-setup">
      <h2>Set up root key custody</h2>
      <p class="muted">
        This device is the keymaster station. Import your org root keypair
        so you can attest members from inside Cove. The private key goes
        straight to your OS keychain — it never returns to the app and
        never reaches the hub.
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
          {rootImporting ? 'Importing…' : 'Import root key'}
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
              </tr>
            </thead>
            <tbody>
              {#each observedRoles as role}
                <tr>
                  <td class="role-col"><code>{role}</code></td>
                  {#each CAPABILITIES as cap}
                    <td>
                      <input type="checkbox"
                        checked={(rolesDraft[role] ?? []).includes(cap)}
                        onchange={() => toggleCap(role, cap)} />
                    </td>
                  {/each}
                </tr>
              {/each}
            </tbody>
          </table>
          <p class="muted small">
            Capabilities are protocol-defined.
            <code>admin</code> sees the admin panel + pending queue.
            <code>archive</code> archives or reopens threads.
            Saving root-signs an updated directory manifest — every
            connected client refreshes immediately via /stream.
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

    <section class="danger-zone">
      <button type="button" class="ghost" onclick={clearRoot}>
        Forget root key on this device
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
  .org-settings .muted {
    color: var(--muted); margin: 0 0 0.8rem; font-size: 0.85rem;
  }
  .danger-zone {
    margin-top: 1.6rem;
    display: flex;
    justify-content: flex-end;
  }
</style>
