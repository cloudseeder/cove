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
  import AddHubPanel from './AddHubPanel.svelte';
  import AuthPanel from './AuthPanel.svelte';
  import OnboardingPanel from './OnboardingPanel.svelte';
  import ThreadView from './ThreadView.svelte';
  import UpdateBar from './UpdateBar.svelte';

  const app = new AppState();

  /** v0.4.0: explicit "I want to onboard" toggle. AuthPanel offers
   *  the link; clicking it surfaces OnboardingPanel until the user
   *  either finishes (authenticated → ThreadView) or backs out. */
  let onboarding = $state(false);
  function startOnboarding() { onboarding = true; }
  function leaveOnboarding() { onboarding = false; }

  onMount(() => {
    // Fire-and-forget silent check. v0.4.79: default is silent so
    // routine no-update outcomes don't flash a 'You're on the latest
    // version' banner on every launch — only an actual available
    // update surfaces in UpdateBar. Sidebar footer's "Check for
    // updates" button passes silent: false when the user wants
    // confirmation their click did something.
    void app.checkForUpdate();
  });
</script>

<UpdateBar {app} />

{#if app.authStatus.kind === 'authenticated'}
  <ThreadView {app} />
  <!-- v0.4.69: add-hub modal overlay. Triggered by the sidebar
       switcher's "+ Add another hub" button; renders on top of
       ThreadView so the current-hub context stays visible behind
       it. -->
  {#if app.addHubOpen}
    <AddHubPanel {app} />
  {/if}
{:else if onboarding}
  <OnboardingPanel {app} onBack={leaveOnboarding} />
{:else}
  <AuthPanel {app} onOnboard={startOnboarding} />
{/if}
