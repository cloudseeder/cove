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

export function isTauri(): boolean {
  return (
    typeof window !== 'undefined'
    && Object.prototype.hasOwnProperty.call(window, '__TAURI_INTERNALS__')
  );
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
