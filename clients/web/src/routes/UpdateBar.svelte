<!--
  UpdateBar — slice 4b. A quiet, opt-in affordance for available
  updates. Lives above whichever panel is showing so it's present in
  both unauth and auth states, but only renders when there's
  something to say. No modal, no nag, no auto-install.

  Trust posture note: this UI prompts; the actual install is gated by
  the Tauri updater plugin's signature verification against the
  pubkey baked into tauri.conf.json. The button does not bypass
  verification — it cannot. A tampered bundle lands as updateStatus
  'error', shown red-toned.
-->
<script lang="ts">
  import type { AppState } from '$lib/cove/state.svelte';

  let { app }: { app: AppState } = $props();

  function dismiss() {
    app.updateStatus = { kind: 'idle' };
  }

  function pct(downloaded: number, total: number | null): string {
    if (total === null || total === 0) return '…';
    return `${Math.floor((downloaded / total) * 100)}%`;
  }
</script>

{#if app.updateStatus.kind === 'available'}
  <div class="bar bar-available" role="status">
    <span class="msg">
      Update available — <strong>v{app.updateStatus.update.version}</strong>
    </span>
    <button class="install" onclick={() => app.installUpdate()}>
      Install & restart
    </button>
    <button class="dismiss" onclick={dismiss} aria-label="Dismiss">×</button>
  </div>
{:else if app.updateStatus.kind === 'available-pwa'}
  <div class="bar bar-available" role="status">
    <span class="msg">
      A new version of Cove is ready. Reload to update.
    </span>
    <button class="install" onclick={() => app.applyPwaUpdate()}>
      Reload
    </button>
    <button class="dismiss" onclick={dismiss} aria-label="Dismiss">×</button>
  </div>
{:else if app.updateStatus.kind === 'installing'}
  <div class="bar bar-installing" role="status">
    <span class="msg">
      {#if app.updateStatus.total === 1}
        Reloading…
      {:else}
        Verifying & installing
        <span class="pct">{pct(app.updateStatus.downloaded, app.updateStatus.total)}</span>
      {/if}
    </span>
  </div>
{:else if app.updateStatus.kind === 'up-to-date'}
  <div class="bar bar-installing" role="status">
    <span class="msg">You're on the latest version.</span>
    <button class="dismiss" onclick={dismiss} aria-label="Dismiss">×</button>
  </div>
{:else if app.updateStatus.kind === 'error'}
  <div class="bar bar-error" role="alert">
    <span class="msg">Update failed: {app.updateStatus.message}</span>
    <button class="dismiss" onclick={dismiss} aria-label="Dismiss">×</button>
  </div>
{/if}

<style>
  .bar {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.5rem 1rem;
    font-size: 0.875rem;
    border-bottom: 1px solid rgba(0, 0, 0, 0.08);
  }
  .bar-available { background: #fff8e1; color: #5a4400; }
  .bar-installing { background: #e8f4ff; color: #1a4373; }
  .bar-error { background: #ffeaea; color: #8a1313; }
  .msg { flex: 1; }
  .pct { font-variant-numeric: tabular-nums; margin-left: 0.5em; }
  button {
    background: transparent;
    border: 1px solid currentColor;
    color: inherit;
    padding: 0.25rem 0.75rem;
    border-radius: 4px;
    cursor: pointer;
    font-size: inherit;
  }
  button.install { background: rgba(0, 0, 0, 0.04); }
  button.dismiss { padding: 0.125rem 0.5rem; border: none; font-size: 1.1em; }
  button:hover { background: rgba(0, 0, 0, 0.06); }
</style>
