<!--
  Inline preview of the latest reply to a parent entry. Clicking opens
  the reply panel (same effect as clicking the old "N replies" button).
  Used by both EntryCard (cards mode) and ChatMessage (chat mode) so
  the two thread-view render paths surface replies identically.

  v0.4.63: introduced. Replaces the tiny "1 reply" footer link with a
  visible chip that shows author + truncated body + total count, so a
  reply — usually the freshest thing in the conversation — isn't buried
  under a discovery click.
-->
<script lang="ts">
  import type { VerifiedEntry } from './client';

  interface Props {
    latestReply: VerifiedEntry;
    /** Total number of replies to the parent — includes latestReply. */
    totalReplyCount: number;
    onOpen: () => void;
    /** Compact typography for chat mode; default (false) is card mode. */
    dense?: boolean;
  }

  let { latestReply, totalReplyCount, onOpen, dense = false }: Props = $props();

  const preview = $derived(
    (() => {
      const body = latestReply.entry.body ?? '';
      if (body.length <= 100) return body;
      return body.slice(0, 100).trimEnd() + '…';
    })(),
  );

  const countLabel = $derived(
    totalReplyCount === 1 ? '1 reply' : `${totalReplyCount} replies`,
  );
</script>

<button type="button" class="reply-preview" class:dense onclick={onOpen}
  title="Open thread">
  <span class="arrow" aria-hidden="true">↳</span>
  <span class="content">
    <span class="author">{latestReply.attestation.display_name}</span>
    <span class="body">{preview || '(attachment)'}</span>
  </span>
  <span class="meta">{countLabel} →</span>
</button>

<style>
  .reply-preview {
    appearance: none;
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid var(--border);
    border-radius: 10px;
    color: inherit;
    font: inherit;
    padding: 0.55rem 0.7rem;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    width: 100%;
    text-align: left;
    cursor: pointer;
    margin-top: 0.55rem;
    transition: border-color 120ms ease, background 120ms ease;
  }
  .reply-preview:hover {
    border-color: rgba(212, 175, 55, 0.5);
    background: rgba(212, 175, 55, 0.05);
  }
  .arrow {
    color: rgba(212, 175, 55, 0.75);
    font-size: 1.05em;
    flex-shrink: 0;
  }
  .content {
    display: flex;
    align-items: baseline;
    gap: 0.45rem;
    min-width: 0;
    flex: 1;
  }
  .author {
    font-weight: 600;
    font-size: 0.86rem;
    flex-shrink: 0;
    max-width: 40%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .body {
    color: var(--muted);
    font-size: 0.86rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }
  .meta {
    color: var(--muted);
    font-size: 0.75rem;
    white-space: nowrap;
    flex-shrink: 0;
  }
  /* Chat-mode variant — tighter and lower contrast to match the
     denser message rhythm. */
  .reply-preview.dense {
    padding: 0.35rem 0.55rem;
    margin-top: 0.3rem;
    border-radius: 8px;
  }
  .reply-preview.dense .author,
  .reply-preview.dense .body {
    font-size: 0.8rem;
  }
  .reply-preview.dense .meta {
    font-size: 0.72rem;
  }
</style>
