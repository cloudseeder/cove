<!--
  Inbox — the email-style landing view introduced in v0.4.19. One row
  per observed thread, sorted by latest activity. Each row carries the
  latest non-receipt entry preview (server-resolved display name + body
  snippet), an unread dot computed from /inbox.my_high_water, and the
  thread's entry count. Click a row → ThreadView.

  This panel sits inside the ThreadView layout, so the sidebar (with the
  Inbox / Admin links + per-thread list) is still on the left. The
  inbox just replaces the main pane.
-->
<script lang="ts">
  import { initials, authorColor, smartTimestamp } from '$lib/cove/chat';
  import type { AppState } from '$lib/cove/state.svelte';
  import type { InboxRow } from '$lib/cove/types';

  let { app }: { app: AppState } = $props();

  function isUnread(row: InboxRow): boolean {
    if (!row.latest_entry) return false;
    // latest_seq covers all kinds including receipts; my_high_water is
    // the seq of our latest receipt. Strict-greater handles the case
    // where my own receipt is the latest entry (not "unread to me").
    return row.latest_seq > row.my_high_water;
  }

  function authorLabel(row: InboxRow): string {
    const e = row.latest_entry;
    if (!e) return '';
    if (e.display_name) return e.display_name;
    return e.author.slice(0, 8) + '…';
  }

  function previewBody(row: InboxRow): string {
    const e = row.latest_entry;
    if (!e) return '(no activity)';
    if (e.kind === 'branch') return '↗ branched a new thread';
    if (e.kind === 'supersede') return 'edited an earlier entry';
    return e.body_preview || '(attachment)';
  }

  // v0.4.25: archived threads are filtered out of the main list by
  // default. A "Show archived" toggle reveals them in a muted section
  // below, so they're never lost — just out of the way.
  let showArchived = $state(false);
  const activeRows = $derived(
    app.inboxRows.filter((r) => !r.archived),
  );
  const archivedRows = $derived(
    app.inboxRows.filter((r) => r.archived),
  );

  // Sort: unread first, then by latest_seq desc within each group.
  const sortBy = (rows: InboxRow[]) => {
    const out = [...rows];
    out.sort((a, b) => {
      const ua = isUnread(a), ub = isUnread(b);
      if (ua !== ub) return ua ? -1 : 1;
      return b.latest_seq - a.latest_seq;
    });
    return out;
  };
  const sortedActive = $derived(sortBy(activeRows));
  const sortedArchived = $derived(sortBy(archivedRows));

  // v0.4.27: + New thread dialog. Audience radio decides whether the
  // thread is public or audience-scoped. For scoped threads the user
  // multi-selects from current members; the caller is auto-included.
  let newDialog = $state<{
    name: string;
    scope: 'public' | 'private';
    selected: Set<string>;       // pubkeys
    message: string;
    submitting: boolean;
    error: string | null;
  } | null>(null);

  function openNewDialog() {
    newDialog = {
      name: '',
      scope: 'public',
      selected: new Set<string>(),
      message: '',
      submitting: false,
      error: null,
    };
  }
  function closeNewDialog() {
    newDialog = null;
  }
  function togglePubkey(pk: string) {
    if (!newDialog) return;
    const next = new Set(newDialog.selected);
    if (next.has(pk)) next.delete(pk);
    else next.add(pk);
    newDialog = { ...newDialog, selected: next };
  }
  function sanitizeNewThreadName(s: string): string {
    return s.trim().toLowerCase().replace(/[^a-z0-9-]+/g, '-')
      .replace(/^-+|-+$/g, '');
  }
  async function submitNewThread() {
    if (!newDialog) return;
    const name = sanitizeNewThreadName(newDialog.name);
    if (!name) {
      newDialog = { ...newDialog, error: 'Thread name is required.' };
      return;
    }
    newDialog = { ...newDialog, submitting: true, error: null };
    try {
      if (newDialog.scope === 'private') {
        await app.createDirectThread({
          thread: name,
          pubkeys: Array.from(newDialog.selected),
          message: newDialog.message,
        });
      } else {
        await app.switchThread(name);
        if (newDialog.message.trim()) await app.post(newDialog.message);
      }
      closeNewDialog();
    } catch (err) {
      newDialog = {
        ...newDialog,
        submitting: false,
        error: (err as Error).message,
      };
    }
  }

  /** Caller's own pubkey — must be in the audience for a private
   *  thread. Excluded from the multi-select list (auto-included
   *  with a static checked badge). */
  const myPubkey = $derived(
    app.authStatus.kind === 'authenticated' ? app.authStatus.pubkey : '',
  );
  const otherMembers = $derived(
    app.members.filter((m) => m.member_pubkey !== myPubkey),
  );
</script>

<section class="inbox" aria-label="Inbox">
  <header>
    <div>
      <h1>Inbox</h1>
      <p class="muted">
        {sortedActive.length} thread{sortedActive.length === 1 ? '' : 's'}
        {#if sortedArchived.length > 0}
          · {sortedArchived.length} archived
        {/if}
        {#if app.inboxStatus.kind === 'loading'} · refreshing…{/if}
      </p>
    </div>
    <div class="head-right">
      <button type="button" class="new-thread-btn"
        title="Start a new thread (public or private to selected members)"
        onclick={openNewDialog}>+ New thread</button>
      <button type="button" class="refresh"
        title="Refresh inbox"
        disabled={app.inboxStatus.kind === 'loading'}
        onclick={() => app.loadInbox()}>↻</button>
    </div>
  </header>

  {#if app.inboxStatus.kind === 'error'}
    <div class="error">⚠ {app.inboxStatus.message}</div>
  {/if}

  {#snippet row(r: InboxRow)}
    {@const e = r.latest_entry}
    {@const unread = isUnread(r)}
    <li class:unread class:archived={r.archived}>
      <button type="button" onclick={() => app.switchThread(r.thread)}>
        <span class="dot" aria-hidden="true" class:unread></span>
        {#if e}
          <span class="avatar" aria-hidden="true"
            style="background: {authorColor(e.author)}">
            {initials(e.display_name ?? e.author.slice(0, 2))}
          </span>
        {:else}
          <span class="avatar empty" aria-hidden="true">·</span>
        {/if}
        <span class="middle">
          <span class="top-row">
            <span class="thread-name">{r.thread}</span>
            <span class="time">{e ? smartTimestamp(e.created_at) : '—'}</span>
          </span>
          <span class="preview">
            {#if e}
              <span class="author">{authorLabel(r)}</span>
              <span class="sep">—</span>
              <span class="body">{previewBody(r)}</span>
            {:else}
              <span class="body muted">{previewBody(r)}</span>
            {/if}
          </span>
          <span class="meta">
            {r.entry_count} {r.entry_count === 1 ? 'entry' : 'entries'}
            {#if unread && e}
              · {r.latest_seq - r.my_high_water} new
            {/if}
            {#if r.archived}
              · archived
            {/if}
          </span>
        </span>
      </button>
    </li>
  {/snippet}

  {#if sortedActive.length === 0 && sortedArchived.length === 0 && app.inboxStatus.kind !== 'loading'}
    <div class="empty">
      <p>No threads yet on this hub.</p>
      <p class="muted">
        Start one from the sidebar — type a name in
        <em>Start a new thread…</em> and post a message.
      </p>
    </div>
  {:else}
    <ul>
      {#each sortedActive as r (r.thread)}
        {@render row(r)}
      {/each}
    </ul>
    {#if sortedArchived.length > 0}
      <div class="archived-section">
        <button type="button" class="archived-toggle"
          onclick={() => (showArchived = !showArchived)}>
          {showArchived ? '▾' : '▸'} {sortedArchived.length} archived
        </button>
        {#if showArchived}
          <ul>
            {#each sortedArchived as r (r.thread)}
              {@render row(r)}
            {/each}
          </ul>
        {/if}
      </div>
    {/if}
  {/if}
</section>

{#if newDialog}
  <div class="modal-backdrop" onclick={closeNewDialog} role="presentation"></div>
  <div class="modal" role="dialog" aria-label="Start a new thread">
    <h3>New thread</h3>

    <label>
      <span>Thread name</span>
      <input type="text" bind:value={newDialog.name}
        placeholder="e.g. board-private-2026-q3"
        maxlength="64" autocapitalize="off"
        autocorrect="off" spellcheck="false" />
    </label>

    <fieldset class="scope">
      <legend>Audience</legend>
      <label class="radio">
        <input type="radio" bind:group={newDialog.scope} value="public" />
        <span>Everyone in the org</span>
      </label>
      <label class="radio">
        <input type="radio" bind:group={newDialog.scope} value="private" />
        <span>Just these people</span>
      </label>
    </fieldset>

    {#if newDialog.scope === 'private'}
      <div class="audience-list">
        <p class="self-line">
          ✓ <strong>You</strong> (auto-included as creator)
        </p>
        <ul>
          {#each otherMembers as m (m.member_pubkey)}
            <li>
              <label>
                <input type="checkbox"
                  checked={newDialog.selected.has(m.member_pubkey)}
                  onchange={() => togglePubkey(m.member_pubkey)} />
                <span class="name">{m.display_name}</span>
                {#if m.role !== 'member'}
                  <span class="role-tag">{m.role}</span>
                {/if}
              </label>
            </li>
          {/each}
        </ul>
        {#if otherMembers.length === 0}
          <p class="muted small">
            No other attested members on this hub yet. A private thread
            still works — you'll be the only audience member.
          </p>
        {/if}
      </div>
    {/if}

    <label>
      <span>First message</span>
      <textarea bind:value={newDialog.message} rows="3"
        placeholder="Type the first message (optional — you can post later)…"></textarea>
    </label>

    {#if newDialog.error}
      <p class="failure" role="alert">{newDialog.error}</p>
    {/if}

    <div class="modal-actions">
      <button type="button" class="ghost" onclick={closeNewDialog}
        disabled={newDialog.submitting}>Cancel</button>
      <button type="button" onclick={submitNewThread}
        disabled={newDialog.submitting || sanitizeNewThreadName(newDialog.name) === ''}>
        {newDialog.submitting
          ? 'Creating…'
          : (newDialog.scope === 'private' ? 'Create private thread' : 'Create thread')}
      </button>
    </div>
  </div>
{/if}

<style>
  .inbox {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg);
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 1.4rem 2rem 1rem;
    border-bottom: 1px solid var(--border);
  }
  header h1 {
    margin: 0 0 0.2rem 0;
    font-size: 1.4rem;
    font-weight: 600;
  }
  header .muted {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }
  .refresh {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
    font-size: 1rem;
    cursor: pointer;
    padding: 0.3rem 0.7rem;
    border-radius: 6px;
  }
  .refresh:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.04);
    color: var(--fg);
  }
  .refresh:disabled {
    opacity: 0.4;
    cursor: wait;
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
    overflow-y: auto;
    flex: 1;
  }
  li {
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  }
  li button {
    width: 100%;
    display: grid;
    grid-template-columns: 1rem 2.4rem 1fr;
    gap: 0.85rem;
    align-items: center;
    padding: 0.85rem 2rem;
    background: transparent;
    border: none;
    text-align: left;
    cursor: pointer;
    color: var(--fg);
    font: inherit;
  }
  li button:hover {
    background: rgba(255, 255, 255, 0.03);
  }
  .dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: transparent;
    justify-self: center;
  }
  .dot.unread {
    background: #d4af37;
    box-shadow: 0 0 6px rgba(212, 175, 55, 0.5);
  }
  .avatar {
    width: 2.4rem;
    height: 2.4rem;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.85rem;
    font-weight: 600;
    color: #fff;
    flex-shrink: 0;
  }
  .avatar.empty {
    background: rgba(255, 255, 255, 0.05);
    color: var(--muted);
  }
  .middle {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    min-width: 0;
  }
  .top-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 1rem;
  }
  .thread-name {
    font-weight: 600;
    color: var(--fg);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  li.unread .thread-name {
    color: #e8c96b;
  }
  .time {
    font-size: 0.78rem;
    color: var(--muted);
    flex-shrink: 0;
    font-variant-numeric: tabular-nums;
  }
  .preview {
    font-size: 0.88rem;
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    display: flex;
    gap: 0.4rem;
  }
  .preview .author {
    color: var(--fg);
    font-weight: 500;
    flex-shrink: 0;
  }
  .preview .sep {
    flex-shrink: 0;
  }
  .preview .body {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .meta {
    font-size: 0.75rem;
    color: var(--muted);
  }
  /* v0.4.25: archived section sits below the active list, collapsed
     by default. Rows render in the same shape but muted. */
  .archived-section {
    margin-top: 0.8rem;
    border-top: 1px solid rgba(255,255,255,0.05);
    padding-top: 0.4rem;
  }
  .archived-toggle {
    appearance: none;
    background: transparent;
    border: none;
    color: var(--muted);
    cursor: pointer;
    padding: 0.4rem 2rem;
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-family: inherit;
  }
  .archived-toggle:hover { color: var(--fg); }
  li.archived {
    opacity: 0.55;
  }
  li.archived:hover { opacity: 0.85; }
  .empty {
    padding: 3rem 2rem;
    text-align: center;
    color: var(--muted);
  }
  .empty em {
    color: var(--fg);
    font-style: normal;
    background: rgba(255, 255, 255, 0.06);
    padding: 0.1rem 0.45rem;
    border-radius: 4px;
  }
  .error {
    background: rgba(220, 60, 60, 0.12);
    color: #ff7777;
    padding: 0.75rem 2rem;
    font-size: 0.9rem;
  }

  /* v0.4.27: + New thread button + audience dialog. The button sits
     next to the refresh control; the dialog is modal-style (backdrop
     dims the inbox so the audience pick is the focus). */
  .new-thread-btn {
    background: #d4af37;
    color: #0a0a0a;
    border: none;
    border-radius: 6px;
    padding: 0.36rem 0.85rem;
    font-size: 0.84rem;
    font-weight: 600;
    cursor: pointer;
  }
  .new-thread-btn:hover { background: #e2bf4e; }

  :global(.modal-backdrop) {
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.5);
    z-index: 50;
  }
  :global(.modal) {
    position: fixed;
    z-index: 51;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    max-width: 520px; width: calc(100vw - 4rem);
    max-height: calc(100vh - 4rem);
    overflow-y: auto;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.4rem 1.6rem;
  }
  :global(.modal h3) { margin: 0 0 0.85rem; font-size: 1.1rem; }
  :global(.modal label) { display: block; margin: 0.7rem 0; }
  :global(.modal label > span) {
    display: block; font-size: 0.82rem; color: var(--muted);
    margin-bottom: 0.3rem;
  }
  :global(.modal input[type="text"]),
  :global(.modal textarea) {
    width: 100%; box-sizing: border-box;
    background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 0.45rem 0.65rem; font: inherit; font-size: 0.9rem;
  }
  :global(.modal textarea) {
    font-family: inherit;
    resize: vertical;
  }

  fieldset.scope {
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.6rem 0.85rem 0.4rem;
    margin: 0.85rem 0;
  }
  fieldset.scope legend {
    padding: 0 0.4rem;
    font-size: 0.78rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  label.radio {
    display: flex; align-items: center; gap: 0.5rem;
    margin: 0.3rem 0;
    font-size: 0.92rem;
  }
  label.radio input { margin: 0; }
  label.radio span { display: inline; font-size: inherit; color: inherit; }

  .audience-list {
    border: 1px dashed var(--border);
    border-radius: 8px;
    padding: 0.7rem 0.85rem;
    margin: 0.85rem 0;
    max-height: 12rem;
    overflow-y: auto;
  }
  .audience-list .self-line {
    margin: 0 0 0.5rem;
    font-size: 0.88rem;
    color: rgb(120, 200, 140);
  }
  .audience-list ul {
    list-style: none; margin: 0; padding: 0;
  }
  .audience-list li {
    padding: 0.15rem 0;
  }
  .audience-list label {
    display: flex; align-items: center; gap: 0.55rem;
    margin: 0; cursor: pointer;
    font-size: 0.9rem;
  }
  .audience-list label .name { font-weight: 500; color: var(--fg); }
  .audience-list label .role-tag {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border);
    padding: 0.05rem 0.4rem;
    border-radius: 999px;
  }

  .modal-actions {
    display: flex; justify-content: flex-end; gap: 0.5rem;
    margin-top: 1rem;
  }
  .modal-actions button {
    background: #d4af37; color: #0a0a0a;
    border: none; border-radius: 999px;
    padding: 0.5rem 1.2rem; font: inherit; font-weight: 600;
    cursor: pointer;
  }
  .modal-actions button:hover:not(:disabled) { background: #e2bf4e; }
  .modal-actions button:disabled {
    background: var(--border); color: var(--muted); cursor: not-allowed;
  }
  .modal-actions button.ghost {
    background: transparent; border: 1px solid var(--border);
    color: var(--muted);
  }
  .modal-actions button.ghost:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.04); color: var(--fg);
  }
  .failure {
    margin: 0.7rem 0 0; padding: 0.5rem 0.75rem;
    background: rgba(220, 38, 38, 0.08);
    border: 1px solid rgba(220, 38, 38, 0.4);
    color: #fca5a5; font-size: 0.86rem;
    border-radius: 6px;
  }
  :global(.modal .small) { font-size: 0.78rem; }
  :global(.modal .muted) { color: var(--muted); }
</style>
