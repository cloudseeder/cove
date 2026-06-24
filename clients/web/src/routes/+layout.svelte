<script lang="ts">
  let { children } = $props();
</script>

<div class="app">
  {@render children?.()}
</div>

<style>
  :global(:root) {
    --bg: #0a0a0a;
    --fg: #f5f5f5;
    --muted: #8a8a8a;
    --panel: #141414;
    --border: rgba(255, 255, 255, 0.08);
    color-scheme: dark;
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
