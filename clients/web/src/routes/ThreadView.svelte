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
  import { shouldGroupWithPrevious } from '$lib/cove/chat';
  import type { AppState } from '$lib/cove/state.svelte';
  import ComposeBox from './ComposeBox.svelte';
  import FilesView from './FilesView.svelte';
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
  const topLevel = $derived(app.entries.filter((ve) => ve.entry.parents.length === 0));
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

  {#if app.view === 'admin'}
    <AdminPanel {app} />
  {:else if app.view === 'files'}
    <FilesView {app} />
  {:else}
    <section class="thread">
      <header>
        <div>
          <h1>{app.thread}</h1>
          <p class="muted">
            {app.entries.length} entr{app.entries.length === 1 ? 'y' : 'ies'}
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
        </div>
      </header>

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
            <ChatMessage
              {ve}
              showHeader={!shouldGroupWithPrevious(
                i > 0 ? topLevel[i - 1].entry : null,
                ve.entry,
              )}
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
