<!--
  Compose box — sticky at the bottom of the thread. Ctrl/Cmd+Enter to
  send. No optimistic insert: the entry only appears in the feed when
  it comes back through the /stream subscription, verified, with proof.
  That's the ceremony — 'sent' isn't the same as 'verified-and-included'.
-->
<script lang="ts">
  import type { AppState } from '$lib/cove/state.svelte';

  interface Props {
    app: AppState;
  }
  let { app }: Props = $props();

  let draft = $state('');
  let sending = $state(false);
  let error = $state<string | null>(null);

  async function send() {
    const body = draft.trim();
    if (!body || sending) return;
    sending = true;
    error = null;
    try {
      await app.post(body);
      draft = '';
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
</script>

<form class="compose" onsubmit={(e) => { e.preventDefault(); void send(); }}>
  <textarea
    bind:value={draft}
    onkeydown={onKey}
    placeholder="Write something. ⌘⏎ to send."
    rows="2"
    disabled={sending}
  ></textarea>
  <button type="submit" disabled={sending || draft.trim() === ''}>
    {sending ? '…' : 'Send'}
  </button>
  {#if error}
    <span class="error">{error}</span>
  {/if}
</form>

<style>
  .compose {
    position: sticky;
    bottom: 1rem;
    display: flex;
    align-items: flex-end;
    gap: 0.6rem;
    padding: 0.6rem;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    backdrop-filter: blur(10px);
  }
  textarea {
    flex: 1;
    background: transparent;
    color: var(--fg);
    border: none;
    resize: none;
    font: inherit;
    padding: 0.45rem 0.5rem;
    min-height: 2.4rem;
    max-height: 12rem;
  }
  textarea:focus {
    outline: none;
  }
  button {
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
  button:hover:not(:disabled) {
    transform: translateY(-1px);
  }
  button:disabled {
    background: var(--border);
    color: var(--muted);
    cursor: not-allowed;
  }
  .error {
    color: #fca5a5;
    font-size: 0.85rem;
    flex-basis: 100%;
  }
</style>
