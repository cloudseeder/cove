<!--
  ThreadList — left sidebar in the ThreadView layout. Renders the
  observed threads from the hub with a 'start a new thread' input at
  the bottom. Active thread is highlighted.

  Threads are open-namespace: typing a new name and submitting just
  switches the active thread to it. The thread materializes on the hub
  when the first entry is posted to it. No 'create' API to call.
-->
<script lang="ts">
  import { sanitizeThreadName } from '$lib/cove/threadname';
  import type { AppState } from '$lib/cove/state.svelte';
  import HubSwitcher from './HubSwitcher.svelte';

  let { app }: { app: AppState } = $props();

  let newThreadName = $state('');

  /** v0.4.65: user's own pubkey — needed for federation flows where
   *  the user tells a different hub's admin what to attest. Auto-
   *  generated on-device by the client, so historically never surfaced
   *  in the UI. Now shown as a truncated chip at the bottom of the
   *  sidebar; click to copy the full 64-char hex to clipboard. */
  const myPk = $derived(
    app.authStatus.kind === 'authenticated' ? app.authStatus.pubkey : '',
  );
  const myName = $derived(app.myAttestation?.display_name ?? '');
  const shortPk = $derived(
    myPk ? `${myPk.slice(0, 6)}…${myPk.slice(-4)}` : '',
  );
  let copyState = $state<'idle' | 'copied' | 'failed'>('idle');
  let copyResetTimer: ReturnType<typeof setTimeout> | null = null;
  async function copyMyPubkey() {
    if (!myPk) return;
    try {
      await navigator.clipboard.writeText(myPk);
      copyState = 'copied';
    } catch {
      copyState = 'failed';
    }
    if (copyResetTimer) clearTimeout(copyResetTimer);
    copyResetTimer = setTimeout(() => (copyState = 'idle'), 1500);
  }

  /** v0.1.10: file count for the active thread, derived from the
   *  attachments across all loaded entries (top-level + replies).
   *  Shown next to the 'Files' sub-button so the user can see at a
   *  glance whether there's anything to browse. */
  const fileCount = $derived(
    app.entries.reduce((n, ve) => n + ve.entry.blobs.length, 0),
  );

  /** v0.4.19: count of inbox rows where latest_seq > my_high_water.
   *  Drives the badge on the "Inbox" sidebar link. */
  const inboxUnread = $derived(
    app.inboxRows.filter((r) => r.latest_entry && r.latest_seq > r.my_high_water).length,
  );

  // v0.2: sub-threads nest under their parent. A thread without a
  // parent_thread renders at the top level; children are indented
  // under it. We build the tree at render time from the flat list
  // returned by /threads — parent_thread on each row is enough.
  // v0.4.25: archived threads are filtered out of the main tree and
  // shown in a collapsible "Archived" section so they don't clutter
  // day-to-day navigation. Show-archived expands them on demand.
  type ThreadNode = {
    thread: string;
    entry_count: number;
    latest_seq: number;
    type?: 'permanent' | 'ephemeral' | 'tombstoned';
    expires_at?: string | null;
    children: ThreadNode[];
  };
  const activeThreads = $derived(app.threads.filter((t) => !t.archived));
  const archivedThreads = $derived(app.threads.filter((t) => t.archived));
  let showArchived = $state(false);

  const tree = $derived.by(() => {
    const byName = new Map<string, ThreadNode>();
    for (const t of activeThreads) {
      byName.set(t.thread, {
        thread: t.thread,
        entry_count: t.entry_count,
        latest_seq: t.latest_seq,
        type: t.type,
        expires_at: t.expires_at,
        children: [],
      });
    }
    const roots: ThreadNode[] = [];
    for (const t of activeThreads) {
      const node = byName.get(t.thread)!;
      const parent = t.parent_thread ? byName.get(t.parent_thread) : null;
      if (parent) parent.children.push(node);
      else roots.push(node);
    }
    return roots;
  });

  /** v0.4.38: short human relative-time label for the ephemeral badge.
   *  "3d" for days, "5h" for hours, "just now" if past-due (auto-seal
   *  will catch up shortly). */
  function relativeExpiry(iso: string | null | undefined): string {
    if (!iso) return '';
    const ms = new Date(iso).getTime() - Date.now();
    if (ms <= 0) return 'expired';
    const days = Math.floor(ms / 86_400_000);
    if (days >= 2) return `${days}d`;
    const hours = Math.floor(ms / 3_600_000);
    if (hours >= 2) return `${hours}h`;
    const mins = Math.max(1, Math.floor(ms / 60_000));
    return `${mins}m`;
  }

  async function handleSwitch(name: string) {
    await app.switchThread(name);
    // v0.4.45: on mobile, close the drawer after picking a thread so
    // the newly-selected content is actually visible. Desktop keeps
    // the sidebar open since it's inline.
    if (typeof window !== 'undefined'
        && window.matchMedia('(max-width: 640px)').matches) {
      app.closeSidebar();
    }
  }

  function handleInbox() {
    app.goToInbox();
    if (typeof window !== 'undefined'
        && window.matchMedia('(max-width: 640px)').matches) {
      app.closeSidebar();
    }
  }

  async function handleNewThread(ev: SubmitEvent) {
    ev.preventDefault();
    const name = sanitizeThreadName(newThreadName);
    if (!name) return;
    // v0.4.39: route through the shared new-thread dialog so the
    // sidebar entry point offers the same audience + retention
    // controls as the InboxPanel button. Pre-fill the name so
    // the user's typing carries over.
    newThreadName = '';
    app.openNewThreadDialog();
    if (app.newThreadDialog) {
      app.newThreadDialog.name = name;
    }
  }
</script>

<aside class="thread-list" aria-label="Threads">
  <header>
    <h2>Threads</h2>
    <div class="header-actions">
      <!-- v0.4.30: + New thread button mirrors the InboxPanel one so
           a user inside any thread can compose a fresh one without
           navigating back to Inbox. Same dialog, same state. -->
      <button
        type="button"
        class="new-thread"
        title="Start a new thread (public or private to selected members)"
        onclick={() => app.openNewThreadDialog()}
      >
        +
      </button>
      <button
        type="button"
        class="refresh"
        title="Refresh thread list"
        onclick={() => app.loadThreads()}
      >
        ↻
      </button>
      <!-- v0.4.58: collapse chevron lives INSIDE the sidebar header so it
           can never overlap sidebar content. The complementary "expand"
           button (☰) lives over the main pane in ThreadView, shown only
           when the sidebar is closed — mutually exclusive with this one. -->
      <button
        type="button"
        class="collapse"
        title="Hide threads panel"
        aria-label="Hide threads panel"
        onclick={() => app.closeSidebar()}
      >
        ‹
      </button>
    </div>
  </header>

  <!-- v0.4.69: hub switcher. Renders only when the user has joined at
       least one hub (i.e., always after initial onboarding). Clicking
       a row swaps the active hub; every delegating getter on AppState
       follows so the thread list, inbox, and everything else in the
       pane flip to that hub's data. -->
  <HubSwitcher {app} />

  {#snippet activeSubNav()}
    <ul class="sub">
      <li class:active={app.view === 'messages'}>
        <button type="button" onclick={() => app.setView('messages')}>
          Messages
        </button>
      </li>
      <li class:active={app.view === 'files'}>
        <button type="button" onclick={() => app.setView('files')}>
          <span>Files</span>
          <span class="count">{fileCount}</span>
        </button>
      </li>
    </ul>
  {/snippet}

  {#snippet threadNode(node: ThreadNode)}
    {@const isActive = app.route === 'thread' && node.thread === app.thread}
    <li class:active={isActive} class:ephemeral={node.type === 'ephemeral'} class:tombstoned={node.type === 'tombstoned'}>
      <button type="button" onclick={() => handleSwitch(node.thread)}>
        <span class="name">{node.thread}</span>
        {#if node.type === 'ephemeral'}
          <span class="eph-badge" title="Ephemeral — deletes on {node.expires_at}">
            ⏳ {relativeExpiry(node.expires_at)}
          </span>
        {:else if node.type === 'tombstoned'}
          <span class="eph-badge tombstoned" title="Tombstoned">⚰</span>
        {/if}
        <span class="count">{node.entry_count}</span>
      </button>
      {#if isActive}
        {@render activeSubNav()}
      {/if}
      {#if node.children.length > 0}
        <ul class="children">
          {#each node.children as child (child.thread)}
            {@render threadNode(child)}
          {/each}
        </ul>
      {/if}
    </li>
  {/snippet}

  <ul>
    <li class:active={app.route === 'inbox'} class="inbox-tab">
      <button type="button" onclick={handleInbox}>
        <span class="name">Inbox</span>
        {#if inboxUnread > 0}
          <span class="count badge">{inboxUnread}</span>
        {/if}
      </button>
    </li>
    {#if app.isBoardMember}
      <li class:active={app.route === 'thread' && app.view === 'admin'} class="admin-tab">
        <button type="button" onclick={() => app.setView('admin')}>
          <span class="name">Admin</span>
          {#if app.pendingQueue.length > 0}
            <span class="count badge">{app.pendingQueue.length}</span>
          {/if}
        </button>
      </li>
    {/if}
    {#each tree as node (node.thread)}
      {@render threadNode(node)}
    {/each}
    {#if app.route === 'thread' && !app.threads.some((t) => t.thread === app.thread)}
      <!-- Current thread isn't in the hub list yet (empty / just-typed
           a fresh name). Show it as active anyway so the user can see
           where they are. -->
      <li class="active pending">
        <button type="button" disabled>
          <span class="name">{app.thread}</span>
          <span class="count">—</span>
        </button>
        {@render activeSubNav()}
      </li>
    {/if}
    {#if archivedThreads.length > 0}
      <li class="archived-toggle-row">
        <button type="button" class="archived-toggle"
          onclick={() => (showArchived = !showArchived)}>
          {showArchived ? '▾' : '▸'} {archivedThreads.length} archived
        </button>
      </li>
      {#if showArchived}
        {#each archivedThreads as t (t.thread)}
          {@const isActive = app.route === 'thread' && t.thread === app.thread}
          <li class:active={isActive} class="archived">
            <button type="button" onclick={() => handleSwitch(t.thread)}>
              <span class="name">{t.thread}</span>
              <span class="count">{t.entry_count}</span>
            </button>
            {#if isActive}
              {@render activeSubNav()}
            {/if}
          </li>
        {/each}
      {/if}
    {/if}
  </ul>

  <form onsubmit={handleNewThread}>
    <input
      type="text"
      bind:value={newThreadName}
      placeholder="Start a new thread…"
      maxlength="64"
      autocapitalize="off"
      autocorrect="off"
      spellcheck="false"
    />
    <button type="submit" disabled={!newThreadName.trim()}>+</button>
  </form>

  <!-- v0.4.65: identity chip. Surfaces the user's own pubkey so a
       cross-hub attestation flow — telling a different hub's admin
       "attest me under X" — doesn't require them to fish it out of
       a manifest or a devtools session. Click to copy the full hex. -->
  {#if myPk}
    <button type="button" class="identity"
      title="Click to copy your full public key"
      onclick={copyMyPubkey}>
      <span class="identity-row">
        {#if myName}
          <span class="identity-name">{myName}</span>
        {/if}
        <span class="identity-copy-state" aria-live="polite">
          {#if copyState === 'copied'}✓ copied{:else if copyState === 'failed'}⚠ failed{/if}
        </span>
      </span>
      <span class="identity-key" title={myPk}>{shortPk}</span>
    </button>
  {/if}

  {#if app.appVersion}
    <footer class="version" title="Cove app version">
      v{app.appVersion}
    </footer>
  {/if}
</aside>

<style>
  .thread-list {
    display: flex;
    flex-direction: column;
    width: 240px;
    border-right: 1px solid var(--border);
    background: var(--panel);
    height: 100%;
    overflow: hidden;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.1rem 1.25rem 0.5rem;
  }
  header h2 {
    margin: 0;
    font-size: 0.75rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    font-weight: 600;
  }
  .header-actions {
    display: flex; align-items: center; gap: 0.25rem;
  }
  .refresh, .new-thread, .collapse {
    background: transparent;
    border: none;
    color: var(--muted);
    font-size: 1.1em;
    cursor: pointer;
    padding: 0.1em 0.4em;
    border-radius: 4px;
    line-height: 1;
  }
  .refresh:hover, .new-thread:hover, .collapse:hover {
    background: rgba(255, 255, 255, 0.04);
    color: var(--fg);
  }
  .collapse { font-size: 1.25em; }
  .new-thread {
    /* The + button is the primary action; tint it gold so it doesn't
       disappear next to the refresh control. */
    color: #d4af37;
    font-weight: 600;
    font-size: 1.2em;
  }
  .new-thread:hover { color: #e8c96b; }
  ul {
    list-style: none;
    padding: 0.4rem 0.5rem;
    margin: 0;
    flex: 1;
    overflow-y: auto;
  }
  li {
    margin: 0.1rem 0;
  }
  li button {
    width: 100%;
    text-align: left;
    background: transparent;
    border: none;
    color: var(--fg);
    padding: 0.5rem 0.7rem;
    border-radius: 6px;
    cursor: pointer;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
    font: inherit;
    font-size: 0.92rem;
  }
  li button:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.04);
  }
  li.active > button {
    background: rgba(212, 175, 55, 0.12);
    color: #e8c96b;
  }
  li.active.pending > button {
    color: var(--muted);
    cursor: default;
  }
  ul.sub {
    list-style: none;
    margin: 0.25rem 0 0.35rem 0.6rem;
    padding: 0;
    border-left: 1px solid var(--border);
    padding-left: 0.4rem;
  }
  ul.sub li {
    margin: 0;
  }
  ul.sub button {
    padding: 0.32rem 0.55rem;
    font-size: 0.82rem;
    color: var(--muted);
  }
  ul.sub li.active > button {
    background: rgba(212, 175, 55, 0.08);
    color: #e8c96b;
  }
  /* v0.2: sub-thread nesting. Each level indents under its parent and
     marks the relationship with a thin left rule. Tinted to distinguish
     from the gold Messages/Files sub-list above. */
  ul.children {
    list-style: none;
    margin: 0.1rem 0 0.25rem 0.85rem;
    padding-left: 0.4rem;
    border-left: 2px solid rgba(160, 200, 130, 0.35);
  }
  .inbox-tab > button {
    color: #e8c96b;
  }
  .inbox-tab + .admin-tab,
  .inbox-tab + li:not(.admin-tab) {
    /* Visual separator below the navigation block so it doesn't blend
       into the thread list. */
    margin-top: 0.35rem;
  }
  .admin-tab > button {
    color: #e8c96b;
  }
  /* v0.4.25: archived threads in the sidebar — collapsed by default,
     muted when expanded so they don't compete with active threads. */
  .archived-toggle-row {
    margin: 0.4rem 0 0;
  }
  .archived-toggle {
    width: 100%;
    background: transparent;
    border: none;
    color: var(--muted);
    cursor: pointer;
    padding: 0.32rem 0.7rem;
    font-size: 0.74rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    text-align: left;
    font-family: inherit;
  }
  .archived-toggle:hover { color: var(--fg); }
  li.archived {
    opacity: 0.55;
  }
  li.archived:hover { opacity: 0.85; }
  .badge {
    background: #d4af37; color: #0a0a0a;
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    font-weight: 600;
  }
  .name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .count {
    font-size: 0.78rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  /* v0.4.38: ephemeral / tombstoned pills next to the thread name. */
  .eph-badge {
    font-size: 0.7rem;
    padding: 0.05rem 0.35rem;
    border-radius: 999px;
    background: rgba(212, 175, 55, 0.12);
    color: #e8c96b;
    border: 1px solid rgba(212, 175, 55, 0.35);
    white-space: nowrap;
  }
  .eph-badge.tombstoned {
    background: rgba(120, 120, 120, 0.15);
    color: var(--muted);
    border-color: var(--border);
  }
  li.ephemeral > button > .name {
    font-style: italic;
  }
  li.tombstoned > button {
    opacity: 0.7;
  }
  form {
    display: flex;
    gap: 0.4rem;
    padding: 0.7rem 0.75rem 0.9rem;
    border-top: 1px solid var(--border);
  }
  input {
    flex: 1;
    /* v0.4.58: without min-width:0 a flex text input refuses to shrink
       below its intrinsic content size (~150-180px), pushing the +
       submit button past the sidebar's 240px column and getting clipped
       by the .thread-list overflow:hidden. */
    min-width: 0;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.4rem 0.6rem;
    font: inherit;
    font-size: 0.88rem;
  }
  input:focus {
    outline: none;
    border-color: rgba(212, 175, 55, 0.5);
  }
  form button {
    background: #d4af37;
    color: #0a0a0a;
    border: none;
    border-radius: 6px;
    padding: 0 0.8rem;
    font-weight: 700;
    cursor: pointer;
  }
  /* v0.4.65: identity chip in the sidebar footer. Truncated pubkey +
     name; click to copy the full hex. Hover picks up the gold accent
     so it's visibly interactive. */
  .identity {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    width: 100%;
    background: transparent;
    border: none;
    border-top: 1px solid var(--border);
    color: var(--fg);
    padding: 0.55rem 1.25rem 0.35rem;
    text-align: left;
    cursor: pointer;
    font: inherit;
    transition: background 120ms ease;
  }
  .identity:hover {
    background: rgba(212, 175, 55, 0.05);
  }
  .identity-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 0.35rem;
  }
  .identity-name {
    font-size: 0.82rem;
    font-weight: 500;
    color: var(--fg);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .identity-copy-state {
    font-size: 0.7rem;
    color: rgba(212, 175, 55, 0.9);
    white-space: nowrap;
  }
  .identity-key {
    color: var(--muted);
    font-size: 0.72rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    user-select: all;
  }
  .version {
    color: var(--muted);
    font-size: 0.72rem;
    padding: 0.4rem 1.25rem 0.75rem;
    text-align: center;
    user-select: none;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  form button:disabled {
    background: var(--border);
    color: var(--muted);
    cursor: not-allowed;
  }
</style>
