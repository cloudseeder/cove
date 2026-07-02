// See https://kit.svelte.dev/docs/types#app
declare global {
  namespace App {}

  // v0.4.47: injected by Vite from clients/web/package.json at build
  // time (see the define block in vite.config.ts). Consumed by
  // tauri.ts::appVersion() as the PWA fallback for Tauri's runtime
  // getVersion().
  const __COVE_PACKAGE_VERSION__: string;
}

export {};
