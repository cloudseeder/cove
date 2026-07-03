<!--
  Add-hub modal (v0.4.69 — federation UI, Phase 2).

  Rendered by +page.svelte as an overlay when app.addHubOpen is true.
  Uses the ALREADY-LIVE keypair from the active session — the user has
  already unlocked their keys against whichever hub they're currently
  authenticated on. All this panel asks for is the new hub URL. On
  submit it calls app.connect() which routes through addHub() +
  switchToHub() + HubConnection.authenticate().

  Tauri path: the OS keychain signs — no priv needs to cross into JS.
  PWA / paste path: uses app.livePriv (set at unlock/onboarding time)
  to construct the new HubConnection's InJSSigner. If livePriv is null
  (session dropped for some reason), we surface a helpful error and
  suggest the user go back to full re-onboarding.

  Closes on: successful auth (activeHubUrl flips automatically),
  Cancel button, backdrop click, Escape key.
-->
<script lang="ts">
  import type { AppState } from '$lib/cove/state.svelte';
  import { hubLabel } from '$lib/cove/hubs';

  interface Props { app: AppState; }
  let { app }: Props = $props();

  let hubUrl = $state('');
  let submitting = $state(false);
  let error = $state<string | null>(null);

  const myPubkey = $derived(
    app.authStatus.kind === 'authenticated' ? app.authStatus.pubkey : null,
  );

  function normalizeUrl(raw: string): string {
    const trimmed = raw.trim().replace(/\/$/, '');
    if (!trimmed) return '';
    if (!/^https?:\/\//i.test(trimmed)) return `https://${trimmed}`;
    return trimmed;
  }

  async function connect() {
    error = null;
    const url = normalizeUrl(hubUrl);
    if (!url) {
      error = 'Hub URL is required.';
      return;
    }
    if (app.hubs.has(url)) {
      error = `Already connected to ${hubLabel(url)}. Pick it from the switcher instead.`;
      return;
    }
    if (myPubkey === null) {
      error = 'No live session — sign in first, then add a hub from the sidebar.';
      return;
    }
    // PWA / paste path needs the priv material.
    if (!app.inTauri && !app.livePriv) {
      error = 'Live private key not available. Sign back in on your current hub to reset the session.';
      return;
    }
    submitting = true;
    try {
      await app.connect({
        hubUrl: url,
        publicKey: myPubkey,
        thread: 'annual-meeting',
        mode: app.inTauri ? 'keychain' : 'paste',
        privateKey: app.inTauri ? undefined : app.livePriv ?? undefined,
      });
      // If auth succeeded, the newly-added hub is now active. Close.
      if (app.activeHubUrl === url
        && app.activeHub?.authStatus.kind === 'authenticated') {
        close();
      } else {
        // Auth flow set authStatus to 'failed' internally — expose the reason.
        const st = app.activeHub?.authStatus;
        if (st?.kind === 'failed') error = st.reason;
        else error = 'Could not connect to that hub.';
      }
    } catch (err) {
      error = (err as Error).message;
    } finally {
      submitting = false;
    }
  }

  function close() {
    app.addHubOpen = false;
    hubUrl = '';
    error = null;
  }

  function onBackdrop() { close(); }
  function onKey(ev: KeyboardEvent) {
    if (ev.key === 'Escape') close();
  }

  $effect(() => {
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });
</script>

<div class="backdrop" onclick={onBackdrop} role="presentation"></div>
<aside class="panel" role="dialog" aria-modal="true" aria-label="Add another hub">
  <header>
    <h2>Add another hub</h2>
    <button type="button" class="close" onclick={close}
      aria-label="Close">×</button>
  </header>

  <p class="lede">
    Same identity (<code>{myPubkey?.slice(0, 8)}…{myPubkey?.slice(-4)}</code>)
    across multiple hubs. Enter the URL of a Cove hub whose board has
    attested your public key.
  </p>

  <label>
    <span>Hub URL</span>
    <input type="url" bind:value={hubUrl}
      placeholder="https://another-hub.example.com"
      autocapitalize="off" autocorrect="off" spellcheck="false"
      disabled={submitting}
      onkeydown={(e) => { if (e.key === 'Enter') void connect(); }} />
  </label>

  {#if error}
    <p class="error" role="alert">{error}</p>
  {/if}

  <div class="actions">
    <button type="button" class="ghost" onclick={close} disabled={submitting}>
      Cancel
    </button>
    <button type="button" onclick={connect} disabled={submitting || hubUrl.trim() === ''}>
      {submitting ? 'Connecting…' : 'Connect'}
    </button>
  </div>
</aside>

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.45);
    z-index: 60;
    animation: fade 160ms ease;
  }
  .panel {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: min(460px, calc(100vw - 2rem));
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 14px;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
    padding: 1.3rem 1.5rem 1.5rem;
    z-index: 70;
    animation: slide 200ms ease;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.4rem;
  }
  h2 {
    margin: 0;
    font-size: 1.05rem;
    font-weight: 600;
  }
  .close {
    background: transparent;
    border: none;
    color: var(--muted);
    cursor: pointer;
    font-size: 1.4em;
    line-height: 1;
    padding: 0.1em 0.5em;
    border-radius: 6px;
  }
  .close:hover {
    background: rgba(255, 255, 255, 0.05);
    color: var(--fg);
  }
  .lede {
    color: var(--muted);
    font-size: 0.9rem;
    margin: 0.15rem 0 1rem;
    line-height: 1.5;
  }
  .lede code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.86em;
    background: rgba(255, 255, 255, 0.04);
    padding: 0.05rem 0.35rem;
    border-radius: 4px;
    color: var(--fg);
  }
  label {
    display: block;
    margin: 0.4rem 0 0.6rem;
  }
  label > span {
    display: block;
    font-size: 0.82rem;
    color: var(--muted);
    margin-bottom: 0.3rem;
  }
  input {
    width: 100%;
    box-sizing: border-box;
    background: var(--panel);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.5rem 0.7rem;
    font: inherit;
    font-size: 0.9rem;
  }
  input:focus {
    outline: none;
    border-color: rgba(212, 175, 55, 0.55);
  }
  .error {
    margin: 0.6rem 0 0;
    padding: 0.55rem 0.75rem;
    border-radius: 8px;
    background: rgba(220, 38, 38, 0.08);
    border: 1px solid rgba(220, 38, 38, 0.4);
    color: #fca5a5;
    font-size: 0.85rem;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.6rem;
    margin-top: 1.2rem;
  }
  button {
    background: #d4af37;
    color: #0a0a0a;
    border: none;
    border-radius: 999px;
    padding: 0.5rem 1.25rem;
    font: inherit;
    font-weight: 600;
    cursor: pointer;
    transition: transform 120ms ease, background 200ms ease;
  }
  button:hover:not(:disabled) {
    transform: translateY(-1px);
    background: #e2bf4e;
  }
  button:disabled {
    background: var(--border);
    color: var(--muted);
    cursor: not-allowed;
  }
  button.ghost {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
  }
  button.ghost:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.04);
    color: var(--fg);
  }
  @keyframes fade {
    from { opacity: 0; } to { opacity: 1; }
  }
  @keyframes slide {
    from { opacity: 0; transform: translate(-50%, -46%); }
    to   { opacity: 1; transform: translate(-50%, -50%); }
  }
</style>
