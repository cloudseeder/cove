<!--
  A single VerifiedEntry rendered as a card with its Seal. The seal IS
  the card's identity — gold border on board-signed entries, neutral
  border on member posts, the broken-seal animation if the verification
  chain failed (which shouldn't happen here because the Client only
  produces VerifiedEntry on success — but we leave the state path open
  for forward-compat).
-->
<script lang="ts">
  import type { Client, VerifiedEntry } from './client';
  import { sigSummary } from './client';
  import Attachment from './Attachment.svelte';
  import Seal from './Seal.svelte';

  interface Props {
    ve: VerifiedEntry;
    /** Optional 'just arrived' flag — drives a brief shimmer on push. */
    isNew?: boolean;
    /** Required when the entry carries blobs — the Attachment component
     *  asks the Client to fetch the bytes with auth. Optional only because
     *  text-only entries don't need it. */
    client?: Client | null;
    /** v0.1.9: number of replies (entries whose parents include this id).
     *  Computed by ThreadView from app.entries; passed in rather than
     *  derived locally so the card stays cheap to render. */
    replyCount?: number;
    /** v0.1.9: fired when the user clicks 'Reply' — opens the reply
     *  panel pinned to this entry. */
    onReply?: () => void;
  }

  let { ve, isNew = false, client = null, replyCount = 0,
        onReply }: Props = $props();

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

  {#if ve.entry.body}
    <p class="body">{ve.entry.body}</p>
  {/if}

  {#if ve.entry.blobs.length > 0 && client}
    <div class="attachments">
      {#each ve.entry.blobs as blob (blob.hash)}
        <Attachment {client} {blob} />
      {/each}
    </div>
  {/if}

  {#if onReply}
    <footer>
      <button type="button" class="reply" onclick={onReply}>
        {#if replyCount === 0}
          Reply
        {:else if replyCount === 1}
          1 reply
        {:else}
          {replyCount} replies
        {/if}
      </button>
    </footer>
  {/if}

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

  .attachments {
    margin-top: 0.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  footer {
    margin-top: 0.7rem;
    display: flex;
    justify-content: flex-start;
  }
  .reply {
    background: transparent;
    border: 1px solid transparent;
    color: var(--muted);
    font: inherit;
    font-size: 0.82rem;
    padding: 0.25rem 0.7rem;
    border-radius: 999px;
    cursor: pointer;
    transition: border-color 120ms, color 120ms, background 120ms;
  }
  .reply:hover {
    color: #e8c96b;
    background: rgba(212, 175, 55, 0.06);
    border-color: rgba(212, 175, 55, 0.25);
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
