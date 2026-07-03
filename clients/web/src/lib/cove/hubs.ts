/**
 * Multi-hub persistence helpers (v0.4.69 — federation UI, Phase 2).
 *
 * localStorage keys:
 *   cove.hubs           — JSON array of hub URLs the user has ever added
 *   cove.activeHubUrl   — string; last-active hub
 *   cove.thread.<url>   — per-hub last-viewed thread
 *
 * Legacy keys migrated on boot:
 *   cove.hubUrl         → first element of cove.hubs + cove.activeHubUrl
 *   cove.thread         → cove.thread.<the-legacy-hub-url>
 *
 * All helpers are safe to call in SSR / node contexts (they no-op when
 * localStorage is undefined). We prefer no-op over throwing because the
 * caller shouldn't have to guard every read.
 */

const KEY_HUBS = 'cove.hubs';
const KEY_ACTIVE_HUB = 'cove.activeHubUrl';
const KEY_THREAD_PREFIX = 'cove.thread.';
const LEGACY_KEY_HUB_URL = 'cove.hubUrl';
const LEGACY_KEY_THREAD = 'cove.thread';

function hasLS(): boolean {
  return typeof localStorage !== 'undefined';
}

/** Load the persisted hub-url list. Returns [] if the key is missing,
 *  empty, or malformed. */
export function loadHubUrls(): string[] {
  if (!hasLS()) return [];
  const raw = localStorage.getItem(KEY_HUBS);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((x): x is string => typeof x === 'string' && x.length > 0);
  } catch {
    return [];
  }
}

/** Persist the hub-url list. Deduped in insertion order. */
export function saveHubUrls(urls: readonly string[]): void {
  if (!hasLS()) return;
  const deduped: string[] = [];
  const seen = new Set<string>();
  for (const url of urls) {
    if (typeof url !== 'string' || url.length === 0) continue;
    if (seen.has(url)) continue;
    seen.add(url);
    deduped.push(url);
  }
  localStorage.setItem(KEY_HUBS, JSON.stringify(deduped));
}

/** Load the last-active hub URL. Null if unset. */
export function loadActiveHubUrl(): string | null {
  if (!hasLS()) return null;
  const raw = localStorage.getItem(KEY_ACTIVE_HUB);
  return raw && raw.length > 0 ? raw : null;
}

/** Persist the last-active hub URL. Pass null to clear. */
export function saveActiveHubUrl(url: string | null): void {
  if (!hasLS()) return;
  if (url === null) localStorage.removeItem(KEY_ACTIVE_HUB);
  else localStorage.setItem(KEY_ACTIVE_HUB, url);
}

/** Load the last-viewed thread for a specific hub. Null if unset. */
export function loadThreadFor(hubUrl: string): string | null {
  if (!hasLS() || !hubUrl) return null;
  const raw = localStorage.getItem(KEY_THREAD_PREFIX + hubUrl);
  return raw && raw.length > 0 ? raw : null;
}

/** Persist the last-viewed thread for a specific hub. Pass null to clear. */
export function saveThreadFor(hubUrl: string, thread: string | null): void {
  if (!hasLS() || !hubUrl) return;
  const key = KEY_THREAD_PREFIX + hubUrl;
  if (thread === null || thread.length === 0) localStorage.removeItem(key);
  else localStorage.setItem(key, thread);
}

/** Remove a hub URL from the persisted list + drop its per-hub thread
 *  memory. Does NOT touch activeHubUrl (caller decides fallback). */
export function removeHubUrl(hubUrl: string): void {
  if (!hasLS() || !hubUrl) return;
  const remaining = loadHubUrls().filter((u) => u !== hubUrl);
  saveHubUrls(remaining);
  localStorage.removeItem(KEY_THREAD_PREFIX + hubUrl);
}

/**
 * Migrate legacy single-hub storage keys to the multi-hub shape.
 * Called ONCE at AppState boot before any hub is restored.
 *
 * If `cove.hubUrl` is present but `cove.hubs` is not:
 *   - cove.hubs = [<legacy hub url>]
 *   - cove.activeHubUrl = <legacy hub url>
 *   - if cove.thread present → cove.thread.<legacy url> = <legacy thread>
 *   - both legacy keys removed.
 *
 * No-op if the multi-hub keys are already populated (i.e., we've booted
 * once since the migration). Idempotent — safe to call on every boot.
 */
export function migrateLegacyKeys(): void {
  if (!hasLS()) return;
  const legacyHubUrl = localStorage.getItem(LEGACY_KEY_HUB_URL);
  if (!legacyHubUrl) return;
  // If cove.hubs is already populated, we've migrated before — just
  // clean up the legacy key.
  if (localStorage.getItem(KEY_HUBS)) {
    localStorage.removeItem(LEGACY_KEY_HUB_URL);
    localStorage.removeItem(LEGACY_KEY_THREAD);
    return;
  }
  saveHubUrls([legacyHubUrl]);
  saveActiveHubUrl(legacyHubUrl);
  const legacyThread = localStorage.getItem(LEGACY_KEY_THREAD);
  if (legacyThread) {
    saveThreadFor(legacyHubUrl, legacyThread);
  }
  localStorage.removeItem(LEGACY_KEY_HUB_URL);
  localStorage.removeItem(LEGACY_KEY_THREAD);
}

/**
 * Compute a display label for a hub URL. Falls back to the hostname
 * (which is what the sidebar switcher renders). If the URL is unparseable
 * (shouldn't happen in practice), returns the URL verbatim.
 */
export function hubLabel(hubUrl: string): string {
  try {
    return new URL(hubUrl).host;
  } catch {
    return hubUrl;
  }
}
