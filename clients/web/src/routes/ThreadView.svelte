<!--
  Thread view — the running feed. Each entry renders through EntryCard
  with its Seal already verified at appear time. Live updates via
  state.subscribe land at the bottom and use the 'fresh' CSS animation
  to draw the eye without interrupting the read.
-->
<script lang="ts">
  import { untrack } from 'svelte';
  import AdminPanel from './AdminPanel.svelte';
  import ChatMessage from './ChatMessage.svelte';
  import EntryCard from '$lib/cove/EntryCard.svelte';
  import { dayLabel, shouldGroupWithPrevious, shouldShowDayDivider } from '$lib/cove/chat';
  import type { AppState } from '$lib/cove/state.svelte';
  import ComposeBox from './ComposeBox.svelte';
  import FilesView from './FilesView.svelte';
  import InboxPanel from './InboxPanel.svelte';
  import ReplyPanel from './ReplyPanel.svelte';
  import ThreadList from './ThreadList.svelte';

  interface Props {
    app: AppState;
  }
  let { app }: Props = $props();

  // Trail of fresh ids — anything that arrived in the last 1s gets the
  // 'fresh' CSS animation on EntryCard. Older entries render plain.
  //
  // CRITICAL: read freshlyArrived through untrack() inside this $effect.
  // Without it, the read becomes a dep of the effect, the synchronous
  // write below re-triggers the effect, and Svelte 5 reports
  // effect_update_depth_exceeded. The dependency this effect actually
  // wants is app.entries — that's the trigger we want.
  let freshlyArrived: Set<string> = $state(new Set<string>());
  $effect(() => {
    const latest = app.entries.at(-1);
    if (!latest?.entry.id) return;
    const id = latest.entry.id;
    freshlyArrived = untrack(() => new Set(freshlyArrived).add(id));
    const timeout = setTimeout(() => {
      freshlyArrived = untrack(() => {
        const next = new Set(freshlyArrived);
        next.delete(id);
        return next;
      });
    }, 1200);
    return () => clearTimeout(timeout);
  });

  let pubkey = $derived(
    app.authStatus.kind === 'authenticated' ? app.authStatus.pubkey : '',
  );

  // v0.1.9 — Slack-style sub-threads:
  //   - main feed shows ONLY top-level entries (parents.length === 0)
  //   - replies are pulled into the ReplyPanel keyed off entry.id
  //   - reply count per top-level entry is just a filter+count
  //
  // v0.4.19: also hide kind='receipt' entries from the chronological
  // feed. Receipts are noise to readers — they're auto-posted on view
  // for the audit trail. They stay in app.entries (state needs them for
  // the high-water computation in markThreadRead).
  //
  // v0.4.25: hide kind='archive' / 'reopen' too. They're governance
  // metadata, surfaced via the archive banner above the feed. Still in
  // app.entries + the log — the verification reveal exposes them.
  // v0.4.27: 'audience' joins the governance-metadata kinds hidden
  // from the chronological feed (they're surfaced via the audience
  // chip in the header instead).
  const _HIDDEN_KINDS = new Set(['receipt', 'archive', 'reopen', 'audience']);
  const topLevel = $derived(
    app.entries.filter((ve) =>
      ve.entry.parents.length === 0 && !_HIDDEN_KINDS.has(ve.entry.kind),
    ),
  );
  /** v0.4.19/0.4.25: feed count excludes the kinds hidden above
   *  (receipts + archive metadata). */
  const visibleEntryCount = $derived(
    app.entries.filter((ve) => !_HIDDEN_KINDS.has(ve.entry.kind)).length,
  );

  /** v0.4.25: archive state + the affordance to flip it. The banner
   *  shows whenever the current thread is archived; the action button
   *  shows only if the caller has the 'archive' capability. */
  const archived = $derived(app.isThreadArchived(app.thread));
  const canArchive = $derived(app.hasCapability('archive'));
  let archiveDialog = $state<{ kind: 'archive' | 'reopen' } | null>(null);
  let archiveRationale = $state('');

  function openArchiveDialog(kind: 'archive' | 'reopen') {
    archiveDialog = { kind };
    archiveRationale = '';
  }
  function closeArchiveDialog() {
    archiveDialog = null;
  }
  async function submitArchive() {
    if (!archiveDialog) return;
    await app.setThreadArchived(
      app.thread,
      archiveDialog.kind === 'archive',
      archiveRationale.trim(),
    );
    archiveDialog = null;
  }

  /** v0.4.27: audience chip + edit affordance. The chip shows when the
   *  current thread is audience-scoped (server only surfaces scoped
   *  threads to members, so "audience !== null" = "I'm in it"). The
   *  edit dialog lets any in-audience member rewrite the audience —
   *  same authority rule the hub enforces. */
  const audience = $derived(app.threadAudience(app.thread));
  const myPk = $derived(
    app.authStatus.kind === 'authenticated' ? app.authStatus.pubkey : '',
  );
  let audienceDialog = $state<{ selected: Set<string> } | null>(null);
  function openAudienceDialog() {
    audienceDialog = {
      selected: new Set(audience?.pubkeys ?? []),
    };
  }
  function closeAudienceDialog() {
    audienceDialog = null;
  }
  function toggleAudiencePubkey(pk: string) {
    if (!audienceDialog) return;
    const next = new Set(audienceDialog.selected);
    if (next.has(pk)) next.delete(pk);
    else next.add(pk);
    audienceDialog = { selected: next };
  }
  async function submitAudience() {
    if (!audienceDialog) return;
    // Force the caller in — UI doesn't let them remove themselves
    // unintentionally. (Slack lets you leave a private channel; we
    // can add an explicit "leave" later.)
    const pubkeys = new Set(audienceDialog.selected);
    pubkeys.add(myPk);
    await app.setThreadAudience(app.thread, Array.from(pubkeys));
    audienceDialog = null;
  }
  function nameForPubkey(pk: string): string {
    const att = app.members.find((m) => m.member_pubkey === pk);
    return att?.display_name ?? (pk.slice(0, 8) + '…');
  }
  function replyCountFor(parentId: string | null): number {
    if (!parentId) return 0;
    let n = 0;
    for (const ve of app.entries) {
      if (ve.entry.parents.includes(parentId)) n++;
    }
    return n;
  }
</script>

<div class="layout">
  <ThreadList {app} />

  {#if app.route === 'inbox'}
    <InboxPanel {app} />
  {:else if app.view === 'admin'}
    <AdminPanel {app} />
  {:else if app.view === 'files'}
    <FilesView {app} />
  {:else}
    <section class="thread">
      <header>
        <div>
          <h1>{app.thread}</h1>
          <p class="muted">
            {visibleEntryCount} entr{visibleEntryCount === 1 ? 'y' : 'ies'}
            · you are <code>{pubkey.slice(0, 12)}…</code>
          </p>
        </div>
        <div class="head-right">
          <div class="view-toggle" role="group" aria-label="View mode">
            <button type="button"
              class:active={app.viewMode === 'chat'}
              onclick={() => app.setViewMode('chat')}>Chat</button>
            <button type="button"
              class:active={app.viewMode === 'cards'}
              onclick={() => app.setViewMode('cards')}>Cards</button>
          </div>
          {#if app.threadStatus.kind === 'syncing'}
            <span class="status">Syncing…</span>
          {:else if app.threadStatus.kind === 'error'}
            <span class="status error">⚠ {app.threadStatus.message}</span>
          {:else}
            <span class="status pulse" title="History intact ✓">✓ log intact</span>
          {/if}
          {#if canArchive}
            {#if archived}
              <button type="button" class="ghost archive-btn"
                onclick={() => openArchiveDialog('reopen')}>Reopen</button>
            {:else}
              <button type="button" class="ghost archive-btn"
                onclick={() => openArchiveDialog('archive')}>Archive</button>
            {/if}
          {/if}
        </div>
      </header>

      {#if audience}
        <button type="button" class="audience-chip"
          title="Private thread — click to edit who can see it"
          onclick={openAudienceDialog}>
          <span aria-hidden="true">👥</span>
          <span class="names">
            {#each audience.pubkeys as pk, i (pk)}
              <span class="name">{nameForPubkey(pk)}</span>{#if i < audience.pubkeys.length - 1}<span class="comma">,</span> {/if}
            {/each}
          </span>
          <span class="edit-hint">edit</span>
        </button>
      {/if}

      {#if archived}
        <div class="archive-banner" role="status">
          <span aria-hidden="true">📁</span>
          <span>This thread is archived — read-only by convention.
            Posts still work; clients hide it from Inbox until reopened.</span>
        </div>
      {/if}

      {#if audienceDialog}
        <div class="audience-dialog">
          <h3>Edit audience for <code>{app.thread}</code></h3>
          <p class="muted">
            Anyone currently in the audience can change it. You'll
            stay in the audience automatically — to leave a thread,
            you'd ask another member to remove you.
          </p>
          <ul class="audience-edit-list">
            {#each app.members as m (m.member_pubkey)}
              {@const isSelf = m.member_pubkey === myPk}
              <li>
                <label>
                  <input type="checkbox"
                    checked={audienceDialog.selected.has(m.member_pubkey) || isSelf}
                    disabled={isSelf}
                    onchange={() => toggleAudiencePubkey(m.member_pubkey)} />
                  <span class="name">{m.display_name}</span>
                  {#if isSelf}<span class="role-tag">you</span>{/if}
                  {#if m.role !== 'member'}
                    <span class="role-tag">{m.role}</span>
                  {/if}
                </label>
              </li>
            {/each}
          </ul>
          <div class="archive-actions">
            <button type="button" class="ghost"
              onclick={closeAudienceDialog}>Cancel</button>
            <button type="button" onclick={submitAudience}>
              Save audience
            </button>
          </div>
        </div>
      {/if}

      {#if archiveDialog}
        <div class="archive-dialog">
          <h3>
            {archiveDialog.kind === 'archive' ? 'Archive' : 'Reopen'}
            <code>{app.thread}</code>?
          </h3>
          <p class="muted">
            {#if archiveDialog.kind === 'archive'}
              Hides the thread from Inbox + sidebar for everyone in the
              org. Reversible — anyone with the 'archive' capability can
              reopen it. A signed kind='archive' entry lands in the log.
            {:else}
              Returns the thread to the active Inbox. A signed kind='reopen'
              entry lands in the log so the action is auditable.
            {/if}
          </p>
          <label>
            <span>Rationale (becomes the entry's body)</span>
            <input type="text" bind:value={archiveRationale}
              placeholder={archiveDialog.kind === 'archive'
                ? 'Inactive since the annual meeting'
                : 'Topic resurfaced'} />
          </label>
          <div class="archive-actions">
            <button type="button" class="ghost"
              onclick={closeArchiveDialog}>Cancel</button>
            <button type="button" onclick={submitArchive}>
              {archiveDialog.kind === 'archive' ? 'Archive thread' : 'Reopen thread'}
            </button>
          </div>
        </div>
      {/if}

      <div class="feed" class:chat-mode={app.viewMode === 'chat'}>
        {#if topLevel.length === 0}
          <p class="empty">No entries yet. Be the first.</p>
        {:else if app.viewMode === 'cards'}
          {#each topLevel as ve (ve.entry.id)}
            <EntryCard
              {ve}
              isNew={freshlyArrived.has(ve.entry.id!)}
              client={app.client}
              replyCount={replyCountFor(ve.entry.id)}
              onReply={() => app.openReplyPanel(ve)}
              onFollowBranch={(sub) => app.switchThread(sub)}
            />
          {/each}
        {:else}
          {#each topLevel as ve, i (ve.entry.id)}
            {@const prev = i > 0 ? topLevel[i - 1].entry : null}
            {@const showDivider = shouldShowDayDivider(prev, ve.entry)}
            {#if showDivider}
              <div class="day-divider" role="separator">
                <span>{dayLabel(ve.entry.created_at)}</span>
              </div>
            {/if}
            <ChatMessage
              {ve}
              showHeader={showDivider || !shouldGroupWithPrevious(prev, ve.entry)}
              isNew={freshlyArrived.has(ve.entry.id!)}
              client={app.client}
              replyCount={replyCountFor(ve.entry.id)}
              onReply={() => app.openReplyPanel(ve)}
              onFollowBranch={(sub) => app.switchThread(sub)}
            />
          {/each}
        {/if}
      </div>

      <ComposeBox {app} />
    </section>
  {/if}
</div>

<ReplyPanel {app} />

<style>
  .layout {
    display: flex;
    height: 100vh;
    overflow: hidden;
  }
  .thread {
    flex: 1;
    overflow-y: auto;
    padding: 1.5rem;
    /* The .feed + compose box are the centered column; the
       scrollable region itself stretches to fill the pane. */
  }
  .thread > :global(*) {
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
  .muted {
    margin: 0;
    color: var(--muted);
    font-size: 0.88rem;
  }
  .status {
    color: var(--muted);
    font-size: 0.82rem;
    padding-top: 0.4rem;
  }
  .status.error {
    color: #fca5a5;
  }
  .status.pulse {
    color: rgba(212, 175, 55, 0.8);
    animation: pulse 3s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 0.7; }
    50%      { opacity: 1; }
  }
  .feed {
    margin-bottom: 1rem;
  }
  .feed.chat-mode {
    /* Tighter rhythm — chat mode prefers density over breathing room. */
    line-height: 1.4;
  }
  .day-divider {
    /* v0.4.20: centered pill between messages from different calendar
       days. Subtle so it doesn't compete with content, but visible
       enough that "what day is this?" is answered without scanning a
       full timestamp. */
    display: flex;
    align-items: center;
    justify-content: center;
    margin: 1rem 0 0.4rem;
    font-size: 0.74rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    position: relative;
  }
  .day-divider::before,
  .day-divider::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border);
    opacity: 0.55;
  }
  .day-divider span {
    padding: 0 0.7rem;
    background: var(--bg);
  }
  /* v0.4.25: archive affordances. The banner sits between the header
     and the feed so it's read before the first message. The dialog is
     inline (not modal) — it sits in the same column. */
  .archive-banner {
    display: flex; gap: 0.6rem; align-items: center;
    background: rgba(212, 175, 55, 0.08);
    border: 1px solid rgba(212, 175, 55, 0.3);
    border-radius: 8px;
    padding: 0.6rem 0.85rem;
    margin: 0 0 1rem;
    font-size: 0.88rem;
    color: var(--fg);
  }
  .archive-dialog {
    border: 1px dashed rgba(212, 175, 55, 0.4);
    border-radius: 10px;
    padding: 1rem 1.2rem;
    background: var(--panel);
    margin: 0 0 1rem;
  }
  .archive-dialog h3 { margin: 0 0 0.4rem; font-size: 1rem; }
  .archive-dialog .muted {
    color: var(--muted); margin: 0 0 0.7rem; font-size: 0.86rem;
  }
  .archive-dialog label { display: block; margin: 0.5rem 0; }
  .archive-dialog label span {
    display: block; font-size: 0.78rem; color: var(--muted);
    margin-bottom: 0.25rem;
  }
  .archive-dialog input {
    width: 100%; box-sizing: border-box;
    background: var(--bg); color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.45rem 0.65rem;
    font: inherit;
    font-size: 0.9rem;
  }
  .archive-actions {
    display: flex; justify-content: flex-end; gap: 0.5rem;
    margin-top: 0.7rem;
  }
  button.archive-btn { padding: 0.32rem 0.85rem; font-size: 0.85rem; }

  /* v0.4.27: audience chip + edit dialog. The chip sits above the
     feed (next to the archive banner slot) so the audience is the
     first thing read on opening a private thread. */
  .audience-chip {
    display: flex; align-items: center; gap: 0.5rem;
    background: rgba(120, 180, 255, 0.08);
    border: 1px solid rgba(120, 180, 255, 0.3);
    border-radius: 8px;
    padding: 0.5rem 0.85rem;
    margin: 0 0 1rem;
    cursor: pointer;
    color: var(--fg);
    text-align: left;
    font-size: 0.88rem;
    width: 100%;
  }
  .audience-chip:hover {
    background: rgba(120, 180, 255, 0.12);
  }
  .audience-chip .names { flex: 1; }
  .audience-chip .name { font-weight: 500; }
  .audience-chip .comma { color: var(--muted); }
  .audience-chip .edit-hint {
    color: var(--muted); font-size: 0.78rem;
    flex-shrink: 0;
  }
  .audience-dialog {
    border: 1px dashed rgba(120, 180, 255, 0.4);
    border-radius: 10px;
    padding: 1rem 1.2rem;
    background: var(--panel);
    margin: 0 0 1rem;
  }
  .audience-dialog h3 { margin: 0 0 0.4rem; font-size: 1rem; }
  .audience-dialog .muted {
    color: var(--muted); margin: 0 0 0.85rem; font-size: 0.86rem;
  }
  .audience-edit-list {
    list-style: none; margin: 0 0 0.5rem; padding: 0;
    max-height: 14rem; overflow-y: auto;
  }
  .audience-edit-list li {
    padding: 0.1rem 0;
  }
  .audience-edit-list label {
    display: flex; align-items: center; gap: 0.55rem;
    margin: 0; cursor: pointer;
    font-size: 0.9rem;
  }
  .audience-edit-list label .name { font-weight: 500; }
  .audience-edit-list label .role-tag {
    font-size: 0.7rem; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--muted);
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border);
    padding: 0.05rem 0.4rem; border-radius: 999px;
  }
  .empty {
    color: var(--muted);
    text-align: center;
    padding: 2rem;
  }
  .head-right {
    display: flex;
    align-items: center;
    gap: 0.85rem;
  }
  .view-toggle {
    display: inline-flex;
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    font-size: 0.78rem;
  }
  .view-toggle button {
    appearance: none;
    background: transparent;
    border: 0;
    padding: 0.32rem 0.65rem;
    color: var(--muted);
    cursor: pointer;
    font: inherit;
  }
  .view-toggle button.active {
    background: rgba(212, 175, 55, 0.18);
    color: rgb(212, 175, 55);
  }
  .view-toggle button:not(.active):hover {
    background: var(--panel);
  }
</style>
