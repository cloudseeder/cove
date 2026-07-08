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
  <!-- v0.4.82: sidebar reorg.
         header       — brand + admin key (board-only) + collapse
         inbox-pinned — email-style "what's new" landing, always visible
         threads      — section header + list, scrollable middle region
         hubs-pinned  — pinned above identity chip
         identity     — user's own pubkey chip + version footer
       Threads dominate visually (largest region) with Inbox as the
       primary shortcut above them; hubs pin at the bottom so multi-hub
       users can flip without losing the thread list to a scroll. -->
  <header>
    <h2>Cove</h2>
    <div class="header-actions">
      {#if app.isBoardMember}
        <!-- v0.4.82: admin key. Board-role only; hidden entirely for
             non-admin members. Jumps straight to the Admin route
             (single click, replaces main pane) — same navigation
             semantics as clicking a thread. -->
        <button
          type="button"
          class="admin-icon"
          class:active={app.route === 'thread' && app.view === 'admin'}
          title="Admin — keymaster tools"
          aria-label="Admin — keymaster tools"
          onclick={() => app.setView('admin')}
        >
          🔑
          {#if app.pendingQueue.length > 0}
            <span class="pending-dot" aria-label="{app.pendingQueue.length} pending"></span>
          {/if}
        </button>
      {/if}
      <button
        type="button"
        class="collapse"
        title="Hide sidebar"
        aria-label="Hide sidebar"
        onclick={() => app.closeSidebar()}
      >
        ❮
      </button>
    </div>
  </header>

  <!-- Inbox: pinned primary landing shortcut. "What's new across every
       thread" — email-style. Users returning to Cove hit this first,
       then either drill into a specific row or scroll to the thread
       they want in the Threads list below. -->
  <button
    type="button"
    class="inbox-pinned"
    class:active={app.route === 'inbox'}
    onclick={handleInbox}
  >
    <span class="inbox-icon" aria-hidden="true">📥</span>
    <span class="inbox-label">Inbox</span>
    {#if inboxUnread > 0}
      <span class="badge">{inboxUnread}</span>
    {/if}
  </button>

  <!-- Threads section header with the + and refresh controls that used
       to live in the top header. Sits above the scrollable thread list. -->
  <div class="threads-section-header">
    <span class="section-label">Threads</span>
    <div class="section-actions">
      <button type="button" class="new-thread"
        title="Start a new thread"
        onclick={() => app.openNewThreadDialog()}>+</button>
      <button type="button" class="refresh"
        title="Refresh thread list"
        onclick={() => app.loadThreads()}>↻</button>
    </div>
  </div>

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

  <!-- v0.4.82: Inbox + Admin moved out of this list — Inbox is now the
       pinned button above the section header, Admin is the 🔑 key in
       the top-right header. This list is thread-only. -->
  <ul class="threads-scroll">
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

  <!-- v0.4.82: hub switcher moved from the top of the sidebar to just
       above the identity chip. Multi-hub users flip hubs without
       losing the thread list to a scroll (the thread list stays in
       its scrollable region above). -->
  <HubSwitcher {app} />

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
      <span class="version-sep">·</span>
      <button type="button" class="check-updates"
        onclick={() => app.checkForUpdate({ silent: false })}
        disabled={app.updateStatus.kind === 'checking'}>
        {app.updateStatus.kind === 'checking' ? 'Checking…' : 'Check for updates'}
      </button>
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
    /* v0.4.82: was 'THREADS' small caps; now 'Cove' brand mark at the
       top of the sidebar since threads have their own section header
       lower down. */
    margin: 0;
    font-size: 1.05rem;
    letter-spacing: -0.005em;
    color: var(--fg);
    font-weight: 600;
    font-family: "Iowan Old Style", "Palatino", "Georgia", ui-serif, serif;
  }
  .header-actions {
    display: flex; align-items: center; gap: 0.4rem;
  }
  /* v0.4.82: admin key (board-only). Sized to match the collapse
     chevron's 44px tap target so the top-right cluster is balanced. */
  .admin-icon {
    position: relative;
    background: transparent;
    border: none;
    color: var(--fg);
    font-size: 1.15rem;
    cursor: pointer;
    padding: 0.3em 0.55em;
    border-radius: 6px;
    line-height: 1;
    min-width: 44px;
    min-height: 44px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .admin-icon:hover { background: var(--hover); }
  .admin-icon.active {
    background: rgba(212, 175, 55, 0.15);
  }
  .admin-icon .pending-dot {
    position: absolute;
    top: 0.35rem;
    right: 0.35rem;
    width: 0.55rem;
    height: 0.55rem;
    background: #d4af37;
    border: 2px solid var(--panel);
    border-radius: 50%;
    box-sizing: content-box;
  }
  /* v0.4.82: Inbox pinned above the threads section. Full-width tap
     target, prominent icon + label + unread badge. */
  .inbox-pinned {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    background: transparent;
    border: none;
    color: var(--fg);
    padding: 0.7rem 1.25rem;
    margin: 0 0.5rem 0.3rem;
    border-radius: 8px;
    cursor: pointer;
    font: inherit;
    font-size: 0.95rem;
    text-align: left;
    width: calc(100% - 1rem);
  }
  .inbox-pinned:hover { background: var(--hover); }
  .inbox-pinned.active {
    background: rgba(212, 175, 55, 0.12);
    color: var(--fg);
  }
  .inbox-pinned .inbox-icon { font-size: 1.1rem; }
  .inbox-pinned .inbox-label { flex: 1; font-weight: 500; }
  .inbox-pinned .badge {
    background: #d4af37;
    color: #0a0a0a;
    border-radius: 999px;
    font-size: 0.7rem;
    font-weight: 700;
    padding: 0.1rem 0.5rem;
    line-height: 1.4;
  }
  /* v0.4.82: threads section header. Small caps section label plus
     the + / ↻ controls that used to live in the top header. */
  .threads-section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.5rem 1.25rem 0.2rem;
    border-top: 1px solid var(--border);
    margin-top: 0.2rem;
  }
  .threads-section-header .section-label {
    font-size: 0.72rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    font-weight: 600;
  }
  .threads-section-header .section-actions {
    display: flex; align-items: center; gap: 0.2rem;
  }
  .threads-section-header .new-thread,
  .threads-section-header .refresh {
    /* Same visual as the header buttons but slightly smaller since
       this row is a subheader, not the top nav. */
    min-width: 32px;
    min-height: 32px;
    font-size: 1.1em;
    padding: 0.2em 0.4em;
  }
  .threads-section-header .new-thread { font-size: 1.3em; }
  /* v0.4.79: sizes bumped from 1.1em / 0.1em 0.4em → 1.4em /
     0.3em 0.55em so board users on desktop and touch targets on
     phones both have a comfortable click area. Every button also
     carries min-width/min-height of 44px (Apple's touch guideline)
     so slim glyphs like the collapse chevron get a full-size hit
     region even when their visual weight is thin. */
  .refresh, .new-thread, .collapse {
    background: transparent;
    border: none;
    color: var(--muted);
    font-size: 1.4em;
    cursor: pointer;
    padding: 0.3em 0.55em;
    border-radius: 6px;
    line-height: 1;
    min-width: 44px;
    min-height: 44px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .refresh:hover, .new-thread:hover, .collapse:hover {
    background: var(--hover);
    color: var(--fg);
  }
  .collapse { font-size: 1.35em; }
  .new-thread {
    /* The + button is the primary action; tint it gold so it doesn't
       disappear next to the refresh control. */
    color: #d4af37;
    font-weight: 600;
    font-size: 1.55em;
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
    background: var(--hover);
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
  /* v0.4.82: .inbox-tab / .admin-tab styles removed — Inbox is now
     the pinned button above the section header (.inbox-pinned) and
     Admin is the 🔑 key icon in the sidebar header (.admin-icon). */
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
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }
  .version-sep { opacity: 0.5; }
  .check-updates {
    background: transparent;
    border: none;
    color: var(--muted);
    text-decoration: underline;
    text-decoration-style: dotted;
    text-underline-offset: 2px;
    font: inherit;
    padding: 0;
    cursor: pointer;
  }
  .check-updates:hover:not(:disabled) { color: var(--fg); }
  .check-updates:disabled { opacity: 0.6; cursor: default; }
  form button:disabled {
    background: var(--border);
    color: var(--muted);
    cursor: not-allowed;
  }
</style>
