/**
 * Cove service worker — v0.4.29 (PWA shell).
 *
 * Strategy: app-shell precache + network-first for everything else.
 *
 * The app shell (index.html, manifest, icons) is cached on install so
 * the PWA launches offline on cold start. Every other request hits the
 * network first; cache is a fallback for the navigation request only.
 *
 * We intentionally do NOT cache API responses. Cove's /sync /threads
 * /inbox endpoints are the source of truth for verifiable state — a
 * stale cached response would confuse the client's high-water + dedup
 * machinery. Let the app's own offline behaviour (none, for now) be the
 * only "no internet" surface.
 *
 * Cache name is versioned by app version so a release wipes prior
 * shell caches via the activate handler. skipWaiting + clients.claim
 * make a new SW take over immediately rather than waiting for every
 * tab to close.
 */
// v0.4.56: this constant is REWRITTEN at build time to match the
// current package.json version via scripts/bump-sw-cache.js
// (registered as a `postbuild` script in package.json). Previously
// sat at 'cove-shell-v0.4.29' across 26 releases because nothing
// bumped it; the SW's activate handler only prunes caches whose
// name doesn't match the running one, so nothing ever fired. The
// value below is only the source-file default (for `pnpm dev`);
// production builds get the current version substituted in.
const CACHE = 'cove-shell-vDEV';
const SHELL = [
  '/',
  '/manifest.json',
  '/icons/256.png',
  '/icons/512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  // Only handle GET — POST/PUT/DELETE always hit network (no caching writes).
  if (req.method !== 'GET') return;

  // Navigation requests (the user hit reload or opened the PWA): try
  // the network so they get the latest shell; fall back to the cached
  // index so the PWA launches when offline. The cached index loads the
  // JS which then tries to /sync and fails gracefully if still offline.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match('/'))
    );
    return;
  }

  // Same-origin static assets (icons, manifest, JS/CSS chunks): stale-
  // while-revalidate so reloads are fast and content updates within a
  // session. Cross-origin requests (the hub API at cove.oap.dev) and
  // WebSocket upgrades pass straight through.
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  event.respondWith(
    caches.match(req).then((cached) => {
      const fetchPromise = fetch(req).then((resp) => {
        if (resp.ok && resp.type === 'basic') {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return resp;
      }).catch(() => cached);
      return cached || fetchPromise;
    })
  );
});
