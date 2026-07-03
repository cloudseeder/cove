<!--
  Thread view — the running feed. Each entry renders through EntryCard
  with its Seal already verified at appear time. Live updates via
  state.subscribe land at the bottom and use the 'fresh' CSS animation
  to draw the eye without interrupting the read.
-->
<script lang="ts">
  import { untrack } from 'svelte';
  import AdminPanel from './AdminPanel.svelte';
  import ChatMessage from './ChatMessage.svelte';
  import EntryCard from '$lib/cove/EntryCard.svelte';
  import { dayLabel, shouldGroupWithPrevious, shouldShowDayDivider } from '$lib/cove/chat';
  import type { AppState } from '$lib/cove/state.svelte';
  import ComposeBox from './ComposeBox.svelte';
  import FilesView from './FilesView.svelte';
  import InboxPanel from './InboxPanel.svelte';
  import ReplyPanel from './ReplyPanel.svelte';
  import ThreadList from './ThreadList.svelte';

  interface Props {
    app: AppState;
  }
  let { app }: Props = $props();

  // Trail of fresh ids — anything that arrived in the last 1s gets the
  // 'fresh' CSS animation on EntryCard. Older entries render plain.
  //
  // CRITICAL: read freshlyArrived through untrack() inside this $effect.
  // Without it, the read becomes a dep of the effect, the synchronous
  // write below re-triggers the effect, and Svelte 5 reports
  // effect_update_depth_exceeded. The dependency this effect actually
  // wants is app.entries — that's the trigger we want.
  let freshlyArrived: Set<string> = $state(new Set<string>());
  $effect(() => {
    const latest = app.entries.at(-1);
    if (!latest?.entry.id) return;
    const id = latest.entry.id;
    freshlyArrived = untrack(() => new Set(freshlyArrived).add(id));
    const timeout = setTimeout(() => {
      freshlyArrived = untrack(() => {
        const next = new Set(freshlyArrived);
        next.delete(id);
        return next;
      });
    }, 1200);
    return () => clearTimeout(timeout);
  });

  let pubkey = $derived(
    app.authStatus.kind === 'authenticated' ? app.authStatus.pubkey : '',
  );

  // v0.1.9 — Slack-style sub-threads:
  //   - main feed shows ONLY top-level entries (parents.length === 0)
  //   - replies are pulled into the ReplyPanel keyed off entry.id
  //   - reply count per top-level entry is just a filter+count
  //
  // v0.4.19: also hide kind='receipt' entries from the chronological
  // feed. Receipts are noise to readers — they're auto-posted on view
  // for the audit trail. They stay in app.entries (state needs them for
  // the high-water computation in markThreadRead).
  //
  // v0.4.25: hide kind='archive' / 'reopen' too. They're governance
  // metadata, surfaced via the archive banner above the feed. Still in
  // app.entries + the log — the verification reveal exposes them.
  // v0.4.27: 'audience' joins the governance-metadata kinds hidden
  // from the chronological feed (they're surfaced via the audience
  // chip in the header instead).
  const _HIDDEN_KINDS = new Set(['receipt', 'archive', 'reopen', 'audience']);
  const topLevel = $derived(
    app.entries.filter((ve) =>
      ve.entry.parents.length === 0 && !_HIDDEN_KINDS.has(ve.entry.kind),
    ),
  );
  /** v0.4.19/0.4.25: feed count excludes the kinds hidden above
   *  (receipts + archive metadata). */
  const visibleEntryCount = $derived(
    app.entries.filter((ve) => !_HIDDEN_KINDS.has(ve.entry.kind)).length,
  );

  /** v0.4.25: archive state + the affordance to flip it. The banner
   *  shows whenever the current thread is archived; the action button
   *  shows only if the caller has the 'archive' capability. */
  const archived = $derived(app.isThreadArchived(app.thread));
  const canArchive = $derived(app.hasCapability('archive'));
  let archiveDialog = $state<{ kind: 'archive' | 'reopen' } | null>(null);
  let archiveRationale = $state('');

  /** v0.4.38: lookup the /threads row for the current thread so we
   *  can render the ephemeral banner or the tombstone card. Null if
   *  the row hasn't arrived yet (freshly-typed name that the hub has
   *  never seen), in which case both branches are skipped. */
  const ephemeralRow = $derived(
    app.threads.find((t) => t.thread === app.thread) ?? null,
  );

  /* v0.4.43: ephemeral banner "…" menu + inline confirm.
   * Previously we used window.confirm(), which on Tauri's WKWebView
   * often returns false silently — the seal request never fired and
   * the button looked broken. Now: the menu opens an in-app confirm
   * card, no browser dialogs. */
  let ephMenuOpen = $state(false);
  let sealConfirm = $state<{ error: string | null; running: boolean } | null>(null);

  function openSealConfirm() {
    ephMenuOpen = false;
    sealConfirm = { error: null, running: false };
  }
  function closeSealConfirm() {
    sealConfirm = null;
  }
  async function confirmSealNow() {
    if (!app.client || !sealConfirm) return;
    sealConfirm = { ...sealConfirm, running: true, error: null };
    try {
      await app.client.tombstoneThread(app.thread);
      sealConfirm = null;
      // WS thread_tombstoned event drives the purge + badge flip.
    } catch (err) {
      sealConfirm = {
        error: err instanceof Error ? err.message : String(err),
        running: false,
      };
    }
  }

  function openArchiveDialog(kind: 'archive' | 'reopen') {
    archiveDialog = { kind };
    archiveRationale = '';
  }
  function closeArchiveDialog() {
    archiveDialog = null;
  }
  async function submitArchive() {
    if (!archiveDialog) return;
    await app.setThreadArchived(
      app.thread,
      archiveDialog.kind === 'archive',
      archiveRationale.trim(),
    );
    archiveDialog = null;
  }

  /** v0.4.27: audience chip + edit affordance. The chip shows when the
   *  current thread is audience-scoped (server only surfaces scoped
   *  threads to members, so "audience !== null" = "I'm in it"). The
   *  edit dialog lets any in-audience member rewrite the audience —
   *  same authority rule the hub enforces. */
  const audience = $derived(app.threadAudience(app.thread));
  const myPk = $derived(
    app.authStatus.kind === 'authenticated' ? app.authStatus.pubkey : '',
  );
  let audienceDialog = $state<{ selected: Set<string> } | null>(null);
  function openAudienceDialog() {
    audienceDialog = {
      selected: new Set(audience?.pubkeys ?? []),
    };
  }
  function closeAudienceDialog() {
    audienceDialog = null;
  }
  function toggleAudiencePubkey(pk: string) {
    if (!audienceDialog) return;
    const next = new Set(audienceDialog.selected);
    if (next.has(pk)) next.delete(pk);
    else next.add(pk);
    audienceDialog = { selected: next };
  }
  async function submitAudience() {
    if (!audienceDialog) return;
    // Force the caller in — UI doesn't let them remove themselves
    // unintentionally. (Slack lets you leave a private channel; we
    // can add an explicit "leave" later.)
    const pubkeys = new Set(audienceDialog.selected);
    pubkeys.add(myPk);
    await app.setThreadAudience(app.thread, Array.from(pubkeys));
    audienceDialog = null;
  }
  function nameForPubkey(pk: string): string {
    const att = app.members.find((m) => m.member_pubkey === pk);
    return att?.display_name ?? (pk.slice(0, 8) + '…');
  }
  function replyCountFor(parentId: string | null): number {
    if (!parentId) return 0;
    let n = 0;
    for (const ve of app.entries) {
      if (ve.entry.parents.includes(parentId)) n++;
    }
    return n;
  }
</script>

<div class="layout" class:sidebar-open={app.sidebarOpen} class:sidebar-closed={!app.sidebarOpen}>
  <!-- v0.4.45: sidebar toggle. Always visible; renders as a hamburger
       when the sidebar is closed and a chevron when it's open. On
       mobile it stays fixed at the top-left of the viewport so it's
       reachable from any panel; on desktop the sidebar is usually
       inline and the button collapses it back. -->
  <button type="button" class="sidebar-toggle"
    class:pushed={app.sidebarOpen}
    title={app.sidebarOpen ? 'Hide threads panel' : 'Show threads panel'}
    aria-label={app.sidebarOpen ? 'Hide threads panel' : 'Show threads panel'}
    aria-expanded={app.sidebarOpen}
    onclick={() => app.toggleSidebar()}>
    {#if app.sidebarOpen}‹{:else}☰{/if}
  </button>

  <!-- v0.4.45: mobile backdrop. Only visible when the sidebar is open
       on a narrow viewport; tapping it closes the sidebar. CSS gates
       the display via media query, so on desktop it stays hidden even
       when sidebarOpen === true (the sidebar is always inline there). -->
  <div class="sidebar-backdrop" role="presentation" aria-hidden="true"
    onclick={() => app.closeSidebar()}></div>

  <ThreadList {app} />

  {#if app.route === 'inbox'}
    <InboxPanel {app} />
  {:else if app.view === 'admin'}
    <AdminPanel {app} />
  {:else if app.view === 'files'}
    <FilesView {app} />
  {:else}
    <section class="thread">
      <header>
        <div>
          <h1>{app.thread}</h1>
          <p class="muted">
            {visibleEntryCount} entr{visibleEntryCount === 1 ? 'y' : 'ies'}
            · you are <code>{pubkey.slice(0, 12)}…</code>
          </p>
        </div>
        <div class="head-right">
          <div class="view-toggle" role="group" aria-label="View mode">
            <button type="button"
              class:active={app.viewMode === 'chat'}
              onclick={() => app.setViewMode('chat')}>Chat</button>
            <button type="button"
              class:active={app.viewMode === 'cards'}
              onclick={() => app.setViewMode('cards')}>Cards</button>
          </div>
          {#if app.threadStatus.kind === 'syncing'}
            <span class="status">Syncing…</span>
          {:else if app.threadStatus.kind === 'error'}
            <span class="status error">⚠ {app.threadStatus.message}</span>
          {:else}
            <span class="status pulse" title="History intact ✓">✓ log intact</span>
          {/if}
          {#if canArchive}
            {#if archived}
              <button type="button" class="ghost archive-btn"
                onclick={() => openArchiveDialog('reopen')}>Reopen</button>
            {:else}
              <button type="button" class="ghost archive-btn"
                onclick={() => openArchiveDialog('archive')}>Archive</button>
            {/if}
          {/if}
        </div>
      </header>

      {#if audience}
        <button type="button" class="audience-chip"
          title="Private thread — click to edit who can see it"
          onclick={openAudienceDialog}>
          <span aria-hidden="true">👥</span>
          <span class="names">
            {#each audience.pubkeys as pk, i (pk)}
              <span class="name">{nameForPubkey(pk)}</span>{#if i < audience.pubkeys.length - 1}<span class="comma">,</span> {/if}
            {/each}
          </span>
          <span class="edit-hint">edit</span>
        </button>
      {/if}

      {#if archived}
        <div class="archive-banner" role="status">
          <span aria-hidden="true">📁</span>
          <span>This thread is archived — read-only by convention.
            Posts still work; clients hide it from Inbox until reopened.</span>
        </div>
      {/if}

      {#if ephemeralRow && ephemeralRow.type === 'ephemeral'}
        <div class="ephemeral-banner" role="status">
          <span aria-hidden="true">⏳</span>
          <span>Ephemeral thread — deletes on
            <strong>{ephemeralRow.expires_at}</strong>. Save anything
            you want to keep.</span>
          {#if app.authStatus.kind === 'authenticated' && ephemeralRow.creator_pubkey === app.authStatus.pubkey}
            <div class="eph-menu-wrap">
              <button type="button" class="eph-menu-btn"
                title="Thread actions"
                aria-haspopup="menu"
                aria-expanded={ephMenuOpen}
                onclick={() => (ephMenuOpen = !ephMenuOpen)}>⋯</button>
              {#if ephMenuOpen}
                <div class="eph-menu" role="menu">
                  <button type="button" role="menuitem"
                    class="eph-menu-item danger"
                    onclick={openSealConfirm}>
                    Delete this thread now
                  </button>
                </div>
              {/if}
            </div>
          {/if}
        </div>
        {#if sealConfirm}
          <div class="seal-confirm" role="alertdialog"
            aria-label="Confirm delete this thread">
            <p>
              <strong>Delete <code>{app.thread}</code> now?</strong>
              Its entries will be removed from the hub immediately. A
              signed tombstone stays in the main log forever; the
              sealed final STH is preserved so members who kept a
              local copy can still prove what was there.
            </p>
            {#if sealConfirm.error}
              <p class="failure" role="alert">
                Seal failed: {sealConfirm.error}
              </p>
            {/if}
            <div class="seal-confirm-actions">
              <button type="button" class="ghost"
                onclick={closeSealConfirm}
                disabled={sealConfirm.running}>Cancel</button>
              <button type="button" class="danger"
                onclick={confirmSealNow}
                disabled={sealConfirm.running}>
                {sealConfirm.running ? 'Deleting…' : 'Delete now'}
              </button>
            </div>
          </div>
        {/if}
      {:else if ephemeralRow && ephemeralRow.type === 'tombstoned'}
        <div class="tombstone-card" role="status">
          <span aria-hidden="true">⚰</span>
          <div>
            <p><strong>This thread was sealed on {ephemeralRow.tombstoned_at}.</strong></p>
            {#if ephemeralRow.final_sth}
              <p class="muted small">
                Final tree_size: <code>{ephemeralRow.final_sth.tree_size}</code>,
                root_hash: <code>{ephemeralRow.final_sth.root_hash.slice(0, 16)}…</code>
              </p>
            {/if}
            <p class="muted small">
              The entries are gone from the hub. Anyone who kept a
              local copy can prove it existed against the final STH.
            </p>
          </div>
        </div>
      {/if}

      {#if audienceDialog}
        <div class="audience-dialog">
          <h3>Edit audience for <code>{app.thread}</code></h3>
          <p class="muted">
            Anyone currently in the audience can change it. You'll
            stay in the audience automatically — to leave a thread,
            you'd ask another member to remove you.
          </p>
          <ul class="audience-edit-list">
            {#each app.members as m (m.member_pubkey)}
              {@const isSelf = m.member_pubkey === myPk}
              <li>
                <label>
                  <input type="checkbox"
                    checked={audienceDialog.selected.has(m.member_pubkey) || isSelf}
                    disabled={isSelf}
                    onchange={() => toggleAudiencePubkey(m.member_pubkey)} />
                  <span class="name">{m.display_name}</span>
                  {#if isSelf}<span class="role-tag">you</span>{/if}
                  {#if m.role !== 'member'}
                    <span class="role-tag">{m.role}</span>
                  {/if}
                </label>
              </li>
            {/each}
          </ul>
          <div class="archive-actions">
            <button type="button" class="ghost"
              onclick={closeAudienceDialog}>Cancel</button>
            <button type="button" onclick={submitAudience}>
              Save audience
            </button>
          </div>
        </div>
      {/if}

      {#if archiveDialog}
        <div class="archive-dialog">
          <h3>
            {archiveDialog.kind === 'archive' ? 'Archive' : 'Reopen'}
            <code>{app.thread}</code>?
          </h3>
          <p class="muted">
            {#if archiveDialog.kind === 'archive'}
              Hides the thread from Inbox + sidebar for everyone in the
              org. Reversible — anyone with the 'archive' capability can
              reopen it. A signed kind='archive' entry lands in the log.
            {:else}
              Returns the thread to the active Inbox. A signed kind='reopen'
              entry lands in the log so the action is auditable.
            {/if}
          </p>
          <label>
            <span>Rationale (becomes the entry's body)</span>
            <input type="text" bind:value={archiveRationale}
              placeholder={archiveDialog.kind === 'archive'
                ? 'Inactive since the annual meeting'
                : 'Topic resurfaced'} />
          </label>
          <div class="archive-actions">
            <button type="button" class="ghost"
              onclick={closeArchiveDialog}>Cancel</button>
            <button type="button" onclick={submitArchive}>
              {archiveDialog.kind === 'archive' ? 'Archive thread' : 'Reopen thread'}
            </button>
          </div>
        </div>
      {/if}

      <div class="feed" class:chat-mode={app.viewMode === 'chat'}>
        {#if topLevel.length === 0}
          <p class="empty">No entries yet. Be the first.</p>
        {:else if app.viewMode === 'cards'}
          {#each topLevel as ve (ve.entry.id)}
            <EntryCard
              {ve}
              isNew={freshlyArrived.has(ve.entry.id!)}
              client={app.client}
              replyCount={replyCountFor(ve.entry.id)}
              onReply={() => app.openReplyPanel(ve)}
              onFollowBranch={(sub) => app.switchThread(sub)}
              members={app.members}
            />
          {/each}
        {:else}
          {#each topLevel as ve, i (ve.entry.id)}
            {@const prev = i > 0 ? topLevel[i - 1].entry : null}
            {@const showDivider = shouldShowDayDivider(prev, ve.entry)}
            {#if showDivider}
              <div class="day-divider" role="separator">
                <span>{dayLabel(ve.entry.created_at)}</span>
              </div>
            {/if}
            <ChatMessage
              {ve}
              showHeader={showDivider || !shouldGroupWithPrevious(prev, ve.entry)}
              isNew={freshlyArrived.has(ve.entry.id!)}
              client={app.client}
              replyCount={replyCountFor(ve.entry.id)}
              onReply={() => app.openReplyPanel(ve)}
              onFollowBranch={(sub) => app.switchThread(sub)}
              members={app.members}
            />
          {/each}
        {/if}
      </div>

      <!-- v0.4.49: hide compose in a tombstoned thread — the hub
           refuses writes to it, so surfacing the input would just
           produce a bewildering error card on submit. -->
      {#if !ephemeralRow || ephemeralRow.type !== 'tombstoned'}
        <ComposeBox {app} />
      {/if}
    </section>
  {/if}
</div>

<ReplyPanel {app} />

<!--
  v0.4.30: + New thread dialog. State lives on AppState (newThreadDialog)
  so any UI surface — InboxPanel header, ThreadList sidebar button,
  future deep links — can open it via app.openNewThreadDialog(). The
  dialog renders at this layout level rather than inside InboxPanel
  so it's reachable from inside a thread too.
-->
{#if app.newThreadDialog}
  {@const d = app.newThreadDialog}
  {@const myPk = app.authStatus.kind === 'authenticated' ? app.authStatus.pubkey : ''}
  {@const otherMembers = app.members.filter((m) => m.member_pubkey !== myPk)}
  <div class="modal-backdrop" onclick={() => app.closeNewThreadDialog()} role="presentation"></div>
  <div class="modal" role="dialog" aria-label="Start a new thread">
    <h3>New thread</h3>

    <label>
      <span>Thread name</span>
      <input type="text" bind:value={d.name}
        placeholder="e.g. board-private-2026-q3"
        maxlength="64" autocapitalize="off"
        autocorrect="off" spellcheck="false" />
    </label>

    <fieldset class="scope">
      <legend>Audience</legend>
      <label class="radio">
        <input type="radio" bind:group={d.scope} value="public" />
        <span>Everyone in the org</span>
      </label>
      <label class="radio">
        <input type="radio" bind:group={d.scope} value="private" />
        <span>Just these people</span>
      </label>
    </fieldset>

    {#if d.scope === 'private'}
      <div class="audience-list">
        <p class="self-line">
          ✓ <strong>You</strong> (auto-included as creator)
        </p>
        <ul>
          {#each otherMembers as m (m.member_pubkey)}
            <li>
              <label>
                <input type="checkbox"
                  checked={d.selected.has(m.member_pubkey)}
                  onchange={() => app.toggleNewThreadMember(m.member_pubkey)} />
                <span class="name">{m.display_name}</span>
                {#if m.role !== 'member'}
                  <span class="role-tag">{m.role}</span>
                {/if}
              </label>
            </li>
          {/each}
        </ul>
        {#if otherMembers.length === 0}
          <p class="muted small">
            No other attested members on this hub yet. A private thread
            still works — you'll be the only audience member.
          </p>
        {/if}
      </div>
    {/if}

    <fieldset class="scope">
      <legend>Retention</legend>
      <label class="radio">
        <input type="radio" name="retention"
          checked={!d.ephemeral}
          onchange={() => { d.ephemeral = false; }} />
        <span>Permanent — governance-grade record</span>
      </label>
      <label class="radio">
        <input type="radio" name="retention"
          checked={d.ephemeral}
          onchange={() => { d.ephemeral = true; }} />
        <span>Ephemeral — deletes after a TTL</span>
      </label>
      {#if d.ephemeral}
        <div class="ttl-row">
          <span>Delete after</span>
          <button type="button" class="ttl-preset" class:selected={d.ttlDays === 7}
            onclick={() => (d.ttlDays = 7)}>7d</button>
          <button type="button" class="ttl-preset" class:selected={d.ttlDays === 30}
            onclick={() => (d.ttlDays = 30)}>30d</button>
          <button type="button" class="ttl-preset" class:selected={d.ttlDays === 90}
            onclick={() => (d.ttlDays = 90)}>90d</button>
          <input type="number" min="1" max="365"
            bind:value={d.ttlDays} class="ttl-custom" />
          <span>days</span>
        </div>
        <p class="muted small">
          Fully accountable while alive. On expiration the hub deletes
          the entries and records a signed tombstone. Once gone, the
          content is gone.
        </p>
      {/if}
    </fieldset>

    <label>
      <span>First message</span>
      <textarea bind:value={d.message} rows="3"
        placeholder="Type the first message (optional — you can post later)…"></textarea>
    </label>

    {#if d.error}
      <p class="failure" role="alert">{d.error}</p>
    {/if}

    <div class="modal-actions">
      <button type="button" class="ghost" onclick={() => app.closeNewThreadDialog()}
        disabled={d.submitting}>Cancel</button>
      <button type="button" onclick={() => app.submitNewThread()}
        disabled={d.submitting || d.name.trim() === ''}>
        {d.submitting
          ? 'Creating…'
          : (d.scope === 'private' ? 'Create private thread' : 'Create thread')}
      </button>
    </div>
  </div>
{/if}

<style>
  /* v0.4.30: + New thread dialog styles. Lifted from InboxPanel
     together with the markup so the dialog renders correctly from
     either trigger (InboxPanel header button OR sidebar button). */
  .modal-backdrop {
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.5);
    z-index: 50;
  }
  .modal {
    position: fixed;
    z-index: 51;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    max-width: 520px; width: calc(100vw - 4rem);
    max-height: calc(100vh - 4rem);
    overflow-y: auto;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.4rem 1.6rem;
  }
  .modal h3 { margin: 0 0 0.85rem; font-size: 1.1rem; }
  .modal label { display: block; margin: 0.7rem 0; }
  .modal label > span {
    display: block; font-size: 0.82rem; color: var(--muted);
    margin-bottom: 0.3rem;
  }
  .modal input[type="text"],
  .modal textarea {
    width: 100%; box-sizing: border-box;
    background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 0.45rem 0.65rem; font: inherit; font-size: 0.9rem;
  }
  .modal textarea { font-family: inherit; resize: vertical; }
  fieldset.scope {
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.6rem 0.85rem 0.4rem;
    margin: 0.85rem 0;
  }
  fieldset.scope legend {
    padding: 0 0.4rem;
    font-size: 0.78rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  label.radio {
    display: flex; align-items: center; gap: 0.5rem;
    margin: 0.3rem 0;
    font-size: 0.92rem;
  }
  label.radio input { margin: 0; }
  label.radio span { display: inline; font-size: inherit; color: inherit; }
  /* v0.4.38: TTL picker inside the Retention fieldset. */
  .ttl-row {
    display: flex; align-items: center; gap: 0.4rem;
    margin: 0.5rem 0 0.15rem;
    font-size: 0.85rem;
  }
  .ttl-preset {
    background: transparent; color: var(--fg);
    border: 1px solid var(--border); border-radius: 999px;
    padding: 0.15rem 0.6rem; cursor: pointer;
    font-size: 0.8rem;
  }
  .ttl-preset.selected {
    background: rgba(212, 175, 55, 0.15);
    border-color: rgba(212, 175, 55, 0.5);
    color: #e8c96b;
  }
  .ttl-custom {
    width: 4rem;
    background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 0.15rem 0.4rem; font: inherit; font-size: 0.85rem;
  }
  .muted.small { color: var(--muted); font-size: 0.78rem; margin: 0.3rem 0 0; }
  /* v0.4.38: ephemeral banner + tombstone card. */
  .ephemeral-banner {
    display: flex; align-items: center; gap: 0.5rem;
    padding: 0.5rem 0.85rem; margin: 0.6rem 0;
    background: rgba(212, 175, 55, 0.08);
    border: 1px solid rgba(212, 175, 55, 0.35);
    border-radius: 8px;
    font-size: 0.88rem;
  }
  /* v0.4.43: "…" menu inside the ephemeral banner. Only shown to the
     creator. Opens an in-app confirm card, no browser dialogs. */
  .eph-menu-wrap { margin-left: auto; position: relative; }
  .eph-menu-btn {
    background: transparent; color: var(--muted);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 0.1rem 0.55rem; cursor: pointer;
    font-size: 1rem; line-height: 1; letter-spacing: 0.1em;
  }
  .eph-menu-btn:hover { color: var(--fg); border-color: rgba(212, 175, 55, 0.5); }
  .eph-menu {
    position: absolute; top: 100%; right: 0;
    margin-top: 0.35rem;
    background: var(--panel);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 0.3rem;
    min-width: 12rem;
    z-index: 10;
    box-shadow: 0 4px 16px rgba(0,0,0,0.35);
  }
  .eph-menu-item {
    display: block; width: 100%; text-align: left;
    background: transparent; color: var(--fg);
    border: none; border-radius: 6px;
    padding: 0.4rem 0.6rem; cursor: pointer;
    font: inherit; font-size: 0.88rem;
  }
  .eph-menu-item:hover { background: rgba(255, 255, 255, 0.04); }
  .eph-menu-item.danger { color: #d97a7a; }
  .eph-menu-item.danger:hover { background: rgba(220, 38, 38, 0.08); }
  /* Inline confirm card (renders below the banner). */
  .seal-confirm {
    margin: 0.4rem 0 0.6rem;
    padding: 0.8rem 1rem;
    background: rgba(220, 38, 38, 0.05);
    border: 1px solid rgba(220, 38, 38, 0.35);
    border-radius: 8px;
    font-size: 0.9rem;
  }
  .seal-confirm p { margin: 0 0 0.6rem; }
  .seal-confirm code {
    background: var(--bg); padding: 0.05rem 0.3rem;
    border-radius: 4px; font-size: 0.85rem;
  }
  .seal-confirm .failure {
    color: #d97a7a;
    font-size: 0.85rem;
  }
  .seal-confirm-actions {
    display: flex; gap: 0.5rem; justify-content: flex-end;
  }
  .seal-confirm-actions .ghost {
    background: transparent; color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 0.35rem 0.8rem; cursor: pointer; font-size: 0.85rem;
  }
  .seal-confirm-actions .danger {
    background: rgba(220, 38, 38, 0.15); color: #f0a0a0;
    border: 1px solid rgba(220, 38, 38, 0.5); border-radius: 6px;
    padding: 0.35rem 0.9rem; cursor: pointer; font-size: 0.85rem;
    font-weight: 500;
  }
  .seal-confirm-actions .danger:hover:not(:disabled) {
    background: rgba(220, 38, 38, 0.25);
  }
  .seal-confirm-actions button:disabled {
    opacity: 0.6; cursor: not-allowed;
  }
  .tombstone-card {
    display: flex; gap: 0.7rem; align-items: flex-start;
    padding: 0.85rem 1rem; margin: 0.6rem 0;
    background: rgba(120, 120, 120, 0.05);
    border: 1px solid var(--border); border-radius: 8px;
  }
  .tombstone-card p { margin: 0.2rem 0; }
  .tombstone-card code {
    font-size: 0.75rem; background: var(--bg);
    padding: 0.05rem 0.3rem; border-radius: 4px;
  }
  .audience-list {
    border: 1px dashed var(--border);
    border-radius: 8px;
    padding: 0.7rem 0.85rem;
    margin: 0.85rem 0;
    max-height: 12rem;
    overflow-y: auto;
  }
  .audience-list .self-line {
    margin: 0 0 0.5rem;
    font-size: 0.88rem;
    color: rgb(120, 200, 140);
  }
  .audience-list ul { list-style: none; margin: 0; padding: 0; }
  .audience-list li { padding: 0.15rem 0; }
  .audience-list label {
    display: flex; align-items: center; gap: 0.55rem;
    margin: 0; cursor: pointer;
    font-size: 0.9rem;
  }
  .audience-list label .name { font-weight: 500; color: var(--fg); }
  .audience-list label .role-tag {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border);
    padding: 0.05rem 0.4rem;
    border-radius: 999px;
  }
  .modal-actions {
    display: flex; justify-content: flex-end; gap: 0.5rem;
    margin-top: 1rem;
  }
  .modal-actions button {
    background: #d4af37; color: #0a0a0a;
    border: none; border-radius: 999px;
    padding: 0.5rem 1.2rem; font: inherit; font-weight: 600;
    cursor: pointer;
  }
  .modal-actions button:hover:not(:disabled) { background: #e2bf4e; }
  .modal-actions button:disabled {
    background: var(--border); color: var(--muted); cursor: not-allowed;
  }
  .modal-actions button.ghost {
    background: transparent; border: 1px solid var(--border);
    color: var(--muted);
  }
  .modal-actions button.ghost:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.04); color: var(--fg);
  }
  .modal .failure {
    margin: 0.7rem 0 0; padding: 0.5rem 0.75rem;
    background: rgba(220, 38, 38, 0.08);
    border: 1px solid rgba(220, 38, 38, 0.4);
    color: #fca5a5; font-size: 0.86rem;
    border-radius: 6px;
  }
  .modal .small { font-size: 0.78rem; }
  .modal .muted { color: var(--muted); }

  .layout {
    display: flex;
    height: 100vh;
    overflow: hidden;
    position: relative;
  }
  /* v0.4.45: sidebar toggle button. Positioned in the top-left corner
     of the layout container so it's reachable from any panel. Higher
     z-index than the backdrop so it stays clickable. */
  .sidebar-toggle {
    position: absolute;
    /* v0.4.46: max() honors the iPhone status-bar / notch safe area
       when the PWA is installed to Home Screen (standalone mode).
       On desktop and Android the env() value is 0, so max() falls
       back to the base 0.6rem and the button stays where it was. */
    top: max(0.6rem, calc(env(safe-area-inset-top) + 0.4rem));
    left: max(0.6rem, calc(env(safe-area-inset-left) + 0.4rem));
    /* v0.4.51: on desktop when the sidebar is open, shift the toggle
       to just RIGHT of the sidebar's edge so it sits at the sidebar
       boundary (chevron collapsing inward) instead of on top of the
       sidebar's own header. Mobile behavior is unchanged (drawer is
       an overlay; button overlays it and taps to close). */
    z-index: 20;
    background: var(--panel);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 6px;
    /* v0.4.46: bigger tap target for phones (was 0.25rem × 0.65rem —
       roughly 30×20 px on iPhone, well below Apple's 44×44 pt guideline). */
    padding: 0.5rem 0.85rem;
    font-size: 1.15rem;
    line-height: 1;
    cursor: pointer;
    opacity: 0.75;
    /* v0.4.57: single transition declaration — the earlier `transition:
       left 120ms ease;` at the top of this block was being clobbered by
       the second transition further down, so the `left` shift on
       .pushed snapped instantly. Combined here. */
    transition: left 120ms ease, opacity 120ms, border-color 120ms;
  }
  .sidebar-toggle:hover {
    opacity: 1;
    border-color: rgba(212, 175, 55, 0.5);
  }
  /* v0.4.51: on viewports where the sidebar sits inline (not overlay),
     push the toggle out of the sidebar's column when it's open. 240px
     matches the sidebar's width in ThreadList.svelte.
     v0.4.57: switched from a `:global(.layout.sidebar-open) .sidebar-toggle`
     descendant selector to a direct `.sidebar-toggle.pushed` class binding.
     The descendant form was landing under Svelte's CSS scoper in a way
     that kept the button at left:0.6rem — right on top of the sidebar
     header — even when the sidebar was open. The direct class avoids
     any :global() interaction and is simpler to reason about. */
  @media (min-width: 641px) {
    .sidebar-toggle.pushed {
      left: calc(240px + 0.6rem);
    }
  }
  /* v0.4.45: mobile sidebar backdrop. Hidden on desktop; shown on
     narrow viewports only when the sidebar is open. */
  .sidebar-backdrop {
    display: none;
  }
  /* v0.4.45: sidebar collapse. Uses width transition so the layout
     doesn't jump. When collapsed, ThreadList's border-right is also
     hidden via :global. */
  :global(.layout.sidebar-closed > .thread-list) {
    width: 0;
    border-right: none;
    overflow: hidden;
  }
  .thread {
    flex: 1;
    overflow-y: auto;
    padding: 1.5rem;
    /* v0.4.46: leave headroom for the toggle button so the first line
       of the header doesn't sit under it. On PWAs installed to home
       screen, add the iPhone status-bar / notch safe area on top of
       that clearance so the toggle isn't cramped against the top edge. */
    padding-top: calc(env(safe-area-inset-top, 0px) + 3.25rem);
    /* The .feed + compose box are the centered column; the
       scrollable region itself stretches to fill the pane. */
  }
  /* On mobile, sidebar becomes an overlay drawer. Fixed position,
     full height, slides in from the left. Backdrop dims the content. */
  @media (max-width: 640px) {
    :global(.layout > .thread-list) {
      position: fixed;
      top: 0; left: 0; bottom: 0;
      z-index: 15;
      width: 80vw;
      max-width: 300px;
      transition: transform 200ms ease;
      transform: translateX(-100%);
    }
    :global(.layout.sidebar-open > .thread-list) {
      transform: translateX(0);
      width: 80vw;
      border-right: 1px solid var(--border);
    }
    :global(.layout.sidebar-closed > .thread-list) {
      transform: translateX(-100%);
      width: 80vw;
    }
    .layout.sidebar-open .sidebar-backdrop {
      display: block;
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0, 0, 0, 0.5);
      z-index: 12;
    }
    .thread {
      /* v0.4.46: on mobile keep the safe-area clearance from the
         non-mobile rule; only the horizontal padding tightens. */
      padding-top: calc(env(safe-area-inset-top, 0px) + 3.25rem);
      padding-left: 0.9rem;
      padding-right: 0.9rem;
      padding-bottom: 1rem;
    }
    /* v0.4.50: on mobile, stack the header vertically. A long thread
       name gets the whole first line; the view-toggle + status +
       archive cluster gets its own row underneath. Prevents the
       right cluster from being crushed to unreadable width by a long
       name, and prevents a long name from being cramped by the cluster. */
    section.thread header {
      flex-direction: column;
      align-items: stretch;
      gap: 0.6rem;
      margin-bottom: 1rem;
    }
    section.thread header > .head-right {
      justify-content: flex-start;
      gap: 0.7rem;
    }
    section.thread header h1 {
      font-size: 1.25rem;
    }
    section.thread .status {
      padding-top: 0;
    }
  }
  .thread > :global(*) {
    max-width: 720px;
    margin-left: auto;
    margin-right: auto;
  }
  header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.5rem;
  }
  header > div:first-child {
    /* v0.4.50: give the name column something to shrink into so long
       thread names don't push the right cluster off the edge. */
    min-width: 0;
    flex: 1 1 auto;
  }
  h1 {
    margin: 0 0 0.25rem;
    font-size: 1.4rem;
    font-weight: 600;
    /* v0.4.50: wrap long names inside the h1 rather than pushing
       the container width; break-word covers URL-shaped names too. */
    overflow-wrap: anywhere;
    word-break: break-word;
    line-height: 1.15;
  }
  .muted {
    margin: 0;
    color: var(--muted);
    font-size: 0.88rem;
  }
  .status {
    color: var(--muted);
    font-size: 0.82rem;
    padding-top: 0.4rem;
  }
  .status.error {
    color: #fca5a5;
  }
  .status.pulse {
    color: rgba(212, 175, 55, 0.8);
    animation: pulse 3s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 0.7; }
    50%      { opacity: 1; }
  }
  .feed {
    margin-bottom: 1rem;
  }
  .feed.chat-mode {
    /* Tighter rhythm — chat mode prefers density over breathing room. */
    line-height: 1.4;
  }
  .day-divider {
    /* v0.4.20: centered pill between messages from different calendar
       days. Subtle so it doesn't compete with content, but visible
       enough that "what day is this?" is answered without scanning a
       full timestamp. */
    display: flex;
    align-items: center;
    justify-content: center;
    margin: 1rem 0 0.4rem;
    font-size: 0.74rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    position: relative;
  }
  .day-divider::before,
  .day-divider::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border);
    opacity: 0.55;
  }
  .day-divider span {
    padding: 0 0.7rem;
    background: var(--bg);
  }
  /* v0.4.25: archive affordances. The banner sits between the header
     and the feed so it's read before the first message. The dialog is
     inline (not modal) — it sits in the same column. */
  .archive-banner {
    display: flex; gap: 0.6rem; align-items: center;
    background: rgba(212, 175, 55, 0.08);
    border: 1px solid rgba(212, 175, 55, 0.3);
    border-radius: 8px;
    padding: 0.6rem 0.85rem;
    margin: 0 0 1rem;
    font-size: 0.88rem;
    color: var(--fg);
  }
  .archive-dialog {
    border: 1px dashed rgba(212, 175, 55, 0.4);
    border-radius: 10px;
    padding: 1rem 1.2rem;
    background: var(--panel);
    margin: 0 0 1rem;
  }
  .archive-dialog h3 { margin: 0 0 0.4rem; font-size: 1rem; }
  .archive-dialog .muted {
    color: var(--muted); margin: 0 0 0.7rem; font-size: 0.86rem;
  }
  .archive-dialog label { display: block; margin: 0.5rem 0; }
  .archive-dialog label span {
    display: block; font-size: 0.78rem; color: var(--muted);
    margin-bottom: 0.25rem;
  }
  .archive-dialog input {
    width: 100%; box-sizing: border-box;
    background: var(--bg); color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.45rem 0.65rem;
    font: inherit;
    font-size: 0.9rem;
  }
  .archive-actions {
    display: flex; justify-content: flex-end; gap: 0.5rem;
    margin-top: 0.7rem;
  }
  button.archive-btn { padding: 0.32rem 0.85rem; font-size: 0.85rem; }

  /* v0.4.27: audience chip + edit dialog. The chip sits above the
     feed (next to the archive banner slot) so the audience is the
     first thing read on opening a private thread. */
  .audience-chip {
    display: flex; align-items: center; gap: 0.5rem;
    background: rgba(120, 180, 255, 0.08);
    border: 1px solid rgba(120, 180, 255, 0.3);
    border-radius: 8px;
    padding: 0.5rem 0.85rem;
    margin: 0 0 1rem;
    cursor: pointer;
    color: var(--fg);
    text-align: left;
    font-size: 0.88rem;
    width: 100%;
  }
  .audience-chip:hover {
    background: rgba(120, 180, 255, 0.12);
  }
  .audience-chip .names { flex: 1; }
  .audience-chip .name { font-weight: 500; }
  .audience-chip .comma { color: var(--muted); }
  .audience-chip .edit-hint {
    color: var(--muted); font-size: 0.78rem;
    flex-shrink: 0;
  }
  .audience-dialog {
    border: 1px dashed rgba(120, 180, 255, 0.4);
    border-radius: 10px;
    padding: 1rem 1.2rem;
    background: var(--panel);
    margin: 0 0 1rem;
  }
  .audience-dialog h3 { margin: 0 0 0.4rem; font-size: 1rem; }
  .audience-dialog .muted {
    color: var(--muted); margin: 0 0 0.85rem; font-size: 0.86rem;
  }
  .audience-edit-list {
    list-style: none; margin: 0 0 0.5rem; padding: 0;
    max-height: 14rem; overflow-y: auto;
  }
  .audience-edit-list li {
    padding: 0.1rem 0;
  }
  .audience-edit-list label {
    display: flex; align-items: center; gap: 0.55rem;
    margin: 0; cursor: pointer;
    font-size: 0.9rem;
  }
  .audience-edit-list label .name { font-weight: 500; }
  .audience-edit-list label .role-tag {
    font-size: 0.7rem; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--muted);
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border);
    padding: 0.05rem 0.4rem; border-radius: 999px;
  }
  .empty {
    color: var(--muted);
    text-align: center;
    padding: 2rem;
  }
  .head-right {
    display: flex;
    align-items: center;
    gap: 0.85rem;
    flex-shrink: 0;
    /* v0.4.50: allow the cluster to wrap under itself instead of
       squeezing the toggle buttons on a cramped viewport. */
    flex-wrap: wrap;
    justify-content: flex-end;
  }
  .view-toggle {
    display: inline-flex;
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    font-size: 0.78rem;
    flex-shrink: 0;
  }
  .view-toggle button {
    appearance: none;
    background: transparent;
    border: 0;
    padding: 0.32rem 0.65rem;
    color: var(--muted);
    cursor: pointer;
    font: inherit;
    /* v0.4.50: force each button to be a whole word wide so a cramped
       parent can't clip "Cards" to "Car". */
    white-space: nowrap;
    flex-shrink: 0;
  }
  .view-toggle button.active {
    background: rgba(212, 175, 55, 0.18);
    color: rgb(212, 175, 55);
  }
  .view-toggle button:not(.active):hover {
    background: var(--panel);
  }
</style>
