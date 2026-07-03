<!--
  Sidebar hub switcher (v0.4.69 — federation UI, Phase 2).

  Renders between the sidebar header and the thread list. One row per
  joined hub. Clicking a row swaps the active hub — all delegating
  getters on AppState follow, so the thread list, inbox, and everything
  else in the pane flip to that hub's data.

  Placement: BETWEEN <header> and <ul> in ThreadList.svelte. Not inside
  the thread <ul> (which is a scroll region), so the hub list stays
  pinned above threads regardless of scroll state.

  Placeholder hubs (restored from localStorage but not yet authenticated
  this session) show a small "🔒" affordance — clicking the row still
  activates the hub; auth completes when the user unlocks (Tauri
  keychain or vault passphrase).
-->
<script lang="ts">
  import type { AppState } from '$lib/cove/state.svelte';
  import { hubLabel } from '$lib/cove/hubs';

  interface Props {
    app: AppState;
  }
  let { app }: Props = $props();

  const rows = $derived([...app.hubs.entries()]);
</script>

{#if rows.length > 0}
  <nav class="hubs" aria-label="Joined hubs">
    <div class="section-label">Hubs</div>
    {#each rows as [url, hub] (url)}
      <div class="hub-row-wrap" class:active={url === app.activeHubUrl}>
        <button type="button" class="hub-row"
          onclick={() => app.switchToHub(url)}
          title={url}>
          <span class="hub-label">{hubLabel(url)}</span>
          {#if hub.authStatus.kind === 'connecting'}
            <span class="hub-connecting" aria-label="Connecting…">…</span>
          {:else if hub.authStatus.kind === 'failed'}
            <span class="hub-locked" aria-label="Auth failed"
              title={hub.authStatus.reason}>⚠</span>
          {:else if hub.authStatus.kind !== 'authenticated'}
            <span class="hub-locked" aria-label="Not unlocked this session">🔒</span>
          {/if}
        </button>
        <!-- v0.4.70: per-row remove affordance so a failed / stale add
             can be cleaned up. Only shown on the non-active row so a
             one-hub session can't accidentally remove the hub it's on. -->
        {#if url !== app.activeHubUrl}
          <button type="button" class="hub-remove"
            onclick={() => app.removeHub(url)}
            title="Remove this hub from the switcher"
            aria-label="Remove {hubLabel(url)}">
            ×
          </button>
        {/if}
      </div>
    {/each}
    <button type="button" class="hub-row add-hub"
      onclick={() => (app.addHubOpen = true)}
      title="Join another Cove hub with the same keypair">
      + Add another hub
    </button>
  </nav>
{/if}

<style>
  .hubs {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    padding: 0.35rem 0.5rem 0.55rem;
    border-bottom: 1px solid var(--border);
  }
  .section-label {
    font-size: 0.7rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    font-weight: 600;
    padding: 0.3rem 0.7rem 0.15rem;
  }
  /* v0.4.70: wrapper hosts the row button + a remove × button so both
     can share the same active-row background and hover state. */
  .hub-row-wrap {
    display: flex;
    align-items: stretch;
    border-radius: 6px;
    transition: background 120ms ease;
  }
  .hub-row-wrap:hover {
    background: rgba(255, 255, 255, 0.04);
  }
  .hub-row-wrap.active {
    background: rgba(212, 175, 55, 0.12);
  }
  .hub-row-wrap.active .hub-row {
    color: #e8c96b;
    font-weight: 500;
  }
  .hub-row {
    flex: 1;
    display: flex;
    justify-content: space-between;
    align-items: center;
    min-width: 0;
    background: transparent;
    border: none;
    color: var(--fg);
    padding: 0.4rem 0.7rem;
    border-radius: 6px;
    cursor: pointer;
    font: inherit;
    font-size: 0.88rem;
    text-align: left;
    transition: color 120ms ease;
  }
  .hub-remove {
    background: transparent;
    border: none;
    color: var(--muted);
    padding: 0 0.55rem;
    font-size: 1.1rem;
    line-height: 1;
    cursor: pointer;
    border-radius: 6px;
    opacity: 0;
    transition: opacity 120ms ease, color 120ms ease, background 120ms ease;
  }
  .hub-row-wrap:hover .hub-remove {
    opacity: 0.6;
  }
  .hub-remove:hover {
    opacity: 1;
    color: #fca5a5;
    background: rgba(220, 38, 38, 0.08);
  }
  .hub-connecting {
    font-size: 0.85rem;
    color: var(--muted);
    opacity: 0.8;
    flex-shrink: 0;
  }
  .hub-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.82rem;
  }
  .hub-locked {
    font-size: 0.75rem;
    opacity: 0.7;
    flex-shrink: 0;
  }
  .hub-row.add-hub {
    color: #d4af37;
    font-weight: 500;
    font-size: 0.82rem;
    justify-content: flex-start;
    margin-top: 0.15rem;
  }
  .hub-row.add-hub:hover {
    background: rgba(212, 175, 55, 0.06);
    color: #e8c96b;
  }
</style>
