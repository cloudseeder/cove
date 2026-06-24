import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';

// Tauri convention — see https://v2.tauri.app/start/frontend/sveltekit/
const host = process.env.TAURI_DEV_HOST;

export default defineConfig({
  plugins: [sveltekit()],

  // Prevent vite from clobbering the Rust compiler's stdout/stderr.
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host
      ? { protocol: 'ws', host, port: 1421 }
      : undefined,
    watch: { ignored: ['**/src-tauri/**'] }
  },

  test: {
    include: ['src/**/*.test.ts'],
    environment: 'node'
  }
});
