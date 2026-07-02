import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// Tauri convention — see https://v2.tauri.app/start/frontend/sveltekit/
const host = process.env.TAURI_DEV_HOST;

// v0.4.47: inject package.json's version at build time so the PWA
// (which has no Tauri getVersion() to call) can render it. Tauri
// desktop still calls @tauri-apps/api/app::getVersion at runtime,
// which is authoritative for the installed bundle; this is only a
// fallback for browser mode.
const pkg = JSON.parse(
  readFileSync(fileURLToPath(new URL('./package.json', import.meta.url)), 'utf-8'),
);

export default defineConfig({
  plugins: [sveltekit()],
  define: {
    '__COVE_PACKAGE_VERSION__': JSON.stringify(pkg.version),
  },

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
