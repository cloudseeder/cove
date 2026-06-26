<!--
  ThreadList — left sidebar in the ThreadView layout. Renders the
  observed threads from the hub with a 'start a new thread' input at
  the bottom. Active thread is highlighted.

  Threads are open-namespace: typing a new name and submitting just
  switches the active thread to it. The thread materializes on the hub
  when the first entry is posted to it. No 'create' API to call.
-->
<script lang="ts">
  import type { AppState } from '$lib/cove/state.svelte';

  let { app }: { app: AppState } = $props();

  let newThreadName = $state('');

  /** v0.1.10: file count for the active thread, derived from the
   *  attachments across all loaded entries (top-level + replies).
   *  Shown next to the 'Files' sub-button so the user can see at a
   *  glance whether there's anything to browse. */
  const fileCount = $derived(
    app.entries.reduce((n, ve) => n + ve.entry.blobs.length, 0),
  );

  async function handleSwitch(name: string) {
    await app.switchThread(name);
  }

  async function handleNewThread(ev: SubmitEvent) {
    ev.preventDefault();
    const name = newThreadName.trim();
    if (!name) return;
    newThreadName = '';
    await app.switchThread(name);
  }
</script>

<aside class="thread-list" aria-label="Threads">
  <header>
    <h2>Threads</h2>
    <button
      type="button"
      class="refresh"
      title="Refresh thread list"
      onclick={() => app.loadThreads()}
    >
      ↻
    </button>
  </header>

  <ul>
    {#each app.threads as t (t.thread)}
      {@const isActive = t.thread === app.thread}
      <li class:active={isActive}>
        <button type="button" onclick={() => handleSwitch(t.thread)}>
          <span class="name">{t.thread}</span>
          <span class="count">{t.entry_count}</span>
        </button>
        {#if isActive}
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
        {/if}
      </li>
    {/each}
    {#if !app.threads.some((t) => t.thread === app.thread)}
      <!-- Current thread isn't in the hub list yet (empty / just-typed
           a fresh name). Show it as active anyway so the user can see
           where they are. -->
      <li class="active pending">
        <button type="button" disabled>
          <span class="name">{app.thread}</span>
          <span class="count">—</span>
        </button>
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
      </li>
    {/if}
  </ul>

  <form onsubmit={handleNewThread}>
    <input
      type="text"
      bind:value={newThreadName}
      placeholder="Start a new thread…"
      maxlength="64"
    />
    <button type="submit" disabled={!newThreadName.trim()}>+</button>
  </form>
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
  .refresh {
    background: transparent;
    border: none;
    color: var(--muted);
    font-size: 1.1em;
    cursor: pointer;
    padding: 0.1em 0.4em;
    border-radius: 4px;
  }
  .refresh:hover {
    background: rgba(255, 255, 255, 0.04);
    color: var(--fg);
  }
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
  form {
    display: flex;
    gap: 0.4rem;
    padding: 0.7rem 0.75rem 0.9rem;
    border-top: 1px solid var(--border);
  }
  input {
    flex: 1;
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
  form button:disabled {
    background: var(--border);
    color: var(--muted);
    cursor: not-allowed;
  }
</style>
