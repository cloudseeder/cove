<script lang="ts">
  let { children } = $props();
</script>

<div class="app">
  {@render children?.()}
</div>

<style>
  /* v0.4.82: dark-first with light-mode variant via prefers-color-scheme.
     Values align with love.cove.oap.dev's landing palette so the app,
     specs, and marketing site feel like one product across system
     appearance switches. `color-scheme: light dark` tells the browser
     both are supported so form controls, scrollbars, and the browser's
     own chrome match the current system preference. */
  :global(:root) {
    /* Default: dark (matches original app appearance). */
    --bg: #0a0a0a;
    --fg: #f5f5f5;
    --muted: #8a8a8a;
    --panel: #141414;
    --border: rgba(255, 255, 255, 0.08);
    /* Row / button hover neutral. Dark: light overlay; light: dark
       overlay. Components should use `background: var(--hover)`
       rather than hardcoding rgba(255,255,255,X). */
    --hover: rgba(255, 255, 255, 0.04);
    color-scheme: light dark;
  }
  @media (prefers-color-scheme: light) {
    :global(:root) {
      /* Light: warm cream + brown, same palette as the landing site. */
      --bg: #fbf7ee;
      --fg: #2b2620;
      --muted: #6f6a60;
      --panel: #ffffff;
      --border: #e6ddc9;
      --hover: rgba(0, 0, 0, 0.05);
    }
  }
  :global(body) {
    margin: 0;
    background: var(--bg);
    color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .app {
    min-height: 100vh;
  }
</style>
