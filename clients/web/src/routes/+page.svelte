<!--
  Main entry — switches between AuthPanel and ThreadView based on auth
  state. One AppState instance lives at the top so the Client + entries
  + status are shared across the panels.
-->
<script lang="ts">
  import { AppState } from '$lib/cove/state.svelte';
  import AuthPanel from './AuthPanel.svelte';
  import ThreadView from './ThreadView.svelte';

  const app = new AppState();
</script>

{#if app.authStatus.kind === 'authenticated'}
  <ThreadView {app} />
{:else}
  <AuthPanel {app} />
{/if}
