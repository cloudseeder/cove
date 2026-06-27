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
  import type { AppState } from '$lib/cove/state.svelte';

  interface Props {
    app: AppState;
  }
  let { app }: Props = $props();

  let rootPriv = $state('');
  let rootPub = $state('');
  let rootImporting = $state(false);
  let rootImportError = $state<string | null>(null);

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

  function shortFp(pk: string): string {
    return pk.slice(0, 8).toUpperCase() + '…' + pk.slice(-4).toUpperCase();
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
      <button type="button" class="ghost" onclick={clearRoot}>
        Forget root key on this device
      </button>
    </div>

  {:else}
    <ul class="queue">
      {#each app.pendingQueue as row (row.pubkey)}
        <li>
          <div class="row-summary">
            <div class="row-meta">
              <span class="row-name">{row.name_hint}</span>
              <span class="row-fp">{shortFp(row.pubkey)}</span>
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
    font-size: 0.8rem;
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
</style>
