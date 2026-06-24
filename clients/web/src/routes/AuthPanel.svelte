<!--
  Auth panel — hub URL + keypair (paste box or drop a .priv/.pub pair).
  Slice 3 replaces the paste boxes with Tauri keychain custody.
-->
<script lang="ts">
  import type { AppState } from '$lib/cove/state.svelte';

  interface Props {
    app: AppState;
  }
  let { app }: Props = $props();

  let hubUrl = $state('http://localhost:8000');
  let priv = $state('');
  let pub = $state('');
  let thread = $state('annual-meeting');

  async function dropKeyfile(ev: DragEvent) {
    ev.preventDefault();
    const files = ev.dataTransfer?.files;
    if (!files) return;
    for (const f of Array.from(files)) {
      const text = (await f.text()).trim();
      if (f.name.endsWith('.priv')) priv = text;
      else if (f.name.endsWith('.pub')) pub = text;
    }
  }

  async function connect() {
    await app.connect({ hubUrl, privateKey: priv.trim(), publicKey: pub.trim(), thread });
  }

  function preventDefault(ev: DragEvent) {
    ev.preventDefault();
  }

  let connecting = $derived(app.authStatus.kind === 'connecting');
  let failure = $derived(
    app.authStatus.kind === 'failed' ? app.authStatus.reason : null,
  );
</script>

<section
  class="auth"
  ondrop={dropKeyfile}
  ondragover={preventDefault}
  aria-label="Connect to your hub"
>
  <h1>Connect</h1>
  <p class="muted">
    Drop a paired <code>.priv</code> and <code>.pub</code> file anywhere in this panel,
    or paste them. Hub URL is wherever your Cove server is running.
  </p>

  <label>
    <span>Hub URL</span>
    <input type="url" bind:value={hubUrl} placeholder="http://localhost:8000" />
  </label>

  <label>
    <span>Private key (hex)</span>
    <textarea bind:value={priv} rows="2" autocomplete="off" spellcheck="false"
      placeholder="64-char hex from scripts/gen_keys.py"></textarea>
  </label>

  <label>
    <span>Public key (hex)</span>
    <textarea bind:value={pub} rows="2" autocomplete="off" spellcheck="false"
      placeholder="64-char hex"></textarea>
  </label>

  <label>
    <span>Thread</span>
    <input type="text" bind:value={thread} placeholder="annual-meeting" />
  </label>

  <div class="actions">
    <button type="button" onclick={connect}
      disabled={connecting || !priv.trim() || !pub.trim()}>
      {connecting ? 'Connecting…' : 'Connect'}
    </button>
  </div>

  {#if failure}
    <p class="failure" role="alert">{failure}</p>
  {/if}
</section>

<style>
  .auth {
    max-width: 520px;
    margin: 4rem auto;
    padding: 2.5rem;
    border: 1px dashed var(--border);
    border-radius: 16px;
    background: var(--panel);
  }
  h1 {
    margin: 0 0 0.5rem;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  .muted {
    color: var(--muted);
    margin: 0 0 1.6rem;
    font-size: 0.95rem;
  }
  label {
    display: block;
    margin: 0.9rem 0;
  }
  label > span {
    display: block;
    font-size: 0.85rem;
    color: var(--muted);
    margin-bottom: 0.35rem;
  }
  input, textarea {
    width: 100%;
    box-sizing: border-box;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.55rem 0.75rem;
    font: inherit;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.86rem;
  }
  input:focus, textarea:focus {
    outline: none;
    border-color: rgba(212, 175, 55, 0.55);
  }
  textarea {
    resize: vertical;
    min-height: 2.4rem;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    margin-top: 1.5rem;
  }
  button {
    background: #d4af37;
    color: #0a0a0a;
    border: none;
    border-radius: 999px;
    padding: 0.55rem 1.4rem;
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
  .failure {
    margin-top: 1rem;
    padding: 0.8rem 1rem;
    border-radius: 8px;
    background: rgba(220, 38, 38, 0.08);
    border: 1px solid rgba(220, 38, 38, 0.4);
    color: #fca5a5;
    font-size: 0.9rem;
  }
</style>
