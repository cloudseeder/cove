/**
 * Tauri bridge — detection + typed wrappers around the Rust commands.
 *
 * Importing this module is safe in a browser-only context; everything
 * is guarded behind `isTauri()`. The `@tauri-apps/api` package is
 * imported dynamically so a browser bundle without Tauri doesn't fail
 * to load.
 *
 * The webview's window object carries `__TAURI_INTERNALS__` when
 * served from a Tauri shell; absence means we're in a plain browser.
 */

export interface KeyStatus {
  /** True iff a paired (priv, pub) is in the OS keychain. */
  has_keys: boolean;
  /** Public key hex; null when has_keys is false. Private key NEVER
   *  exposed to JS post-import. */
  public_key: string | null;
}

/** Reads the bundle version. In Tauri, calls @tauri-apps/api/app's
 *  getVersion() which returns the .app/.dmg/.msi bundle version.
 *  In browser mode (PWA, dev server), falls back to the value Vite
 *  injected from clients/web/package.json at build time — see the
 *  `define` block in vite.config.ts. Resolved lazily so the import
 *  has no Tauri-side side-effects at module load. */
export async function appVersion(): Promise<string | null> {
  if (!isTauri()) {
    // v0.4.47: build-time constant injected by Vite. Falls back to
    // null in vitest (jsdom / node env) where the define didn't fire.
    return typeof __COVE_PACKAGE_VERSION__ !== 'undefined'
      ? __COVE_PACKAGE_VERSION__
      : null;
  }
  const { getVersion } = await import('@tauri-apps/api/app');
  return await getVersion();
}

export function isTauri(): boolean {
  return (
    typeof window !== 'undefined'
    && Object.prototype.hasOwnProperty.call(window, '__TAURI_INTERNALS__')
  );
}

/** v0.4.29: is the app running as an installed PWA? Two signals matter:
 *  the display-mode media query (set to `standalone` when launched from
 *  the home-screen icon on iOS / Android), and the iOS Safari-specific
 *  `navigator.standalone` flag (pre-display-mode-support). Both have
 *  the same UX consequence: there's no browser chrome, the user
 *  installed this, paste-each-launch friction needs to go down.
 *  Returns false outside the browser. */
export function isPWA(): boolean {
  if (typeof window === 'undefined') return false;
  if (isTauri()) return false;  // Tauri webview reports standalone too
  try {
    if (window.matchMedia?.('(display-mode: standalone)')?.matches) return true;
  } catch { /* matchMedia is fine to throw on legacy WebKit */ }
  // iOS Safari predates display-mode; check the legacy flag too.
  const nav = navigator as Navigator & { standalone?: boolean };
  return !!nav.standalone;
}

async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  if (!isTauri()) {
    throw new Error(`Tauri command '${cmd}' called outside Tauri context`);
  }
  const api = await import('@tauri-apps/api/core');
  return api.invoke<T>(cmd, args);
}

/**
 * Typed keychain commands. Every call here resolves to a Rust function
 * in src-tauri/src/commands.rs.
 */
export const keychain = {
  status(): Promise<KeyStatus> {
    return invoke<KeyStatus>('keys_status');
  },
  import(privateKey: string, publicKey: string): Promise<void> {
    return invoke<void>('keys_import', { privateKey, publicKey });
  },
  /**
   * v0.4.0: generate a fresh Ed25519 keypair on-device. Private key
   * stays in the OS keychain; the returned hex IS the public key the
   * UI hands to the pairing payload. Throws if a keypair is already
   * stored — caller must `clear()` first to deliberately rotate.
   */
  generate(): Promise<string> {
    return invoke<string>('keys_generate');
  },
  clear(): Promise<void> {
    return invoke<void>('keys_clear');
  },
  /**
   * Sign arbitrary bytes with the stored private key. Used by the
   * Tauri-backed Signer (see client.ts) for both the auth nonce
   * signature AND the canonical-content entry signature. Bytes go
   * in; hex string comes out.
   */
  signMessage(message: Uint8Array): Promise<string> {
    // The Tauri command takes Vec<u8>; we pass a number array so it
    // serializes cleanly across the boundary.
    return invoke<string>('sign_message', { message: Array.from(message) });
  },
};

/**
 * v0.4.0: root keychain slot (keymaster-only). Separate from `keychain`
 * so the surface for "is root present?" is distinct from "is member
 * present?". A device that's both keymaster and member sees two
 * independent states. The hub never holds root.priv (CLAUDE.md
 * non-negotiable #1); this is on the admin's device only.
 */
export const rootKeychain = {
  status(): Promise<KeyStatus> {
    return invoke<KeyStatus>('root_status');
  },
  import(privateKey: string, publicKey: string): Promise<void> {
    return invoke<void>('root_import', { privateKey, publicKey });
  },
  clear(): Promise<void> {
    return invoke<void>('root_clear');
  },
  /** Sign canonical-content bytes (Attestation or DirectoryManifest)
   *  with the root private key. Bytes in, hex string out. */
  signMessage(message: Uint8Array): Promise<string> {
    return invoke<string>('root_sign_message', { message: Array.from(message) });
  },
};

/**
 * Background subscription control. The Rust task survives webview close;
 * onPush is called with the raw JSON-stringified push payload exactly
 * as it came off the WebSocket — verification is the caller's job.
 * Returns a teardown function that both stops the Rust subscription AND
 * unregisters the event listener.
 */
export const stream = {
  async start(
    opts: { hubUrl: string; token: string; thread: string },
    onPush: (rawPayload: string) => void,
  ): Promise<() => Promise<void>> {
    if (!isTauri()) {
      throw new Error('stream.start() requires the Tauri shell');
    }
    // Subscribe to the entry_pushed event BEFORE we ask Rust to start
    // — the first push could land within microseconds of start().
    const { listen } = await import('@tauri-apps/api/event');
    const unlisten = await listen<string>('entry_pushed', (event) => {
      onPush(event.payload);
    });
    await invoke<void>('stream_start', {
      hubUrl: opts.hubUrl,
      token: opts.token,
      thread: opts.thread,
    });
    return async () => {
      unlisten();
      try {
        await invoke<void>('stream_stop');
      } catch {
        // already stopped; ignore
      }
    };
  },
};

/**
 * Request notification permission. macOS in particular only prompts on
 * first call. Calling this at first-auth time means the prompt fires
 * before any actual notification would, so the user grants permission
 * with context.
 */
export async function ensureNotificationPermission(): Promise<boolean> {
  if (!isTauri()) return false;
  const plugin = await import('@tauri-apps/plugin-notification');
  if (await plugin.isPermissionGranted()) return true;
  const result = await plugin.requestPermission();
  return result === 'granted';
}

/**
 * Auto-update — slice 4b.
 *
 * Trust posture: the updater plugin verifies every downloaded bundle
 * against the public key embedded in tauri.conf.json BEFORE it's
 * installed. That signature is independent of OS code-signing — so
 * even unsigned macOS/Windows builds get an authenticated update
 * channel. A tampered or unsigned bundle is refused by the plugin
 * with no UI involvement.
 *
 * UX intent: we own the prompt (tauri.conf.json sets `dialog: false`).
 * The default modal would shove a system-y dialog in front of the
 * verification ceremony, which fights the rest of the app's tone.
 * Our flow is: silent check on startup → quiet affordance in the
 * thread view → user explicitly opts in to install + restart.
 */
export interface AvailableUpdate {
  version: string;
  /** Release notes from latest.json; may be empty. */
  notes: string | null;
  /** ISO-8601 publish date if present in the feed. */
  date: string | null;
}

export const updater = {
  /**
   * Check the updater feed. Returns null when up-to-date, an
   * AvailableUpdate record otherwise. NEVER throws on "no update
   * available"; only throws on network/signature failure — those are
   * worth surfacing.
   */
  async check(): Promise<AvailableUpdate | null> {
    if (!isTauri()) return null;
    const plugin = await import('@tauri-apps/plugin-updater');
    const update = await plugin.check();
    if (update === null) return null;
    return {
      version: update.version,
      notes: update.body ?? null,
      date: update.date ?? null,
    };
  },
  /**
   * Download, verify (the plugin checks the signature against the
   * pubkey baked into tauri.conf.json), install, then restart. This
   * is one atomic operation from the user's perspective — once they
   * say yes, they're getting a new app.
   *
   * The intermediate progress callback is fired with bytes-downloaded
   * tallies; the UI can show a progress bar if desired. We keep it
   * simple for now.
   */
  async downloadAndInstallAndRestart(
    onProgress?: (downloaded: number, total: number | null) => void,
  ): Promise<void> {
    if (!isTauri()) {
      throw new Error('updater.downloadAndInstallAndRestart() requires the Tauri shell');
    }
    const plugin = await import('@tauri-apps/plugin-updater');
    const update = await plugin.check();
    if (update === null) {
      throw new Error('No update available');
    }
    let downloaded = 0;
    let total: number | null = null;
    await update.downloadAndInstall((event) => {
      if (event.event === 'Started') {
        total = event.data.contentLength ?? null;
      } else if (event.event === 'Progress') {
        downloaded += event.data.chunkLength;
        onProgress?.(downloaded, total);
      }
    });
    const process = await import('@tauri-apps/plugin-process');
    await process.relaunch();
  },
};
