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
  import { sanitizeThreadName } from '$lib/cove/threadname';
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

  // v0.2: branch dialog
  let branchDialogOpen = $state(false);
  let branchName = $state('');
  let branching = $state(false);
  let branchError = $state<string | null>(null);

  // v0.6.0: ballot dialog
  let ballotDialogOpen = $state(false);
  let ballotQuestion = $state('');
  let ballotOptions = $state<string[]>(['', '']);
  let ballotClosesInHours = $state(24);
  let ballotting = $state(false);
  let ballotError = $state<string | null>(null);

  function openBallotDialog() {
    ballotError = null;
    ballotQuestion = '';
    ballotOptions = ['', ''];
    ballotClosesInHours = 24;
    ballotDialogOpen = true;
  }
  function closeBallotDialog() { ballotDialogOpen = false; }
  function addBallotOption() { ballotOptions = [...ballotOptions, '']; }
  function removeBallotOption(i: number) {
    ballotOptions = ballotOptions.filter((_, idx) => idx !== i);
  }
  async function submitBallot(ev: SubmitEvent) {
    ev.preventDefault();
    const question = ballotQuestion.trim();
    const options = ballotOptions.map((o) => o.trim()).filter(Boolean);
    if (!question || options.length < 2 || ballotting) return;
    if (new Set(options).size !== options.length) {
      ballotError = 'Options must be distinct.';
      return;
    }
    ballotting = true;
    ballotError = null;
    try {
      const closes = new Date(Date.now() + ballotClosesInHours * 3600 * 1000);
      await app.createBallot({
        question, options,
        closesAt: closes.toISOString(),
      });
      ballotDialogOpen = false;
    } catch (err) {
      ballotError = (err as Error).message;
    } finally {
      ballotting = false;
    }
  }

  function openBranchDialog() {
    branchError = null;
    branchName = '';
    branchDialogOpen = true;
  }

  function closeBranchDialog() {
    branchDialogOpen = false;
  }

  async function submitBranch(ev: SubmitEvent) {
    ev.preventDefault();
    const name = sanitizeThreadName(branchName);
    const body = draft.trim();
    if (!name || branching) return;
    branching = true;
    branchError = null;
    try {
      await app.branchOff(name, body || `Branched off into ${name}`);
      draft = '';
      pending = [];
      branchDialogOpen = false;
    } catch (err) {
      branchError = (err as Error).message;
    } finally {
      branching = false;
    }
  }

  /** v0.4.25: soft block when posting to an archived thread. The hub
   *  still accepts these entries — archive state is a visibility
   *  filter, not a write barrier (CLAUDE.md non-negotiable #5: no
   *  silent failures). The user has to explicitly confirm. */
  const archived = $derived(app.isThreadArchived(app.thread));
  let armedForArchived = $state(false);

  async function send() {
    const body = draft.trim();
    if ((!body && pending.length === 0) || sending) return;
    // v0.4.25: archived-thread confirmation gate. First click arms the
    // button ("Post anyway?"); second click posts. Cleared on any
    // unrelated state change (thread switch, draft change, etc).
    if (archived && !armedForArchived && !replyTo) {
      armedForArchived = true;
      return;
    }
    sending = true;
    error = null;
    try {
      await app.post(body, pending, replyTo);
      draft = '';
      pending = [];
      armedForArchived = false;
    } catch (err) {
      error = (err as Error).message;
    } finally {
      sending = false;
    }
  }
  // Reset the armed flag when the user navigates away or the thread
  // state changes underneath us (someone reopened the thread, say).
  $effect(() => {
    if (!archived) armedForArchived = false;
  });

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
    {#if !replyTo}
      <button type="button" class="attach branch-btn"
        onclick={openBranchDialog}
        disabled={sending}
        title="Branch off into a sub-thread"
        aria-label="Branch off">
        🌿
      </button>
      <button type="button" class="attach ballot-btn"
        onclick={openBallotDialog}
        disabled={sending}
        title="Start a vote"
        aria-label="Start a vote">
        🗳
      </button>
    {/if}
    <button type="submit"
      class:armed={archived && armedForArchived}
      disabled={sending || (draft.trim() === '' && pending.length === 0)}>
      {sending
        ? '…'
        : (archived && armedForArchived
          ? 'Post anyway'
          : 'Send')}
    </button>
  </div>

  {#if archived && !replyTo}
    <span class="archive-hint">
      📁 This thread is archived.
      {#if armedForArchived}
        Click <strong>Post anyway</strong> to confirm — the entry lands
        in the log but the thread stays out of the active Inbox.
      {:else}
        Posting still works, but a reader has to expand the archived
        section to see it.
      {/if}
    </span>
  {/if}

  {#if error}
    <span class="error">{error}</span>
  {/if}
</form>

{#if branchDialogOpen}
  <div class="modal-backdrop" onclick={closeBranchDialog} role="presentation"></div>
  <div class="modal" role="dialog" aria-label="Branch off into a sub-thread">
    <form onsubmit={submitBranch}>
      <h3>Branch off</h3>
      <p class="hint">
        Spawn a sub-thread linked from this one. The current draft
        becomes the rationale that shows up in the parent feed.
      </p>
      <label>
        <span>New thread name</span>
        <input type="text" bind:value={branchName}
          placeholder="e.g. budget-details"
          maxlength="64" autofocus
          autocapitalize="off" autocorrect="off" spellcheck="false" />
      </label>
      <div class="modal-actions">
        <button type="button" class="ghost" onclick={closeBranchDialog}
          disabled={branching}>Cancel</button>
        <button type="submit" disabled={branching || !branchName.trim()}>
          {branching ? '…' : 'Branch off'}
        </button>
      </div>
      {#if branchError}
        <p class="error">{branchError}</p>
      {/if}
    </form>
  </div>
{/if}

{#if ballotDialogOpen}
  <div class="modal-backdrop" onclick={closeBallotDialog} role="presentation"></div>
  <div class="modal" role="dialog" aria-label="Start a vote">
    <form onsubmit={submitBallot}>
      <h3>Start a vote</h3>
      <p class="hint">
        Signed votes visible to everyone in this thread; the tally
        updates live. Voters can change their mind until the deadline.
      </p>
      <label>
        <span>Question</span>
        <input type="text" bind:value={ballotQuestion}
          placeholder="e.g. Approve the 2026 landscaping RFP?"
          maxlength="200" required />
      </label>
      <fieldset class="ballot-options">
        <legend>Options</legend>
        {#each ballotOptions as _, i}
          <div class="ballot-opt-row">
            <input type="text" bind:value={ballotOptions[i]}
              placeholder="Option {i + 1}"
              maxlength="80" />
            {#if ballotOptions.length > 2}
              <button type="button" class="remove"
                onclick={() => removeBallotOption(i)}
                aria-label="Remove option {i + 1}">×</button>
            {/if}
          </div>
        {/each}
        {#if ballotOptions.length < 10}
          <button type="button" class="ghost add-option"
            onclick={addBallotOption}>+ Add option</button>
        {/if}
      </fieldset>
      <label>
        <span>Closes in</span>
        <select bind:value={ballotClosesInHours}>
          <option value={1}>1 hour</option>
          <option value={6}>6 hours</option>
          <option value={24}>24 hours</option>
          <option value={72}>3 days</option>
          <option value={168}>7 days</option>
          <option value={336}>14 days</option>
        </select>
      </label>
      <div class="modal-actions">
        <button type="button" class="ghost" onclick={closeBallotDialog}
          disabled={ballotting}>Cancel</button>
        <button type="submit"
          disabled={ballotting || !ballotQuestion.trim()
                    || ballotOptions.filter((o) => o.trim()).length < 2}>
          {ballotting ? '…' : 'Post ballot'}
        </button>
      </div>
      {#if ballotError}
        <p class="error">{ballotError}</p>
      {/if}
    </form>
  </div>
{/if}

<style>
  .compose {
    /* v0.4.58: no longer position: sticky. The parent .thread in
       ThreadView is now a flex column with .feed as the scrolling
       child, so .compose sits at the true bottom of the pane and the
       feed scrolls above it. The old sticky+bottom:1rem shape was
       floating the compose over the feed with a ~40px gap under it —
       messages ran behind the box instead of terminating at its top
       edge. */
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 0.6rem;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    backdrop-filter: blur(10px);
    transition: border-color 120ms ease, background 120ms ease;
    /* Don't grow to fill the parent flex column vertically — hug
       content and sit at the bottom. flex-shrink stays 1 so cross-axis
       margin/box calc behavior matches .feed's default. */
    flex: 0 1 auto;
    /* v0.4.59: match .feed's cross-axis width exactly. Without these,
       the auto margins from `.thread > *` (max-width:720px; margin:auto)
       interact with the compose's own padding/border under content-box
       sizing in a way that leaves the compose visually narrower than
       .feed on wide viewports. `box-sizing: border-box` makes max-width
       include padding+border so the OUTER box is 720px (matching .feed's
       720px content-box, which has no padding). `width: 100%` prevents
       the flex column parent from ever intrinsic-sizing the compose
       to its content-width — it always fills the cross-axis up to
       max-width. `align-self: stretch` is explicit for the same reason. */
    box-sizing: border-box;
    width: 100%;
    align-self: stretch;
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
    background: var(--hover);
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
  /* v0.4.25: archived-thread soft-block hint. Sits below the send row,
     same column. */
  .archive-hint {
    display: block;
    margin-top: 0.5rem;
    padding: 0.5rem 0.75rem;
    background: rgba(212, 175, 55, 0.08);
    border: 1px solid rgba(212, 175, 55, 0.3);
    border-radius: 6px;
    font-size: 0.83rem;
    color: var(--fg);
    line-height: 1.4;
  }
  button.armed {
    background: rgba(212, 175, 55, 0.85);
    color: #0a0a0a;
  }

  /* Branch button — distinct from the paperclip via a slightly warmer
     idle border. The 🌿 itself is enough hint when hovered. */
  .branch-btn:hover:not(:disabled) {
    border-color: rgba(160, 200, 130, 0.4);
  }

  /* Branch dialog */
  .modal-backdrop {
    position: fixed; inset: 0;
    background: rgba(0, 0, 0, 0.45);
    z-index: 60;
  }
  .modal {
    position: fixed;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    width: min(420px, 92vw);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 1.5rem;
    z-index: 70;
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.4);
  }
  .modal h3 {
    margin: 0 0 0.5rem;
    font-size: 1.1rem;
  }
  .modal .hint {
    color: var(--muted);
    font-size: 0.85rem;
    margin: 0 0 1.2rem;
  }
  .modal label {
    display: block;
    margin-bottom: 1rem;
  }
  .modal label span {
    display: block;
    font-size: 0.78rem;
    color: var(--muted);
    margin-bottom: 0.3rem;
  }
  .modal input[type='text'] {
    width: 100%;
    box-sizing: border-box;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.5rem 0.7rem;
    font: inherit;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .modal input[type='text']:focus {
    outline: none;
    border-color: rgba(160, 200, 130, 0.5);
  }
  .modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.6rem;
  }
  .modal-actions .ghost {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
  }
  .modal-actions .ghost:hover:not(:disabled) {
    color: var(--fg);
  }
  /* v0.6.0: ballot dialog. Compact options list with per-row remove +
     an add button; closes-in dropdown; distinct icon for the launcher. */
  .ballot-btn { border-color: rgba(212, 175, 55, 0.4); }
  .ballot-options {
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.55rem 0.7rem;
    margin: 0.4rem 0;
  }
  .ballot-options legend {
    font-size: 0.78rem;
    color: var(--muted);
    padding: 0 0.3rem;
  }
  .ballot-opt-row {
    display: flex;
    gap: 0.35rem;
    align-items: center;
    margin: 0.25rem 0;
  }
  .ballot-opt-row input {
    flex: 1;
    padding: 0.4rem 0.55rem;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg, inherit);
    color: inherit;
  }
  .ballot-opt-row .remove {
    padding: 0 0.55rem;
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--muted);
    cursor: pointer;
  }
  .ballot-opt-row .remove:hover { color: var(--danger, #c33); }
  .add-option {
    background: transparent;
    border: 1px dashed var(--border);
    color: var(--muted);
    padding: 0.35rem 0.6rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.82rem;
    margin-top: 0.35rem;
  }
  .add-option:hover { color: inherit; border-color: var(--accent, #d4af37); }
</style>
