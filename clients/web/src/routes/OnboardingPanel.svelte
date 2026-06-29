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
  // Default thread for the new member's first landing. Priority:
  //   1. v0.4.13+ hub-side default_thread hint from /directory
  //   2. localStorage cove.thread (this device's prior Cove history)
  //   3. 'general' as a final fallback
  // Resolved at start() time so the hub URL the user typed is honored.
  const localFallback = (typeof localStorage !== 'undefined'
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
    const url = hubUrl.trim();
    // Try the v0.4.13+ hub hint. Best-effort: if the hub is older, the
    // network is flaky, or the response isn't shaped as expected, fall
    // back to the local default. Either way the user lands somewhere.
    let chosenThread = localFallback;
    try {
      const res = await fetch(`${url}/directory`, { method: 'GET' });
      if (res.ok) {
        const manifest = await res.json();
        if (typeof manifest?.default_thread === 'string'
            && manifest.default_thread.length > 0) {
          chosenThread = manifest.default_thread;
        }
      }
    } catch {
      // network error → silent fallback to localFallback
    }
    await app.generateAndPair({
      hubUrl: url,
      nameHint: nameHint.trim(),
      thread: sanitizeThreadName(chosenThread) || 'general',
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
    <!-- Waiting for keymaster approval — the active part of the flow.
         The hub already has the new member's pubkey from the /pending
         POST during start(); the keymaster sees this request in their
         admin queue automatically. So the user's job here isn't to
         "deliver" the pubkey (the hub did that) — it's to help the
         keymaster confirm "yes, that's me" before they approve.
         The fingerprint is the verification artifact. The QR + deep
         link are secondary (work for in-person scenarios where the
         keymaster scans / clicks; deep links are unreliable across
         platforms, so they're not the primary path). -->
    <h1>Waiting for approval</h1>
    <p class="muted">
      Your keymaster has been notified. To confirm it's you, share
      this fingerprint with them by voice, text, or in person — they'll
      see the same one next to your row in their queue.
    </p>

    <div class="field">
      <span class="field-label">Your fingerprint</span>
      <code class="fingerprint">{status.fingerprint}</code>
    </div>

    <div class="status-pulse">
      <span class="dot"></span>
      <span>Listening for your attestation…</span>
    </div>

    <details class="more-ways">
      <summary>More ways to share</summary>
      <p class="muted small">
        These let the keymaster pre-fill the approval from a phone scan
        or a clicked link, when device support cooperates. They aren't
        required — the queue entry already has everything they need.
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
    </details>

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
    padding: 0.9rem 0.75rem;
    background: var(--bg);
    color: #e8c96b;
    border: 1px solid var(--border);
    border-radius: 8px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 1.05rem;
    letter-spacing: 0.05em;
    text-align: center;
    word-spacing: 0.1em;
  }
  .more-ways {
    margin: 1.4rem 0 0.4rem;
    border-top: 1px solid var(--border);
    padding-top: 1rem;
  }
  .more-ways > summary {
    cursor: pointer;
    color: var(--muted);
    font-size: 0.88rem;
    user-select: none;
    padding: 0.2rem 0;
  }
  .more-ways > summary:hover {
    color: var(--fg);
  }
  .more-ways .small {
    font-size: 0.82rem;
    margin: 0.6rem 0 0.9rem;
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
