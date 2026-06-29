<!--
  Verification chain reveal — extracted from EntryCard (v0.4.20) so the
  chat-mode message renderer can show the same chain on click without
  duplicating the layout. Pure presentation; the math already ran inside
  Client.verify() before the VerifiedEntry got here.
-->
<script lang="ts">
  import type { VerifiedEntry } from './client';
  import { sigSummary } from './client';

  let { ve }: { ve: VerifiedEntry } = $props();

  const personTitle = $derived(ve.attestation.title);
  const summary = $derived(sigSummary(ve));
</script>

<aside class="chain" aria-label="Verification chain">
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

<style>
  .chain {
    margin-top: 0.75rem;
    padding-top: 0.75rem;
    border-top: 1px solid var(--border);
  }
  .chain h4 {
    margin: 0 0 0.55rem;
    font-size: 0.78rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  dl {
    display: grid;
    grid-template-columns: max-content 1fr;
    column-gap: 1rem;
    row-gap: 0.32rem;
    margin: 0 0 0.55rem;
    font-size: 0.85rem;
  }
  dt {
    color: var(--muted);
  }
  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  code {
    font-size: 0.86em;
  }
  .affiliation {
    color: var(--muted);
    font-size: 0.85em;
  }
  .summary {
    margin: 0;
    color: var(--muted);
    font-size: 0.82rem;
    font-style: italic;
  }
</style>
