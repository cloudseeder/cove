<!--
  Onboarding panel — v0.4.0 on-device key generation flow.

  Member's first launch:
    1. Enter name + hub URL → press "Get started"
    2. App generates keys (priv straight to OS keychain), shows the
       pairing payload as a QR + copyable link + readable fingerprint
    3. WebSocket /pending/watch is held open; when the keymaster
       attests, the app transitions out of this panel automatically

  No polling. No paste. The pairing payload is the entire artifact the
  member sends to the keymaster (in person, via Signal, whatever) —
  trust flows from the channel that link travelled over, the same way
  WhatsApp Web / Steam Guard pairing does.
-->
<script lang="ts">
  import type { AppState } from '$lib/cove/state.svelte';
  import { qrSvg } from '$lib/cove/pairing';
  import { sanitizeThreadName } from '$lib/cove/threadname';

  interface Props {
    app: AppState;
    /** Switch back to the AuthPanel (existing keys / paste mode). */
    onBack: () => void;
  }
  let { app, onBack }: Props = $props();

  let hubUrl = $state('https://cove.oap.dev');
  let nameHint = $state('');
  // v0.4.12: a brand-new member has no way to know which thread to
  // land on — the question is meaningless to them. Default to 'general'
  // (or whatever they last visited if they happen to have prior
  // localStorage state from this device); once attested, the sidebar
  // shows the real thread list and they can navigate.
  const initialThread = (typeof localStorage !== 'undefined'
    && localStorage.getItem('cove.thread')) || 'general';

  // ---- derived views into the AppState onboarding state machine ----
  const status = $derived(app.onboardStatus);
  const isGenerating = $derived(status.kind === 'generating');
  const isWaiting = $derived(status.kind === 'waiting');
  const isError = $derived(status.kind === 'error');

  const qrSvgString = $derived(
    status.kind === 'waiting' ? qrSvg(status.pairingLink, { size: 240 }) : '',
  );

  async function start() {
    if (!nameHint.trim()) return;
    await app.generateAndPair({
      hubUrl: hubUrl.trim(),
      nameHint: nameHint.trim(),
      thread: sanitizeThreadName(initialThread) || 'general',
    });
  }

  let copied = $state(false);
  async function copyLink() {
    if (status.kind !== 'waiting') return;
    try {
      await navigator.clipboard.writeText(status.pairingLink);
      copied = true;
      setTimeout(() => { copied = false; }, 1500);
    } catch {
      // Clipboard API unavailable — silent; the link is visible anyway.
    }
  }

  function back() {
    app.cancelOnboarding();
    onBack();
  }
</script>

<section class="onboard" aria-label="Set up your Cove identity">
  {#if status.kind === 'idle' || isGenerating || isError}
    <!-- Setup form -->
    <h1>Get started</h1>
    <p class="muted">
      Cove will generate a new keypair on this device. The private key
      stays in your OS keychain — neither the hub nor anyone else ever
      sees it. Your keymaster issues you an attestation that connects
      your key to your real name.
    </p>

    <label>
      <span>Your name</span>
      <input type="text" bind:value={nameHint}
        placeholder="How you want to appear in the directory"
        autocomplete="name" disabled={isGenerating} />
    </label>

    <label>
      <span>Hub URL</span>
      <input type="url" bind:value={hubUrl}
        placeholder="https://cove.oap.dev" disabled={isGenerating} />
    </label>

    {#if isError && status.kind === 'error'}
      <p class="failure" role="alert">{status.message}</p>
    {/if}

    <div class="actions">
      <button type="button" class="ghost" onclick={onBack}
        disabled={isGenerating}>
        I already have a key
      </button>
      <button type="button" onclick={start}
        disabled={isGenerating || !nameHint.trim() || !hubUrl.trim()}>
        {isGenerating ? 'Generating…' : 'Get started'}
      </button>
    </div>

  {:else if isWaiting && status.kind === 'waiting'}
    <!-- Waiting for keymaster approval — the active part of the flow -->
    <h1>Waiting for approval</h1>
    <p class="muted">
      Send the QR or link below to your keymaster. Your app will unlock
      automatically as soon as they attest your key.
    </p>

    <div class="qr">
      {@html qrSvgString}
    </div>

    <div class="field">
      <span class="field-label">Pairing link</span>
      <div class="link-row">
        <code class="link">{status.pairingLink}</code>
        <button type="button" class="copy" onclick={copyLink}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
    </div>

    <div class="field">
      <span class="field-label">
        Fingerprint (compare with the one your keymaster sees)
      </span>
      <code class="fingerprint">{status.fingerprint}</code>
    </div>

    <div class="status-pulse">
      <span class="dot"></span>
      <span>Listening for your attestation…</span>
    </div>

    <div class="actions">
      <button type="button" class="ghost" onclick={back}>Back</button>
    </div>

  {:else if status.kind === 'attested'}
    <!-- Brief transition state before connect() takes over -->
    <h1>You're in</h1>
    <p class="muted">Connecting…</p>
  {/if}
</section>

<style>
  .onboard {
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
  input {
    width: 100%;
    box-sizing: border-box;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.55rem 0.75rem;
    font: inherit;
    font-size: 0.95rem;
  }
  input:focus {
    outline: none;
    border-color: rgba(212, 175, 55, 0.55);
  }
  input:disabled {
    opacity: 0.6;
  }
  .qr {
    display: flex;
    justify-content: center;
    padding: 1rem 0 1.4rem;
  }
  .qr :global(svg) {
    border-radius: 12px;
    box-shadow: 0 0 0 1px var(--border);
  }
  .link-row {
    display: flex;
    gap: 0.4rem;
    align-items: stretch;
  }
  .link {
    flex: 1;
    padding: 0.55rem 0.75rem;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 8px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.78rem;
    overflow-x: auto;
    white-space: nowrap;
  }
  .copy {
    padding: 0 1rem;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
    border-radius: 8px;
    font: inherit;
    font-size: 0.85rem;
    cursor: pointer;
  }
  .copy:hover {
    color: #e8c96b;
    border-color: rgba(212, 175, 55, 0.4);
  }
  .fingerprint {
    display: block;
    padding: 0.7rem 0.75rem;
    background: var(--bg);
    color: #e8c96b;
    border: 1px solid var(--border);
    border-radius: 8px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.95rem;
    letter-spacing: 0.04em;
    text-align: center;
  }
  .status-pulse {
    margin: 1.5rem 0 0.6rem;
    display: flex;
    align-items: center;
    gap: 0.55rem;
    color: var(--muted);
    font-size: 0.88rem;
  }
  .dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: #d4af37;
    box-shadow: 0 0 8px rgba(212, 175, 55, 0.6);
    animation: pulse 1.4s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 0.4; transform: scale(0.85); }
    50%      { opacity: 1; transform: scale(1.1); }
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
    background: rgba(255, 255, 255, 0.04);
    color: var(--fg);
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
