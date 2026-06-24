<!--
  Foundation demo. Shows the Seal ceremony in all three states against
  a fixture entry from fixtures.json — concrete proof the verification
  math is wired up. Not the production UI; the production thread view
  lands in slice 2.
-->
<script lang="ts">
  import Seal from '$lib/cove/Seal.svelte';
  import fixtures from '$lib/cove/fixtures.json';
  import { verifyEntry, verifyInclusion, verifySth, verifyDirectoryManifest } from '$lib/cove/verify';
  import type { Entry, InclusionProof, STH, DirectoryManifest } from '$lib/cove/types';

  const sth = fixtures.sth as STH;
  const manifest = fixtures.manifest as DirectoryManifest;
  const items = fixtures.entries as Array<{ entry: Entry; seq: number; proof: InclusionProof }>;

  // Run the full chain client-side, just like the production sync path will.
  function classify(entry: Entry, seq: number, proof: InclusionProof, tamper: 'none' | 'body' | 'proof' = 'none') {
    const ev = tamper === 'body' ? { ...entry, body: 'TAMPERED' } : entry;
    const pr = tamper === 'proof' ? { ...proof, audit_path: proof.audit_path.map(() => 'f'.repeat(64)) } : proof;
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
  <h1>Cove</h1>
  <p class="lede">
    Verifiable, accountable group messaging. Every entry signed; every notice provably delivered.
  </p>
  <div class="meta">
    <span>Hub key: <code>{sth.hub_key.slice(0, 16)}…</code></span>
    <span>Log size: <code>{sth.tree_size}</code></span>
    <span>Manifest: <code>{manifestOk ? 'verified' : 'INVALID'}</code></span>
  </div>
</section>

<section class="ceremony">
  <h2>The seal — three states</h2>
  <p class="muted">
    The seal IS the message's identity. Tap it to reveal the chain.
    Verification runs in this tab using <code>@noble/curves</code> +
    a tiny RFC&nbsp;8785 implementation, against fixtures captured from
    the Python reference. The math agrees byte-for-byte (21 vitest cases).
  </p>

  <div class="seal-row">
    <Seal
      state={classify(first.entry, first.seq, first.proof, 'none')}
      title="Verified from Board"
      summary="Signed by Board (board) — inclusion proof verified"
      onReveal={() => reveal('verified')}
    />
    <Seal
      state="pending"
      title="Verifying…"
      summary="Checking sig + inclusion proof"
    />
    <Seal
      state={classify(first.entry, first.seq, first.proof, 'body')}
      title="VERIFICATION FAILED"
      summary="Content does not match the signature"
      onReveal={() => reveal('broken')}
    />
  </div>

  {#if revealed === 'verified'}
    <div class="reveal">
      <h3>Verification chain</h3>
      <ol>
        <li>Author public key: <code>{first.entry.author.slice(0, 16)}…</code></li>
        <li>Signed content hash: <code>{first.entry.id}</code></li>
        <li>Author attested by root: <code>{manifest.org.slice(0, 16)}…</code></li>
        <li>Inclusion proof position <code>{first.proof.leaf_index}</code> of <code>{first.proof.tree_size}</code></li>
        <li>STH root: <code>{sth.root_hash.slice(0, 24)}…</code> (hub-signed)</li>
      </ol>
    </div>
  {/if}
  {#if revealed === 'broken'}
    <div class="reveal reveal-broken">
      <h3>Verification failed</h3>
      <p>
        Signature does not match the canonical content. Either the message
        was edited after signing, or it was forged. Do not act on it.
      </p>
    </div>
  {/if}
</section>

<style>
  .hero {
    padding: 4rem 2rem 2rem;
    max-width: 720px;
    margin: 0 auto;
  }
  h1 {
    font-size: 2.5rem;
    font-weight: 600;
    margin: 0 0 0.5rem;
    letter-spacing: -0.02em;
  }
  .lede {
    color: var(--muted);
    margin: 0 0 1.5rem;
    font-size: 1.05rem;
  }
  .meta {
    display: flex;
    gap: 1.5rem;
    flex-wrap: wrap;
    color: var(--muted);
    font-size: 0.875rem;
  }
  .meta code {
    color: var(--fg);
  }

  .ceremony {
    padding: 2rem;
    max-width: 720px;
    margin: 0 auto;
  }
  h2 {
    font-size: 1.25rem;
    font-weight: 600;
    margin: 0 0 0.75rem;
  }
  .muted {
    color: var(--muted);
    font-size: 0.95rem;
  }
  .seal-row {
    display: flex;
    flex-wrap: wrap;
    gap: 1rem;
    margin: 1.5rem 0;
    align-items: center;
  }

  .reveal {
    margin-top: 1.5rem;
    padding: 1.25rem 1.5rem;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--panel);
  }
  .reveal h3 {
    margin: 0 0 0.75rem;
    font-size: 1rem;
  }
  .reveal ol {
    margin: 0;
    padding-left: 1.25rem;
    line-height: 1.7;
    font-size: 0.92rem;
  }
  .reveal code {
    font-size: 0.88em;
    color: var(--muted);
  }
  .reveal-broken {
    border-color: rgba(220, 38, 38, 0.45);
    background: rgba(220, 38, 38, 0.06);
  }
  .reveal-broken h3 {
    color: #dc2626;
  }
</style>
