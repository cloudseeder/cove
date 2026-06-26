<!--
  Compose box — sticky at the bottom of the thread. Ctrl/Cmd+Enter to
  send. No optimistic insert: the entry only appears in the feed when
  it comes back through the /stream subscription, verified, with proof.
  That's the ceremony — 'sent' isn't the same as 'verified-and-included'.

  v0.1.8: paperclip button + drag/drop attachments. Pending files
  render as chips between the textarea and the send button. On send:
  blobs are uploaded first (client-spec §3 strict order), then the
  entry posts referencing them by content-address. A failed upload
  aborts the post — we never ship an entry that points at missing
  bytes.
-->
<script lang="ts">
  import type { AppState } from '$lib/cove/state.svelte';
  import type { VerifiedEntry } from '$lib/cove/client';

  interface Props {
    app: AppState;
    /** v0.1.9: when set, this compose is for a reply — post() routes
     *  through with parents=[replyTo.entry.id]. The placeholder text
     *  also shifts. */
    replyTo?: VerifiedEntry | null;
  }
  let { app, replyTo = null }: Props = $props();

  let draft = $state('');
  let sending = $state(false);
  let error = $state<string | null>(null);
  /** Pending attachments — uploaded on send, not before. */
  let pending = $state<File[]>([]);
  let dragHover = $state(false);
  let fileInput: HTMLInputElement | undefined = $state();

  const placeholder = $derived(
    replyTo
      ? 'Reply… ⌘⏎ to send.'
      : 'Write something. ⌘⏎ to send. Drop files to attach.',
  );

  async function send() {
    const body = draft.trim();
    if ((!body && pending.length === 0) || sending) return;
    sending = true;
    error = null;
    try {
      await app.post(body, pending, replyTo);
      draft = '';
      pending = [];
    } catch (err) {
      error = (err as Error).message;
    } finally {
      sending = false;
    }
  }

  function onKey(ev: KeyboardEvent) {
    if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) {
      ev.preventDefault();
      void send();
    }
  }

  function addFiles(files: FileList | File[] | null | undefined) {
    if (!files) return;
    const arr = Array.from(files);
    // Drop empties (e.g. dragging a folder placeholder on some OSes).
    const real = arr.filter((f) => f.size > 0);
    pending = [...pending, ...real];
  }

  function removeAttachment(i: number) {
    pending = pending.filter((_, j) => j !== i);
  }

  function onFilePick(ev: Event) {
    const t = ev.target as HTMLInputElement;
    addFiles(t.files);
    t.value = '';   // allow re-picking the same file later
  }

  function onDrop(ev: DragEvent) {
    ev.preventDefault();
    dragHover = false;
    addFiles(ev.dataTransfer?.files);
  }

  function onDragOver(ev: DragEvent) {
    ev.preventDefault();
    dragHover = true;
  }

  function onDragLeave() {
    dragHover = false;
  }

  function formatBytes(n: number): string {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }
</script>

<form class="compose" class:drag={dragHover}
  ondrop={onDrop} ondragover={onDragOver} ondragleave={onDragLeave}
  onsubmit={(e) => { e.preventDefault(); void send(); }}>
  <textarea
    bind:value={draft}
    onkeydown={onKey}
    {placeholder}
    rows="2"
    disabled={sending}
  ></textarea>

  {#if pending.length > 0}
    <ul class="chips" aria-label="Pending attachments">
      {#each pending as f, i (i)}
        <li>
          <span class="name" title={f.name}>{f.name}</span>
          <span class="size">{formatBytes(f.size)}</span>
          <button type="button" class="remove"
            onclick={() => removeAttachment(i)}
            disabled={sending}
            aria-label="Remove {f.name}">×</button>
        </li>
      {/each}
    </ul>
  {/if}

  <div class="actions">
    <input bind:this={fileInput} type="file" multiple hidden
      onchange={onFilePick} />
    <button type="button" class="attach"
      onclick={() => fileInput?.click()}
      disabled={sending}
      aria-label="Attach files">
      📎
    </button>
    <button type="submit"
      disabled={sending || (draft.trim() === '' && pending.length === 0)}>
      {sending ? '…' : 'Send'}
    </button>
  </div>

  {#if error}
    <span class="error">{error}</span>
  {/if}
</form>

<style>
  .compose {
    position: sticky;
    bottom: 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 0.6rem;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    backdrop-filter: blur(10px);
    transition: border-color 120ms ease, background 120ms ease;
  }
  .compose.drag {
    border-color: rgba(212, 175, 55, 0.7);
    background: rgba(212, 175, 55, 0.06);
  }
  textarea {
    background: transparent;
    color: var(--fg);
    border: none;
    resize: none;
    font: inherit;
    padding: 0.45rem 0.5rem;
    min-height: 2.4rem;
    max-height: 12rem;
  }
  textarea:focus { outline: none; }

  .chips {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }
  .chips li {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.25rem 0.55rem;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid var(--border);
    border-radius: 999px;
    font-size: 0.82rem;
    max-width: 24rem;
  }
  .chips .name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .chips .size {
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    font-size: 0.75rem;
  }
  .chips .remove {
    background: transparent;
    border: none;
    color: var(--muted);
    cursor: pointer;
    font-size: 1em;
    line-height: 1;
    padding: 0 0.1em;
  }
  .chips .remove:hover:not(:disabled) { color: #fca5a5; }

  .actions {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    gap: 0.5rem;
  }
  .attach {
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 999px;
    width: 2.1rem; height: 2.1rem;
    padding: 0;
    font-size: 0.95em;
    cursor: pointer;
    color: var(--muted);
  }
  .attach:hover:not(:disabled) {
    color: var(--fg);
    border-color: rgba(212, 175, 55, 0.4);
  }
  .attach:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  button[type='submit'] {
    background: #d4af37;
    color: #0a0a0a;
    border: none;
    border-radius: 999px;
    padding: 0.45rem 1.3rem;
    font: inherit;
    font-weight: 600;
    cursor: pointer;
    transition: transform 120ms ease, background 200ms ease;
  }
  button[type='submit']:hover:not(:disabled) {
    transform: translateY(-1px);
  }
  button[type='submit']:disabled {
    background: var(--border);
    color: var(--muted);
    cursor: not-allowed;
  }
  .error {
    color: #fca5a5;
    font-size: 0.85rem;
  }
</style>
