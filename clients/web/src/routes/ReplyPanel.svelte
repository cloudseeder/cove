<!--
  Slack-style reply panel — slides in from the right when a top-level
  entry's 'Reply' button is clicked. Shows the parent pinned at top,
  reply children below in seq order, and a ComposeBox configured to
  post a reply (parents = [parent.id]).

  Replies are stored as ordinary entries in the same thread; the only
  thing that makes them 'replies' is `parents` pointing at a sibling
  entry. They flow through /stream and verify the same way top-level
  entries do.

  Closing the panel: × button, backdrop click, Escape key, or switching
  threads (handled by AppState.switchThread).
-->
<script lang="ts">
  import EntryCard from '$lib/cove/EntryCard.svelte';
  import type { AppState } from '$lib/cove/state.svelte';
  import ComposeBox from './ComposeBox.svelte';

  let { app }: { app: AppState } = $props();

  const parent = $derived(app.replyOpen);

  /** Reply entries — those whose parents include the pinned entry's id.
   *  Sorted by seq (verify-time ordering, already monotonic). */
  const replies = $derived(
    parent === null
      ? []
      : app.entries
          .filter((ve) => ve.entry.parents.includes(parent.entry.id!))
          .sort((a, b) => a.seq - b.seq),
  );

  function onBackdrop() {
    app.closeReplyPanel();
  }

  function onKey(ev: KeyboardEvent) {
    if (ev.key === 'Escape') app.closeReplyPanel();
  }

  $effect(() => {
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });
</script>

{#if parent !== null}
  <div class="backdrop" onclick={onBackdrop} role="presentation"></div>
  <aside class="panel" role="dialog" aria-label="Thread replies">
    <header>
      <h2>Thread</h2>
      <button type="button" class="close" onclick={() => app.closeReplyPanel()}
        aria-label="Close thread">×</button>
    </header>

    <div class="scroll">
      <!-- Pinned parent — same EntryCard, no reply CTA inside the panel
           (you're already 'in' the reply context). -->
      <div class="parent">
        <EntryCard ve={parent} client={app.client} />
      </div>

      <div class="replies">
        {#if replies.length === 0}
          <p class="empty">No replies yet. Start the conversation.</p>
        {:else}
          {#each replies as ve (ve.entry.id)}
            <EntryCard {ve} client={app.client} />
          {/each}
        {/if}
      </div>
    </div>

    <div class="compose-wrap">
      <ComposeBox {app} replyTo={parent} />
    </div>
  </aside>
{/if}

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.35);
    z-index: 40;
    animation: fade 160ms ease;
  }
  .panel {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    width: min(440px, 100vw);
    background: var(--bg);
    border-left: 1px solid var(--border);
    box-shadow: -8px 0 32px rgba(0, 0, 0, 0.3);
    display: flex;
    flex-direction: column;
    z-index: 50;
    animation: slide 220ms cubic-bezier(0.2, 0.8, 0.2, 1);
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 1.25rem;
    border-bottom: 1px solid var(--border);
  }
  header h2 {
    margin: 0;
    font-size: 0.75rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    font-weight: 600;
  }
  .close {
    background: transparent;
    border: none;
    color: var(--muted);
    cursor: pointer;
    font-size: 1.4em;
    line-height: 1;
    padding: 0.1em 0.5em;
    border-radius: 6px;
  }
  .close:hover {
    background: rgba(255, 255, 255, 0.05);
    color: var(--fg);
  }
  .scroll {
    flex: 1;
    overflow-y: auto;
    padding: 1rem 1.25rem;
  }
  .parent {
    /* Visual cue that this is the pinned parent — subtle left rule */
    padding-left: 0.4rem;
    border-left: 2px solid rgba(212, 175, 55, 0.4);
  }
  .replies {
    margin-top: 1.5rem;
    padding-top: 1rem;
    border-top: 1px dashed var(--border);
  }
  .empty {
    color: var(--muted);
    font-size: 0.88rem;
    text-align: center;
    padding: 1rem 0;
  }
  .compose-wrap {
    padding: 0.6rem 1rem 1rem;
    border-top: 1px solid var(--border);
    background: var(--panel);
  }

  @keyframes fade {
    from { opacity: 0; }
    to   { opacity: 1; }
  }
  @keyframes slide {
    from { transform: translateX(100%); }
    to   { transform: translateX(0); }
  }
</style>
