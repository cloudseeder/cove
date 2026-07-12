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
  import type { Attestation } from '$lib/cove/types';
  import { authorColor, initials, smartTimestamp } from '$lib/cove/chat';
  import Attachment from '$lib/cove/Attachment.svelte';
  import DeliveryIndicator from '$lib/cove/DeliveryIndicator.svelte';
  import ExpandableBody from '$lib/cove/ExpandableBody.svelte';
  import ReplyPreview from '$lib/cove/ReplyPreview.svelte';
  import VerificationChain from '$lib/cove/VerificationChain.svelte';

  interface Props {
    ve: VerifiedEntry;
    showHeader: boolean;
    client?: Client | null;
    replyCount?: number;
    /** v0.4.63: freshest reply, surfaced inline as a preview chip. */
    latestReply?: VerifiedEntry | null;
    onReply?: () => void;
    onFollowBranch?: (subThread: string) => void;
    isNew?: boolean;
    /** v0.4.35: see EntryCard. */
    members?: Attestation[];
    /** v0.5.0: for kind='audience' entries — the (added, removed) diff
     *  computed by the parent walking the audience-entry stream in seq
     *  order. Null for non-audience kinds or when the parent has no
     *  diff computed yet. */
    audienceDiff?: { added: string[]; removed: string[] } | null;
  }

  let { ve, showHeader, client = null, replyCount = 0,
        latestReply = null, onReply, onFollowBranch,
        isNew = false, members = [], audienceDiff = null }: Props = $props();

  const isBranch = $derived(ve.entry.kind === 'branch' && !!ve.entry.branch_thread);
  const isBoard = $derived(ve.attestation.role === 'board');
  /** v0.4.21: notices are the headline entry kind — board broadcasts
   *  to all members. They get a gold-bordered standout treatment so
   *  they don't get lost in casual chat. Always show the header
   *  (enforced upstream via shouldGroupWithPrevious). */
  const isNotice = $derived(ve.entry.kind === 'notice');
  /** v0.5.0: audience-change entries render as governance chips
   *  (add/remove/left) instead of as chat. Not a headline event, so
   *  no gold treatment — just a compact inline notice. */
  const isAudienceChange = $derived(ve.entry.kind === 'audience');

  function nameForPk(pk: string): string {
    const m = members.find((x) => x.member_pubkey === pk);
    return m?.display_name ?? pk.slice(0, 8) + '…';
  }

  /** v0.5.0: build the human-readable audience-diff line. Self-leave
   *  gets a distinct phrasing ("left the thread") because the intent
   *  reads differently from a same-list "removed themselves". */
  const audienceDiffText = $derived.by(() => {
    if (!isAudienceChange || !audienceDiff) return '';
    const { added, removed } = audienceDiff;
    const isSelfLeave = removed.length === 1
      && removed[0] === ve.entry.author
      && added.length === 0;
    if (isSelfLeave) return 'left the thread';
    const parts: string[] = [];
    if (added.length > 0) {
      parts.push('added ' + added.map(nameForPk).join(', '));
    }
    if (removed.length > 0) {
      parts.push('removed ' + removed.map(nameForPk).join(', '));
    }
    if (parts.length === 0) return 'updated the audience';
    return parts.join(', ');
  });
  const color = $derived(authorColor(ve.entry.author));
  const inits = $derived(initials(ve.attestation.display_name));
  const time = $derived(smartTimestamp(ve.entry.created_at));

  /** v0.4.20: per-message reveal — clicking the ✓ badge opens the
   *  verification chain inline. State is per-component instance so
   *  every message toggles independently. */
  let revealed = $state(false);
</script>

<div class="row" class:fresh={isNew} class:grouped={!showHeader}
  class:revealed class:notice={isNotice}>
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
        {#if isNotice}
          <span class="notice-badge" aria-label="Board notice">◆ NOTICE</span>
        {/if}
        <span class="time">{time}</span>
      </div>
    {/if}

    {#if isAudienceChange}
      <span class="audience-change">
        <span aria-hidden="true">👥</span>
        <span>{audienceDiffText}</span>
      </span>
    {:else if isBranch && onFollowBranch}
      <button type="button" class="branch-link"
        onclick={() => onFollowBranch(ve.entry.branch_thread!)}>
        <span aria-hidden="true">🌿</span>
        <span>
          Branched off into <strong>{ve.entry.branch_thread}</strong>{#if ve.entry.body} — {ve.entry.body}{/if}
        </span>
      </button>
    {:else if ve.entry.body}
      <ExpandableBody body={ve.entry.body} dense />
    {/if}

    {#if ve.entry.blobs.length > 0 && client}
      <div class="attachments">
        {#each ve.entry.blobs as blob (blob.hash)}
          <Attachment {client} {blob} />
        {/each}
      </div>
    {/if}

    <!-- v0.4.63: latest-reply preview (chat variant is denser). -->
    {#if latestReply && onReply}
      <ReplyPreview {latestReply} totalReplyCount={replyCount} onOpen={onReply} dense />
    {/if}

    {#if !isAudienceChange && (onReply || (client && members.length > 0 && ve.entry.id))}
      <div class="footer-row">
        {#if onReply && !latestReply}
          <button type="button" class="reply-link" onclick={onReply}>Reply</button>
        {/if}
        {#if client && members.length > 0 && ve.entry.id}
          <DeliveryIndicator {client} entryId={ve.entry.id} {members} />
        {/if}
      </div>
    {/if}

    {#if revealed}
      <VerificationChain {ve} />
    {/if}
  </div>

  <button type="button" class="reveal"
    class:active={revealed}
    title={revealed ? 'Hide verification chain' : 'Show verification chain'}
    aria-expanded={revealed}
    aria-label="Verification"
    onclick={() => (revealed = !revealed)}>✓</button>
</div>

<style>
  .row {
    display: flex;
    gap: 0.7rem;
    padding: 0.32rem 0;
    align-items: flex-start;
  }
  .row.revealed {
    background: rgba(212, 175, 55, 0.04);
    border-radius: 8px;
    padding: 0.5rem 0.45rem;
  }
  /* v0.4.21: notice standout. Gold-bordered card-in-stream so a board
     broadcast never reads as just another chat message. The badge near
     the name + slightly more breathing room reinforce the "official"
     framing without breaking the chat density. */
  .row.notice {
    border: 1px solid rgba(212, 175, 55, 0.45);
    background: rgba(212, 175, 55, 0.05);
    border-radius: 10px;
    padding: 0.7rem 0.85rem;
    margin: 0.55rem 0;
    box-shadow: 0 0 0 1px rgba(212, 175, 55, 0.08) inset;
  }
  .row.notice.revealed {
    background: rgba(212, 175, 55, 0.08);
  }
  .notice-badge {
    font-size: 0.66rem;
    letter-spacing: 0.1em;
    color: rgb(212, 175, 55);
    background: rgba(212, 175, 55, 0.12);
    padding: 0.1rem 0.42rem;
    border-radius: 999px;
    font-weight: 600;
    border: 1px solid rgba(212, 175, 55, 0.3);
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
  /* v0.5.0: audience-change chip. Compact + muted so it doesn't
     compete with real posts, but still visible enough to answer
     "why did Bob's messages stop?". */
  .audience-change {
    display: inline-flex;
    gap: 0.4rem;
    align-items: baseline;
    font-size: 0.83rem;
    color: var(--muted, #888);
    font-style: italic;
    padding: 0.15rem 0;
  }
  .attachments {
    margin: 0.3rem 0;
  }
  .footer-row {
    display: flex;
    align-items: center;
    gap: 0.7rem;
    flex-wrap: wrap;
    margin-top: 0.1rem;
  }
  .reply-link {
    appearance: none;
    background: transparent;
    border: none;
    color: var(--muted);
    cursor: pointer;
    font-size: 0.78rem;
    padding: 0.15rem 0;
  }
  .reply-link:hover {
    text-decoration: underline;
    color: rgb(212, 175, 55);
  }
  /* v0.4.20: per-message verification reveal. Default to a low-opacity
     glyph at the message's right edge; hover the row to bring it up to
     full strength. The expanded chain renders inline below the body via
     <VerificationChain>. Always tappable for touch (no hover required). */
  .reveal {
    appearance: none;
    background: transparent;
    border: 1px solid transparent;
    color: var(--muted);
    cursor: pointer;
    font-size: 0.78rem;
    width: 1.55rem;
    height: 1.55rem;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    opacity: 0.25;
    margin-top: 0.2rem;
    transition: opacity 120ms ease, color 120ms ease, border-color 120ms ease;
  }
  .row:hover .reveal {
    opacity: 0.7;
  }
  .reveal:hover,
  .reveal.active {
    opacity: 1;
    color: rgb(212, 175, 55);
    border-color: rgba(212, 175, 55, 0.4);
  }
  @keyframes arrive {
    from { background: rgba(212, 175, 55, 0.10); }
    to { background: transparent; }
  }
</style>
