<!--
  Verification ceremony component — the visual identity of the app.

  Three states, deliberately asymmetric:

    'verified'   — gold seal, subtle bloom. Tap to reveal the full chain.
                   Most of the time, ambient; reveals on demand.
    'pending'    — neutral outline, spinner-pulse. Brief, transient state
                   while the verify chain is running.
    'broken'     — red, jagged break across the seal. Tampering should feel
                   viscerally wrong, not quietly suspicious.

  This is the COMPONENT — page-level animation orchestration (e.g.
  bloom-in on first appearance) lives in the Entry/Thread layer above.
-->
<script lang="ts">
  type State = 'verified' | 'pending' | 'broken';

  interface Props {
    state: State;
    title?: string;       // e.g. "Verified from Board"
    summary?: string;     // one-line chain summary for the reveal modal
    onReveal?: () => void;
  }

  let { state, title = '', summary = '', onReveal }: Props = $props();
</script>

<button
  class="seal seal-{state}"
  type="button"
  aria-label={title || state}
  onclick={onReveal}
  disabled={!onReveal}
>
  <svg viewBox="0 0 64 64" aria-hidden="true" class="seal-glyph">
    <!--
      Concentric ring with an inner mark. State branches do the visual
      identity: gold concentric for verified, red jagged-break for broken,
      hollow with a slow pulse for pending.
    -->
    {#if state === 'verified'}
      <circle cx="32" cy="32" r="26" class="ring-outer" />
      <circle cx="32" cy="32" r="18" class="ring-inner" />
      <path d="M22 32 L29 39 L42 25" class="mark-check" />
    {:else if state === 'broken'}
      <circle cx="32" cy="32" r="26" class="ring-outer-broken" />
      <line x1="12" y1="12" x2="52" y2="52" class="mark-break" />
      <line x1="52" y1="12" x2="12" y2="52" class="mark-break" />
    {:else}
      <circle cx="32" cy="32" r="26" class="ring-pending" />
    {/if}
  </svg>
  {#if title}
    <span class="seal-title">{title}</span>
  {/if}
  {#if summary}
    <span class="seal-summary" aria-hidden="true">{summary}</span>
  {/if}
</button>

<style>
  .seal {
    display: inline-flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.5rem 0.9rem;
    border-radius: 999px;
    background: transparent;
    border: 1px solid transparent;
    font: inherit;
    color: inherit;
    cursor: pointer;
    transition: transform 120ms ease, background 200ms ease, border-color 200ms ease;
  }
  .seal:disabled {
    cursor: default;
  }
  .seal:hover:not(:disabled) {
    transform: translateY(-1px);
  }

  .seal-glyph {
    width: 1.4em;
    height: 1.4em;
    flex-shrink: 0;
  }

  .seal-title {
    font-weight: 600;
    letter-spacing: 0.01em;
  }

  .seal-summary {
    color: var(--muted, #8a8a8a);
    font-size: 0.85em;
  }

  /* ---- verified: gold ring, subtle bloom ---- */
  .seal-verified {
    background: linear-gradient(180deg, rgba(212, 175, 55, 0.08), rgba(212, 175, 55, 0.02));
    border-color: rgba(212, 175, 55, 0.45);
    color: #d4af37;
  }
  .seal-verified .ring-outer {
    fill: none;
    stroke: #d4af37;
    stroke-width: 2;
  }
  .seal-verified .ring-inner {
    fill: none;
    stroke: rgba(212, 175, 55, 0.55);
    stroke-width: 1.4;
  }
  .seal-verified .mark-check {
    fill: none;
    stroke: #d4af37;
    stroke-width: 3;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  /* ---- broken: red, jagged break ---- */
  .seal-broken {
    background: rgba(220, 38, 38, 0.08);
    border-color: rgba(220, 38, 38, 0.6);
    color: #dc2626;
    animation: shake 300ms ease both;
  }
  .seal-broken .ring-outer-broken {
    fill: none;
    stroke: #dc2626;
    stroke-width: 2;
    stroke-dasharray: 4 6;
  }
  .seal-broken .mark-break {
    stroke: #dc2626;
    stroke-width: 3.2;
    stroke-linecap: round;
  }
  @keyframes shake {
    0%, 100% { transform: translateX(0); }
    25% { transform: translateX(-4px); }
    50% { transform: translateX(4px); }
    75% { transform: translateX(-2px); }
  }

  /* ---- pending: hollow ring, slow pulse ---- */
  .seal-pending {
    border-color: rgba(160, 160, 160, 0.4);
    color: var(--muted, #8a8a8a);
  }
  .seal-pending .ring-pending {
    fill: none;
    stroke: currentColor;
    stroke-width: 2;
    stroke-dasharray: 3 6;
    animation: spin 2.4s linear infinite;
    transform-origin: 32px 32px;
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
</style>
