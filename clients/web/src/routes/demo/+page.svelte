<!--
  Offline ceremony demo against fixtures.json. Runs entirely client-side
  with no hub — useful for design iteration on the Seal in all three
  states without needing a running Python server.
-->
<script lang="ts">
  import Seal from '$lib/cove/Seal.svelte';
  import fixtures from '$lib/cove/fixtures.json';
  import { verifyEntry, verifyInclusion, verifySth, verifyDirectoryManifest } from '$lib/cove/verify';
  import type { Entry, InclusionProof, STH, DirectoryManifest } from '$lib/cove/types';

  const sth = fixtures.sth as STH;
  const manifest = fixtures.manifest as DirectoryManifest;
  const items = fixtures.entries as Array<{ entry: Entry; seq: number; proof: InclusionProof }>;

  function classify(entry: Entry, seq: number, proof: InclusionProof, tamper: 'none' | 'body' | 'proof' = 'none') {
    const ev = tamper === 'body' ? { ...entry, body: 'TAMPERED' } : entry;
    const pr = tamper === 'proof'
      ? { ...proof, audit_path: proof.audit_path.map(() => 'f'.repeat(64)) }
      : proof;
    if (!verifyEntry(ev)) return 'broken' as const;
    if (!verifySth(sth)) return 'broken' as const;
    if (!verifyInclusion(ev.id!, seq, pr, sth)) return 'broken' as const;
    return 'verified' as const;
  }

  const manifestOk = verifyDirectoryManifest(manifest);
  let revealed = $state<string | null>(null);
  function reveal(id: string) {
    revealed = revealed === id ? null : id;
  }
  const first = items[0];
</script>

<section class="hero">
  <p class="muted">offline ceremony demo · runs against fixtures.json</p>
  <h1>The seal</h1>
  <p class="lede">
    The seal is the message's identity, not a footer label. Three states
    by design — verified, pending, broken — each visually distinct enough
    to read at a glance.
  </p>
  <div class="meta">
    <span>Hub key: <code>{sth.hub_key.slice(0, 16)}…</code></span>
    <span>Log size: <code>{sth.tree_size}</code></span>
    <span>Manifest: <code>{manifestOk ? 'verified' : 'INVALID'}</code></span>
  </div>
</section>

<section class="row">
  <div class="seal-block">
    <Seal
      state={classify(first.entry, first.seq, first.proof, 'none')}
      title="Verified from Board"
      summary="board"
      onReveal={() => reveal('verified')}
    />
  </div>
  <div class="seal-block">
    <Seal state="pending" title="Verifying…" summary="sig + inclusion" />
  </div>
  <div class="seal-block">
    <Seal
      state={classify(first.entry, first.seq, first.proof, 'body')}
      title="VERIFICATION FAILED"
      summary="content does not match signature"
      onReveal={() => reveal('broken')}
    />
  </div>
</section>

{#if revealed === 'verified'}
  <div class="reveal">
    <h3>Verification chain</h3>
    <ol>
      <li>Author: <code>{first.entry.author.slice(0, 16)}…</code></li>
      <li>Content hash: <code>{first.entry.id}</code></li>
      <li>Attested by root: <code>{manifest.org.slice(0, 16)}…</code></li>
      <li>Position in log: <code>{first.proof.leaf_index}</code> of <code>{first.proof.tree_size}</code></li>
      <li>STH root: <code>{sth.root_hash.slice(0, 24)}…</code> (hub-signed)</li>
    </ol>
  </div>
{/if}
{#if revealed === 'broken'}
  <div class="reveal reveal-broken">
    <h3>Verification failed</h3>
    <p>Signature does not match the canonical content. Do not act on it.</p>
  </div>
{/if}

<p class="back"><a href="/">← Back to app</a></p>

<style>
  .hero {
    padding: 3rem 2rem 1.5rem;
    max-width: 720px;
    margin: 0 auto;
  }
  h1 {
    font-size: 2rem;
    margin: 0 0 0.5rem;
    font-weight: 600;
    letter-spacing: -0.02em;
  }
  .muted {
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-size: 0.75rem;
    margin: 0 0 0.6rem;
  }
  .lede {
    color: var(--muted);
    margin: 0 0 1.5rem;
  }
  .meta {
    display: flex;
    gap: 1.5rem;
    flex-wrap: wrap;
    color: var(--muted);
    font-size: 0.85rem;
  }
  .meta code {
    color: var(--fg);
  }
  .row {
    display: flex;
    gap: 1rem;
    flex-wrap: wrap;
    max-width: 720px;
    margin: 1.5rem auto;
    padding: 0 2rem;
  }
  .reveal {
    max-width: 720px;
    margin: 1.5rem auto;
    padding: 1.25rem 1.5rem;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--panel);
  }
  .reveal-broken {
    border-color: rgba(220, 38, 38, 0.45);
    background: rgba(220, 38, 38, 0.06);
  }
  .reveal h3 { margin: 0 0 0.6rem; font-size: 1rem; }
  .reveal-broken h3 { color: #dc2626; }
  .reveal ol { margin: 0; padding-left: 1.25rem; line-height: 1.7; font-size: 0.92rem; }
  .back {
    max-width: 720px;
    margin: 2rem auto;
    padding: 0 2rem;
    color: var(--muted);
  }
  .back a { color: var(--fg); }
</style>
