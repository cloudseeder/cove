<!--
  Main entry — switches between AuthPanel and ThreadView based on auth
  state. One AppState instance lives at the top so the Client + entries
  + status are shared across the panels.

  Slice 4b adds a quiet update check on mount; the UpdateBar renders
  above whichever panel is showing whenever there's something to say.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { AppState } from '$lib/cove/state.svelte';
  import AuthPanel from './AuthPanel.svelte';
  import ThreadView from './ThreadView.svelte';
  import UpdateBar from './UpdateBar.svelte';

  const app = new AppState();

  onMount(() => {
    // Fire-and-forget; checkForUpdate no-ops outside Tauri and never
    // throws on routine outcomes (no network, no update). Real
    // failures land in app.updateStatus and surface via UpdateBar.
    // (AuthPanel handles refreshKeychain on its own mount.)
    void app.checkForUpdate();
  });
</script>

<UpdateBar {app} />

{#if app.authStatus.kind === 'authenticated'}
  <ThreadView {app} />
{:else}
  <AuthPanel {app} />
{/if}
