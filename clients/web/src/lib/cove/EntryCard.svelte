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
  import type { Attestation } from './types';
  import Attachment from './Attachment.svelte';
  import DeliveryIndicator from './DeliveryIndicator.svelte';
  import ExpandableBody from './ExpandableBody.svelte';
  import Seal from './Seal.svelte';
  import VerificationChain from './VerificationChain.svelte';
  import { smartTimestamp } from './chat';

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
    /** v0.4.35: attested directory for resolving pubkey → display name
     *  in the DeliveryIndicator. Omitted when the parent doesn't have
     *  the manifest yet — the indicator is suppressed in that case so
     *  it doesn't render half-resolved rows. */
    members?: Attestation[];
  }

  let { ve, isNew = false, client = null, replyCount = 0,
        onReply, onFollowBranch, members = [] }: Props = $props();

  const isBranch = $derived(ve.entry.kind === 'branch' && !!ve.entry.branch_thread);

  let revealed = $state(false);

  const isBoard = $derived(ve.attestation.role === 'board');
  /** Title is the human-readable office; surfaces in the byline next to
   *  the name. Role is the trust tier and is conveyed ambiently by the
   *  gold seal styling on board-tier entries. */
  const personTitle = $derived(ve.attestation.title);
  const tooltipTitle = $derived(
    personTitle
      ? `Verified from ${ve.attestation.display_name}, ${personTitle} (${ve.attestation.role})`
      : `Verified from ${ve.attestation.display_name} (${ve.attestation.role})`,
  );
  /** v0.4.57: raw ISO for the <time datetime> a11y attribute; the visible
   *  string is smartTimestamp() so the header doesn't waste screen space
   *  on a full ISO literal. */
  const createdIso = $derived(ve.entry.created_at);
  const createdShort = $derived(smartTimestamp(ve.entry.created_at));
</script>

<article class="card" class:board={isBoard} class:fresh={isNew} class:branch={isBranch}>
  <header>
    <Seal
      state="verified"
      tooltip={tooltipTitle}
      onReveal={() => (revealed = !revealed)}
    />
    <div class="byline">
      <span class="name">{ve.attestation.display_name}</span>
      {#if personTitle}
        <span class="title">{personTitle}</span>
      {/if}
    </div>
    <time datetime={createdIso} title={createdIso}>{createdShort}</time>
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
    <ExpandableBody body={ve.entry.body} />
  {/if}

  {#if ve.entry.blobs.length > 0 && client}
    <div class="attachments">
      {#each ve.entry.blobs as blob (blob.hash)}
        <Attachment {client} {blob} />
      {/each}
    </div>
  {/if}

  {#if onReply || (client && members.length > 0 && ve.entry.id)}
    <footer>
      {#if onReply}
        <button type="button" class="reply" onclick={onReply}>
          {#if replyCount === 0}
            Reply
          {:else if replyCount === 1}
            1 reply
          {:else}
            {replyCount} replies
          {/if}
        </button>
      {/if}
      {#if client && members.length > 0 && ve.entry.id}
        <DeliveryIndicator {client} entryId={ve.entry.id} {members} />
      {/if}
    </footer>
  {/if}

  {#if revealed}
    <VerificationChain {ve} />
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
    gap: 0.7rem;
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
    align-items: center;
    gap: 0.6rem;
    flex-wrap: wrap;
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
