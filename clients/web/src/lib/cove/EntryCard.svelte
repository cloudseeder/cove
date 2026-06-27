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
    /** v0.2: fired when the user clicks a branch-card body — switches to
     *  the sub-thread named in entry.branch_thread. */
    onFollowBranch?: (subThread: string) => void;
  }

  let { ve, isNew = false, client = null, replyCount = 0,
        onReply, onFollowBranch }: Props = $props();

  const isBranch = $derived(ve.entry.kind === 'branch' && !!ve.entry.branch_thread);

  let revealed = $state(false);

  const isBoard = $derived(ve.attestation.role === 'board');
  /** Title is the human-readable office; surfaces in the byline next to
   *  the name. Role is the trust tier and is conveyed ambiently by the
   *  gold seal styling on board-tier entries. */
  const personTitle = $derived(ve.attestation.title);
  const tooltipTitle = $derived(
    personTitle
      ? `Verified from ${ve.attestation.display_name}, ${personTitle}`
      : `Verified from ${ve.attestation.display_name}`,
  );
  const summary = $derived(sigSummary(ve));
  const created = $derived(ve.entry.created_at);
</script>

<article class="card" class:board={isBoard} class:fresh={isNew} class:branch={isBranch}>
  <header>
    <Seal
      state="verified"
      title={tooltipTitle}
      summary={ve.attestation.role}
      onReveal={() => (revealed = !revealed)}
    />
    <div class="byline">
      <span class="name">{ve.attestation.display_name}</span>
      {#if personTitle}
        <span class="title">{personTitle}</span>
      {/if}
    </div>
    <time>{created}</time>
  </header>

  {#if isBranch && onFollowBranch}
    <button type="button" class="branch-link"
      onclick={() => onFollowBranch(ve.entry.branch_thread!)}>
      <span class="branch-icon" aria-hidden="true">🌿</span>
      <span class="branch-meta">
        <span class="branch-label">Branched off into</span>
        <span class="branch-target">{ve.entry.branch_thread}</span>
      </span>
      {#if ve.entry.body}
        <span class="branch-why">{ve.entry.body}</span>
      {/if}
    </button>
  {:else if ve.entry.body}
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
        <dd>
          {ve.attestation.display_name}{#if personTitle}, {personTitle}{/if}
          {#if ve.attestation.affiliation}
            <span class="affiliation">· {ve.attestation.affiliation}</span>
          {/if}
        </dd>
        <dt>Pubkey</dt>
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
    gap: 1rem;
    margin-bottom: 0.6rem;
  }
  .byline {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    line-height: 1.2;
    min-width: 0;
  }
  .byline .name {
    font-weight: 600;
    font-size: 0.96rem;
  }
  .byline .title {
    color: var(--muted);
    font-size: 0.78rem;
  }
  time {
    color: var(--muted);
    font-size: 0.82rem;
    font-feature-settings: 'tnum';
    white-space: nowrap;
  }
  .affiliation {
    color: var(--muted);
    font-weight: normal;
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

  /* Branch card — distinct visual treatment so the eye sees 'this is
     a structural pointer, not a normal message.' */
  .card.branch {
    background: rgba(160, 200, 130, 0.04);
    border-color: rgba(160, 200, 130, 0.25);
  }
  .branch-link {
    display: flex;
    align-items: center;
    gap: 0.7rem;
    width: 100%;
    background: transparent;
    border: 1px dashed rgba(160, 200, 130, 0.35);
    border-radius: 10px;
    padding: 0.8rem 1rem;
    color: var(--fg);
    font: inherit;
    cursor: pointer;
    text-align: left;
  }
  .branch-link:hover {
    background: rgba(160, 200, 130, 0.08);
    border-style: solid;
  }
  .branch-icon { font-size: 1.4em; }
  .branch-meta {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
  }
  .branch-label {
    color: var(--muted);
    font-size: 0.78rem;
  }
  .branch-target {
    font-weight: 600;
    color: #d4eb9f;
  }
  .branch-why {
    margin-left: auto;
    color: var(--muted);
    font-size: 0.85rem;
    font-style: italic;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 16rem;
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
