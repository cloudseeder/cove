<!--
  A single BlobRef rendered as an attachment chip or inline image.

  Auth-bearing fetch: blobs are gated on session, so we can't just hand
  the URL to <img src=...>. Instead we authenticate, pull bytes, verify
  the content-address matches BlobRef.hash, then create an object URL
  for display. This also gives us tamper detection for free — Client
  .fetchBlobBytes raises VerificationError if the server returns bytes
  that don't hash to the claimed BlobRef.

  Object URLs are scoped to the lifetime of the component — onDestroy
  revokes them so we don't leak memory across thread switches.
-->
<script lang="ts">
  import { onDestroy } from 'svelte';
  import type { Client } from './client';
  import type { BlobRef } from './types';

  interface Props {
    client: Client;
    blob: BlobRef;
  }
  let { client, blob }: Props = $props();

  const isImage = $derived(blob.media_type.startsWith('image/'));

  let objectUrl = $state<string | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);

  async function loadInline() {
    if (objectUrl || loading) return;
    loading = true;
    try {
      const b = await client.fetchBlobBytes(blob);
      objectUrl = URL.createObjectURL(b);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  async function download() {
    try {
      const b = await client.fetchBlobBytes(blob);
      const url = URL.createObjectURL(b);
      const a = document.createElement('a');
      a.href = url;
      a.download = blob.name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // Small grace period so the browser actually starts the download
      // before we revoke; revoking too early aborts the click.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      error = (e as Error).message;
    }
  }

  // Inline images load on appear. Other types stay click-to-download.
  $effect(() => {
    if (isImage) void loadInline();
  });

  onDestroy(() => {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
  });

  function formatBytes(n: number): string {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }
</script>

{#if isImage}
  <figure class="image">
    {#if loading}
      <div class="placeholder">Verifying & decoding…</div>
    {:else if objectUrl}
      <img src={objectUrl} alt={blob.name} />
      <figcaption>{blob.name} · {formatBytes(blob.size)}</figcaption>
    {:else if error}
      <div class="broken" role="alert" title={error}>
        ⚠ image failed to load
      </div>
    {/if}
  </figure>
{:else}
  <button type="button" class="file" onclick={download} title="Download {blob.name}">
    <span class="icon" aria-hidden="true">📄</span>
    <span class="meta">
      <span class="name">{blob.name}</span>
      <span class="size">{formatBytes(blob.size)} · {blob.media_type}</span>
    </span>
    {#if error}
      <span class="warn" title={error}>⚠</span>
    {/if}
  </button>
{/if}

<style>
  .image {
    margin: 0.6rem 0;
    padding: 0;
  }
  .image img {
    display: block;
    max-width: 100%;
    max-height: 420px;
    border-radius: 8px;
    border: 1px solid var(--border);
  }
  .image figcaption {
    color: var(--muted);
    font-size: 0.78rem;
    margin-top: 0.35rem;
    font-variant-numeric: tabular-nums;
  }
  .placeholder, .broken {
    padding: 1rem;
    border: 1px dashed var(--border);
    border-radius: 8px;
    color: var(--muted);
    font-size: 0.85rem;
    text-align: center;
  }
  .broken {
    color: #fca5a5;
    border-color: rgba(220, 38, 38, 0.5);
  }

  .file {
    display: inline-flex;
    align-items: center;
    gap: 0.7rem;
    margin: 0.4rem 0;
    padding: 0.6rem 0.85rem;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid var(--border);
    border-radius: 10px;
    color: var(--fg);
    cursor: pointer;
    font: inherit;
    text-align: left;
    width: 100%;
    max-width: 100%;
    box-sizing: border-box;
  }
  .file:hover {
    background: rgba(212, 175, 55, 0.06);
    border-color: rgba(212, 175, 55, 0.3);
  }
  .icon { font-size: 1.4em; }
  .meta {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    min-width: 0;
    flex: 1;
  }
  .name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .size {
    color: var(--muted);
    font-size: 0.78rem;
  }
  .warn {
    color: #fca5a5;
    font-size: 1em;
  }
</style>
