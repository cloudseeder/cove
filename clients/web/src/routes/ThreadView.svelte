<!--
  Thread view — the running feed. Each entry renders through EntryCard
  with its Seal already verified at appear time. Live updates via
  state.subscribe land at the bottom and use the 'fresh' CSS animation
  to draw the eye without interrupting the read.
-->
<script lang="ts">
  import EntryCard from '$lib/cove/EntryCard.svelte';
  import type { AppState } from '$lib/cove/state.svelte';
  import ComposeBox from './ComposeBox.svelte';

  interface Props {
    app: AppState;
  }
  let { app }: Props = $props();

  // Trail of fresh ids — anything that arrived in the last 1s gets the
  // 'fresh' CSS animation on EntryCard. Older entries render plain.
  let freshlyArrived: Set<string> = $state(new Set<string>());
  $effect(() => {
    const latest = app.entries.at(-1);
    if (!latest?.entry.id) return;
    const id = latest.entry.id;
    freshlyArrived = new Set(freshlyArrived).add(id);
    const timeout = setTimeout(() => {
      const next = new Set(freshlyArrived);
      next.delete(id);
      freshlyArrived = next;
    }, 1200);
    return () => clearTimeout(timeout);
  });

  let pubkey = $derived(
    app.authStatus.kind === 'authenticated' ? app.authStatus.pubkey : '',
  );
</script>

<section class="thread">
  <header>
    <div>
      <h1>{app.thread}</h1>
      <p class="muted">
        {app.entries.length} entr{app.entries.length === 1 ? 'y' : 'ies'}
        · you are <code>{pubkey.slice(0, 12)}…</code>
      </p>
    </div>
    {#if app.threadStatus.kind === 'syncing'}
      <span class="status">Syncing…</span>
    {:else if app.threadStatus.kind === 'error'}
      <span class="status error">⚠ {app.threadStatus.message}</span>
    {:else}
      <span class="status pulse" title="History intact ✓">✓ log intact</span>
    {/if}
  </header>

  <div class="feed">
    {#if app.entries.length === 0}
      <p class="empty">No entries yet. Be the first.</p>
    {:else}
      {#each app.entries as ve (ve.entry.id)}
        <EntryCard {ve} isNew={freshlyArrived.has(ve.entry.id!)} />
      {/each}
    {/if}
  </div>

  <ComposeBox {app} />
</section>

<style>
  .thread {
    max-width: 720px;
    margin: 0 auto;
    padding: 1.5rem 1.5rem 6rem;
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
  .empty {
    color: var(--muted);
    text-align: center;
    padding: 2rem;
  }
</style>
