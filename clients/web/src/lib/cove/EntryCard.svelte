<!--
  A single VerifiedEntry rendered as a card with its Seal. The seal IS
  the card's identity — gold border on board-signed entries, neutral
  border on member posts, the broken-seal animation if the verification
  chain failed (which shouldn't happen here because the Client only
  produces VerifiedEntry on success — but we leave the state path open
  for forward-compat).
-->
<script lang="ts">
  import type { VerifiedEntry } from './client';
  import { sigSummary } from './client';
  import Seal from './Seal.svelte';

  interface Props {
    ve: VerifiedEntry;
    /** Optional 'just arrived' flag — drives a brief shimmer on push. */
    isNew?: boolean;
  }

  let { ve, isNew = false }: Props = $props();

  let revealed = $state(false);

  const isBoard = $derived(ve.attestation.role === 'board');
  const title = $derived(
    `Verified from ${ve.attestation.display_name} (${ve.attestation.role})`,
  );
  const summary = $derived(sigSummary(ve));
  const created = $derived(ve.entry.created_at);
</script>

<article class="card" class:board={isBoard} class:fresh={isNew}>
  <header>
    <Seal
      state="verified"
      title={title}
      summary={ve.attestation.role}
      onReveal={() => (revealed = !revealed)}
    />
    <time>{created}</time>
  </header>

  <p class="body">{ve.entry.body}</p>

  {#if revealed}
    <aside class="chain">
      <h4>Verification chain</h4>
      <dl>
        <dt>Author</dt>
        <dd><code>{ve.entry.author.slice(0, 24)}…</code></dd>
        <dt>Content hash</dt>
        <dd><code>{ve.entry.id}</code></dd>
        <dt>Attested by root</dt>
        <dd><code>{ve.attestation.issuer.slice(0, 24)}…</code></dd>
        <dt>Position in log</dt>
        <dd>{ve.inclusionProof.leaf_index} of {ve.sth.tree_size}</dd>
        <dt>STH root</dt>
        <dd><code>{ve.sth.root_hash.slice(0, 24)}…</code></dd>
      </dl>
      <p class="summary">{summary}</p>
    </aside>
  {/if}
</article>

<style>
  .card {
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--panel);
    padding: 1.1rem 1.25rem;
    margin: 0.85rem 0;
  }
  .card.board {
    border-color: rgba(212, 175, 55, 0.35);
    box-shadow: 0 0 0 1px rgba(212, 175, 55, 0.08) inset;
  }
  .card.fresh {
    animation: arrive 800ms ease;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.6rem;
  }
  time {
    color: var(--muted);
    font-size: 0.82rem;
    font-feature-settings: 'tnum';
  }

  .body {
    margin: 0;
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .chain {
    margin-top: 1rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border);
  }
  .chain h4 {
    margin: 0 0 0.6rem;
    font-size: 0.85rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  dl {
    display: grid;
    grid-template-columns: max-content 1fr;
    column-gap: 1rem;
    row-gap: 0.35rem;
    margin: 0 0 0.6rem;
    font-size: 0.88rem;
  }
  dt {
    color: var(--muted);
  }
  dd {
    margin: 0;
  }
  code {
    font-size: 0.86em;
  }
  .summary {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
    font-style: italic;
  }

  @keyframes arrive {
    0% {
      background: rgba(212, 175, 55, 0.18);
      box-shadow: 0 0 24px rgba(212, 175, 55, 0.35);
    }
    100% {
      background: var(--panel);
      box-shadow: none;
    }
  }
</style>
