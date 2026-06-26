<!--
  FilesView — the per-thread Files face of the main pane. Surfaces every
  attachment posted in the active thread (top-level entries AND replies)
  as a sorted list, newest first.

  No new server endpoint: app.entries already holds every sync'd + pushed
  VerifiedEntry for the active thread, each carrying its blobs[]. We
  flatten and sort here.

  Each row is the existing Attachment.svelte component plus a metadata
  line ('from <author> · <date>') so the user can see who posted the
  file and when without scrolling back through the thread.
-->
<script lang="ts">
  import Attachment from '$lib/cove/Attachment.svelte';
  import type { AppState } from '$lib/cove/state.svelte';
  import type { VerifiedEntry } from '$lib/cove/client';
  import type { BlobRef } from '$lib/cove/types';

  let { app }: { app: AppState } = $props();

  type FileRow = { blob: BlobRef; entry: VerifiedEntry };

  const files = $derived<FileRow[]>(
    app.entries
      .flatMap((ve) =>
        ve.entry.blobs.map((blob) => ({ blob, entry: ve } as FileRow)),
      )
      // Newest first by per-thread seq. Same-seq impossible (acceptance
      // pipeline assigns monotonic seq per thread), so this is a total
      // order.
      .sort((a, b) => b.entry.seq - a.entry.seq),
  );

  function formatDate(iso: string): string {
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return iso;
    }
  }
</script>

<section class="files">
  <header>
    <h1>Files</h1>
    <p class="muted">
      {files.length} file{files.length === 1 ? '' : 's'} in <code>{app.thread}</code>
    </p>
  </header>

  {#if files.length === 0}
    <p class="empty">No files yet. Drop one into the compose box on
      Messages to share it.</p>
  {:else}
    <ul>
      {#each files as f (f.entry.entry.id + '|' + f.blob.hash)}
        <li>
          <div class="row">
            {#if app.client}
              <Attachment client={app.client} blob={f.blob} />
            {/if}
            <p class="meta">
              from <code>{f.entry.attestation.display_name}</code>
              · {formatDate(f.entry.entry.created_at)}
            </p>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .files {
    padding: 1.5rem;
    overflow-y: auto;
    height: 100%;
    box-sizing: border-box;
  }
  header {
    margin-bottom: 1.5rem;
  }
  header h1 {
    margin: 0 0 0.25rem;
    font-size: 1.4rem;
    font-weight: 600;
  }
  .muted {
    margin: 0;
    color: var(--muted);
    font-size: 0.88rem;
  }
  .empty {
    color: var(--muted);
    text-align: center;
    padding: 3rem 1rem;
    border: 1px dashed var(--border);
    border-radius: 12px;
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.85rem;
  }
  li {
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 0.85rem 1rem;
    background: var(--panel);
  }
  .row {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .meta {
    margin: 0;
    color: var(--muted);
    font-size: 0.82rem;
  }
</style>
