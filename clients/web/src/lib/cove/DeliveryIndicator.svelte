<!--
  Per-entry delivery panel — the accountability surface.

  Closed state: a small clickable pill. Open state: fetches
  GET /ledger?entry=… once, partitions the attested directory into
  "delivered ✓" and "not yet ⊘", and shows each member by display name.
  Refetches on every collapse → expand so the user can re-check after
  someone catches up.

  Hidden by design on entries the protocol doesn't ledger:
    - receipts (would be circular — they ARE the delivery signal)
    - membership / supersede / revoke / archive / reopen / audience
  ThreadView already filters those out for chronology; the parent here
  passes only renderable entries.
-->
<script lang="ts">
  import type { Client } from './client';
  import type { Attestation, LedgerStatus } from './types';

  interface Props {
    client: Client;
    entryId: string;
    /** Directory snapshot for pubkey → display-name resolution.
     *  Falls back to a truncated pubkey when a member isn't in the list
     *  (the hub may surface a pubkey that has since been revoked and
     *  the client manifest hasn't refreshed). */
    members: Attestation[];
  }

  let { client, entryId, members }: Props = $props();

  let expanded = $state(false);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let status = $state<LedgerStatus | null>(null);

  async function toggle() {
    if (expanded) {
      expanded = false;
      return;
    }
    // Refetch on every expand so the panel reflects the live ledger
    // rather than a stale snapshot from minutes ago.
    loading = true;
    error = null;
    status = null;
    try {
      status = await client.fetchLedger(entryId);
      expanded = true;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  function nameOf(pk: string): string {
    const m = members.find((m) => m.member_pubkey === pk);
    return m?.display_name ?? pk.slice(0, 8) + '…';
  }

  function titleOf(pk: string): string | undefined {
    return members.find((m) => m.member_pubkey === pk)?.title ?? undefined;
  }
</script>

<div class="delivery">
  <button
    type="button"
    class="toggle"
    class:open={expanded}
    onclick={toggle}
    disabled={loading}
    aria-expanded={expanded}
  >
    {#if loading}
      Checking delivery…
    {:else if status !== null}
      {@const total = status.acked.length + status.not_acked.length}
      {#if status.not_acked.length === 0}
        ✓ Delivered to all {total}
      {:else}
        {status.acked.length} of {total} delivered
      {/if}
    {:else}
      Show delivery
    {/if}
  </button>

  {#if error}
    <p class="error">Couldn't load delivery: {error}</p>
  {/if}

  {#if expanded && status !== null}
    <ul class="rows">
      {#each status.acked as pk (pk)}
        <li class="acked">
          <span class="mark" aria-hidden="true">✓</span>
          <span class="who">
            <span class="name">{nameOf(pk)}</span>
            {#if titleOf(pk)}<span class="title">{titleOf(pk)}</span>{/if}
          </span>
        </li>
      {/each}
      {#each status.not_acked as pk (pk)}
        <li class="pending">
          <span class="mark" aria-hidden="true">⊘</span>
          <span class="who">
            <span class="name">{nameOf(pk)}</span>
            {#if titleOf(pk)}<span class="title">{titleOf(pk)}</span>{/if}
          </span>
          <span class="label">not yet</span>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .delivery {
    display: flex;
    flex-direction: column;
    gap: 0.45rem;
    font-size: 0.85rem;
  }
  .toggle {
    align-self: flex-start;
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    cursor: pointer;
    padding: 0.2rem 0.7rem;
    font-size: 0.8rem;
    transition: border-color 120ms, color 120ms, background 120ms;
  }
  .toggle:hover:not(:disabled) {
    border-color: rgba(212, 175, 55, 0.5);
    color: var(--fg);
  }
  .toggle.open {
    background: rgba(212, 175, 55, 0.08);
    border-color: rgba(212, 175, 55, 0.45);
    color: var(--fg);
  }
  .toggle:disabled {
    cursor: progress;
    opacity: 0.7;
  }
  .error {
    color: #d97a7a;
    font-size: 0.8rem;
    margin: 0;
  }
  .rows {
    list-style: none;
    margin: 0;
    padding: 0.4rem 0.7rem;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid var(--border);
    border-radius: 8px;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .rows li {
    display: flex;
    align-items: baseline;
    gap: 0.55rem;
  }
  .mark {
    width: 1rem;
    text-align: center;
    font-weight: 600;
  }
  .acked .mark { color: #6aa86a; }
  .pending .mark { color: #c98a4a; }
  .who {
    display: flex;
    align-items: baseline;
    gap: 0.35rem;
    flex: 1;
    min-width: 0;
  }
  .name {
    color: var(--fg);
  }
  .title {
    color: var(--muted);
    font-size: 0.8rem;
  }
  .label {
    color: var(--muted);
    font-size: 0.75rem;
    font-style: italic;
  }
</style>
