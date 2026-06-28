<!--
  Auth panel. Two paths:

    - In Tauri:
        first launch  → import (paste/drop) → keys stored in OS keychain.
        subsequent     → unlock (no key handling in JS). Private key NEVER
                         reaches the webview after import.
    - In browser-only mode:
        slice-2 paste flow. Private key lives in JS heap; less secure but
        the app still works without a Tauri shell.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import type { AppState } from '$lib/cove/state.svelte';

  interface Props {
    app: AppState;
    /** v0.4.0: switch to the OnboardingPanel for first-time setup. */
    onOnboard?: () => void;
  }
  let { app, onOnboard }: Props = $props();

  let hubUrl = $state('http://localhost:8000');
  let priv = $state('');
  let pub = $state('');
  let thread = $state('annual-meeting');
  let importing = $state(false);
  let importError = $state<string | null>(null);
  /** v0.1.3: opt out of the keychain path inside Tauri and use the JS
   *  signer for the session instead. Useful when the OS keychain
   *  refuses writes (e.g. unsigned macOS builds), and as an explicit
   *  user opt-out from OS-level key storage. */
  let useTauriPaste = $state(false);

  // v0.4.8: remember last-used hub URL + thread across launches. Once a
  // member is attested to a hub, they overwhelmingly use that same hub
  // forever — defaulting back to localhost every relaunch was friction
  // with no upside. localStorage is persistent in the Tauri webview
  // per-origin, no plugin needed. Reads happen in onMount so SSR
  // prerender (where localStorage is undefined) doesn't blow up.
  onMount(async () => {
    const savedHub = localStorage.getItem('cove.hubUrl');
    const savedThread = localStorage.getItem('cove.thread');
    if (savedHub) hubUrl = savedHub;
    if (savedThread) thread = savedThread;
    await app.refreshKeychain();
  });

  // Save on every change. Partial typing gets persisted too, which is
  // fine — next launch shows whatever was last typed (good or bad), and
  // the user fixes it once. No need to gate on connect-success.
  $effect(() => {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('cove.hubUrl', hubUrl);
    }
  });
  $effect(() => {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('cove.thread', thread);
    }
  });

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

  function preventDefault(ev: DragEvent) {
    ev.preventDefault();
  }

  async function importToKeychain() {
    importError = null;
    importing = true;
    try {
      await app.importKeysToKeychain(priv.trim(), pub.trim());
      priv = '';   // never keep it in the JS heap after import
    } catch (err) {
      importError = (err as Error).message;
    } finally {
      importing = false;
    }
  }

  async function connectKeychain() {
    if (app.storedPublicKey === null) return;
    await app.connect({
      hubUrl, publicKey: app.storedPublicKey, thread, mode: 'keychain',
    });
  }

  async function connectPaste() {
    await app.connect({
      hubUrl, privateKey: priv.trim(), publicKey: pub.trim(), thread, mode: 'paste',
    });
  }

  async function forgetKeys() {
    await app.clearKeychain();
  }

  let connecting = $derived(app.authStatus.kind === 'connecting');
  let connectFailure = $derived(
    app.authStatus.kind === 'failed' ? app.authStatus.reason : null,
  );

  // ---- which view? ----
  //   - In Tauri w/ keys already in keychain → unlock.
  //   - In Tauri, no stored keys, NOT opted into paste → import (default).
  //   - In Tauri, opted into paste → paste (uses InJSSigner this session).
  //   - In browser → paste.
  let mode = $derived<'tauri-unlock' | 'tauri-import' | 'paste'>(
    app.inTauri && !useTauriPaste
      ? (app.storedPublicKey ? 'tauri-unlock' : 'tauri-import')
      : 'paste',
  );
</script>

<section class="auth" ondrop={dropKeyfile} ondragover={preventDefault}
  aria-label="Connect to your hub">

  {#if mode === 'tauri-unlock'}
    <!-- Returning user. Keys already in the OS keychain. -->
    <h1>Welcome back</h1>
    <p class="muted">
      Your private key is in this device's keychain. Sign in to open your
      threads — the key stays in the OS, never in the app.
    </p>

    <div class="field">
      <span class="field-label">Public key (from keychain)</span>
      <code class="readonly">{app.storedPublicKey}</code>
    </div>

    <label>
      <span>Hub URL</span>
      <input type="url" bind:value={hubUrl} placeholder="http://localhost:8000" />
    </label>

    <label>
      <span>Thread</span>
      <input type="text" bind:value={thread} placeholder="annual-meeting" />
    </label>

    <div class="actions">
      <button type="button" class="ghost" onclick={forgetKeys}>Forget this identity</button>
      <button type="button" onclick={connectKeychain} disabled={connecting}>
        {connecting ? 'Connecting…' : 'Unlock'}
      </button>
    </div>

  {:else if mode === 'tauri-import'}
    <!-- Tauri shell, first launch. The v0.4.0 default path is
         on-device generation via OnboardingPanel; the import form
         stays here as the "I already have keys" branch (members
         who got an attestation issued out-of-band, or who are
         re-attaching a device using an existing identity). -->
    {#if onOnboard}
      <h1>Welcome to Cove</h1>
      <p class="muted">
        First time here? Generate a key on this device — your keymaster
        will attest it for you and the app unlocks as soon as they do.
      </p>
      <div class="hero-actions">
        <button type="button" onclick={onOnboard}>Get started</button>
      </div>
      <p class="divider"><span>or</span></p>
    {/if}
    <h2>I already have a key</h2>
    <p class="muted">
      Drop a paired <code>.priv</code> and <code>.pub</code> file
      anywhere in this panel, or paste them. The private key goes
      straight to your OS keychain and never returns to the app.
    </p>

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

    <div class="actions">
      <button type="button" class="ghost"
        onclick={() => useTauriPaste = true}>
        Use paste mode instead
      </button>
      <button type="button" onclick={importToKeychain}
        disabled={importing || !priv.trim() || !pub.trim()}>
        {importing ? 'Importing…' : 'Import to keychain'}
      </button>
    </div>

    {#if importError}
      <p class="failure" role="alert">{importError}</p>
      <p class="muted small">
        Keychain unavailable? Use paste mode — the key stays in the
        app session only. Less secure but works while OS-level signing
        is being sorted out.
      </p>
    {/if}

  {:else}
    <!-- Paste flow.
         - Browser: the only flow available. Private key lives in JS
           heap; that's what running outside Tauri costs you.
         - Tauri w/ useTauriPaste opt-in: an explicit fallback when
           keychain custody isn't available (unsigned macOS) or the
           user wants to opt out of OS-level storage. Same JS-heap
           lifetime, but the choice is deliberate. -->
    <h1>Connect</h1>
    {#if app.inTauri && useTauriPaste}
      <p class="muted">
        Paste mode — your private key stays in the app's memory for
        this session only and never touches disk or the OS keychain.
        You'll paste it again next time you launch.
      </p>
    {:else}
      <p class="muted">
        Drop a paired <code>.priv</code> and <code>.pub</code> file anywhere
        in this panel, or paste them. For OS-keychain key custody, run this
        app via Tauri instead of a plain browser.
      </p>
    {/if}

    <label>
      <span>Hub URL</span>
      <input type="url" bind:value={hubUrl} placeholder="http://localhost:8000" />
    </label>

    <label>
      <span>Private key (hex)</span>
      <textarea bind:value={priv} rows="2" autocomplete="off" spellcheck="false"
        placeholder="64-char hex"></textarea>
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
      {#if app.inTauri && useTauriPaste}
        <button type="button" class="ghost"
          onclick={() => useTauriPaste = false}>
          Back to keychain import
        </button>
      {/if}
      <button type="button" onclick={connectPaste}
        disabled={connecting || !priv.trim() || !pub.trim()}>
        {connecting ? 'Connecting…' : 'Connect'}
      </button>
    </div>
  {/if}

  {#if connectFailure}
    <p class="failure" role="alert">{connectFailure}</p>
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
  h2 {
    margin: 0 0 0.5rem;
    font-weight: 600;
    font-size: 1.05rem;
    color: var(--fg);
  }
  .hero-actions {
    margin: 1.4rem 0 0;
    display: flex;
    justify-content: flex-start;
  }
  .divider {
    margin: 2rem 0 1.4rem;
    text-align: center;
    color: var(--muted);
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    position: relative;
  }
  .divider::before, .divider::after {
    content: '';
    position: absolute;
    top: 50%;
    width: 35%;
    height: 1px;
    background: var(--border);
  }
  .divider::before { left: 0; }
  .divider::after  { right: 0; }
  .divider span { background: var(--panel); padding: 0 0.6rem; }
  .muted {
    color: var(--muted);
    margin: 0 0 1.6rem;
    font-size: 0.95rem;
  }
  label, .field {
    display: block;
    margin: 0.9rem 0;
  }
  label > span, .field-label {
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
  .readonly {
    display: block;
    padding: 0.55rem 0.75rem;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--bg);
    color: var(--muted);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.82rem;
    word-break: break-all;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    gap: 0.6rem;
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
  button.ghost {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
  }
  button.ghost:hover:not(:disabled) {
    background: rgba(220, 38, 38, 0.06);
    border-color: rgba(220, 38, 38, 0.4);
    color: #fca5a5;
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
  .muted.small {
    font-size: 0.82rem;
    margin: 0.6rem 0 0;
  }
</style>
