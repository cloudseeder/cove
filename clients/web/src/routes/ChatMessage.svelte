<!--
  Chat-mode renderer for a single VerifiedEntry. Mirrors EntryCard's
  callbacks (onReply, onFollowBranch) so the surrounding flow doesn't
  care which mode is active — only the per-entry visual treatment
  differs.

  Verification status is NOT shown per-entry here. The Client only
  produces VerifiedEntry on success, and the thread header already
  shows the ambient "✓ log intact" indicator. Per-entry seals are
  the Cards-mode concern.

  `showHeader` is decided by ThreadView via shouldGroupWithPrevious():
  true on the first message of a group (avatar + name + timestamp),
  false on subsequent messages in the same group (tight stack).
-->
<script lang="ts">
  import type { Client, VerifiedEntry } from '$lib/cove/client';
  import { authorColor, initials, smartTimestamp } from '$lib/cove/chat';
  import Attachment from '$lib/cove/Attachment.svelte';

  interface Props {
    ve: VerifiedEntry;
    showHeader: boolean;
    client?: Client | null;
    replyCount?: number;
    onReply?: () => void;
    onFollowBranch?: (subThread: string) => void;
    isNew?: boolean;
  }

  let { ve, showHeader, client = null, replyCount = 0,
        onReply, onFollowBranch, isNew = false }: Props = $props();

  const isBranch = $derived(ve.entry.kind === 'branch' && !!ve.entry.branch_thread);
  const isBoard = $derived(ve.attestation.role === 'board');
  const color = $derived(authorColor(ve.entry.author));
  const inits = $derived(initials(ve.attestation.display_name));
  const time = $derived(smartTimestamp(ve.entry.created_at));
</script>

<div class="row" class:fresh={isNew} class:grouped={!showHeader}>
  {#if showHeader}
    <div class="avatar" style="background-color: {color};">{inits}</div>
  {:else}
    <div class="avatar-spacer"></div>
  {/if}

  <div class="content">
    {#if showHeader}
      <div class="head">
        <span class="name" class:board={isBoard}>{ve.attestation.display_name}</span>
        {#if ve.attestation.title}
          <span class="title">· {ve.attestation.title}</span>
        {/if}
        <span class="time">{time}</span>
      </div>
    {/if}

    {#if isBranch && onFollowBranch}
      <button type="button" class="branch-link"
        onclick={() => onFollowBranch(ve.entry.branch_thread!)}>
        <span aria-hidden="true">🌿</span>
        <span>
          Branched off into <strong>{ve.entry.branch_thread}</strong>{#if ve.entry.body} — {ve.entry.body}{/if}
        </span>
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
      <button type="button" class="reply-link" onclick={onReply}>
        {#if replyCount === 0}Reply{:else if replyCount === 1}1 reply{:else}{replyCount} replies{/if}
      </button>
    {/if}
  </div>
</div>

<style>
  .row {
    display: flex;
    gap: 0.7rem;
    padding: 0.32rem 0;
  }
  .row.grouped {
    padding-top: 0.08rem;
  }
  .row.fresh {
    animation: arrive 800ms ease;
  }
  .avatar, .avatar-spacer {
    width: 2.1rem;
    flex-shrink: 0;
  }
  .avatar {
    height: 2.1rem;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.78rem;
    font-weight: 600;
    color: rgba(0,0,0,0.85);
    user-select: none;
    margin-top: 0.15rem;
  }
  .content {
    flex: 1;
    min-width: 0;
  }
  .head {
    display: flex;
    align-items: baseline;
    gap: 0.35rem;
    margin-bottom: 0.12rem;
    font-size: 0.88rem;
  }
  .name {
    font-weight: 600;
  }
  .name.board {
    color: rgb(212, 175, 55);
  }
  .title {
    color: var(--muted);
    font-size: 0.78rem;
  }
  .time {
    color: var(--muted);
    font-size: 0.74rem;
    margin-left: auto;
  }
  .body {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    line-height: 1.45;
    font-size: 0.94rem;
  }
  .branch-link {
    appearance: none;
    background: transparent;
    border: 1px dashed var(--border);
    border-radius: 8px;
    color: inherit;
    padding: 0.45rem 0.65rem;
    cursor: pointer;
    text-align: left;
    font-size: 0.88rem;
    margin: 0.2rem 0;
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
  }
  .branch-link:hover {
    background: var(--panel);
  }
  .attachments {
    margin: 0.3rem 0;
  }
  .reply-link {
    appearance: none;
    background: transparent;
    border: none;
    color: var(--muted);
    cursor: pointer;
    font-size: 0.78rem;
    padding: 0.15rem 0;
    margin-top: 0.1rem;
  }
  .reply-link:hover {
    text-decoration: underline;
    color: rgb(212, 175, 55);
  }
  @keyframes arrive {
    from { background: rgba(212, 175, 55, 0.10); }
    to { background: transparent; }
  }
</style>
