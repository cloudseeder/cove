<!--
  Message-body renderer with a per-instance "Show more / Show less"
  affordance. Used by both EntryCard (cards mode) and ChatMessage
  (chat mode) so the truncation rule + expand UI stay identical
  across the two thread-view render paths.

  v0.4.61: introduced. Messages > 100 chars render truncated + "…"
  with an inline toggle to reveal the full body. `dense` picks the
  compact typography used in chat mode; default is the cards-mode
  reading rhythm.
-->
<script lang="ts">
  interface Props {
    body: string;
    /** Character threshold before truncation kicks in. */
    limit?: number;
    /** Compact typography (chat mode). Defaults to false = cards mode. */
    dense?: boolean;
  }

  let { body, limit = 100, dense = false }: Props = $props();

  let expanded = $state(false);

  const shouldTruncate = $derived(body.length > limit);
  const display = $derived(
    !shouldTruncate || expanded
      ? body
      : body.slice(0, limit).trimEnd() + '…',
  );
</script>

<p class="body" class:dense>{display}</p>
{#if shouldTruncate}
  <button
    type="button"
    class="expander"
    aria-expanded={expanded}
    onclick={() => (expanded = !expanded)}
  >
    {expanded ? 'Show less' : 'Show more'}
  </button>
{/if}

<style>
  .body {
    margin: 0;
    line-height: 1.55;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .body.dense {
    line-height: 1.45;
    font-size: 0.94rem;
  }
  .expander {
    background: transparent;
    border: none;
    color: var(--muted);
    font: inherit;
    font-size: 0.78rem;
    cursor: pointer;
    padding: 0.15rem 0;
    margin-top: 0.15rem;
  }
  .expander:hover {
    color: rgb(212, 175, 55);
    text-decoration: underline;
  }
</style>
