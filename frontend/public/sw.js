/* basis service worker (#1023) — offline SHELL only, never data.
 *
 * The console is a live control surface. A cached HALT state, a cached Live
 * Gate row, or a cached reconciliation verdict is not a degraded experience,
 * it is a WRONG one — an operator deciding from stale numbers is the failure
 * this whole project's supervision rules exist to prevent. So:
 *
 *   /api/*   never touched. Not cached, not served from cache, no offline
 *            fallback. Offline, a fetch fails and the console shows its own
 *            error state, which is true. Silence would be a lie.
 *   /assets/* cache-first. Vite content-hashes these, so a URL's bytes never
 *            change and a hit is always correct.
 *   shell    network-first, cache fallback. Online you always get the
 *            deploy that is actually live; offline you get the last shell
 *            seen, whose asset hashes are in the cache beside it.
 *
 * SECURE CONTEXT: this only registers over HTTPS or localhost. Reached over
 * plain http on a tailnet hostname, registration silently does not happen and
 * the console is a normal web page — same constraint as the clipboard API in
 * #1011.
 */

const VERSION = 'basis-v1';
const SHELL = '/index.html';
// Enough to open cold and offline. Everything else arrives via runtime
// caching, so a missing entry here degrades gracefully instead of failing
// the install (one bad URL rejects addAll and the worker never activates).
const PRECACHE = [SHELL, '/manifest.webmanifest', '/favicon.svg', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(VERSION);
      await Promise.allSettled(PRECACHE.map((url) => cache.add(url)));
      // Take over promptly: a control surface must not keep serving an old
      // shell because a tab somewhere is still open.
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((n) => n !== VERSION).map((n) => caches.delete(n)));
      await self.clients.claim();
    })(),
  );
});

function isApi(url) {
  return url.pathname === '/api' || url.pathname.startsWith('/api/');
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Data: hands off entirely. Not respondWith'd at all, so the browser does
  // exactly what it would with no service worker installed.
  if (isApi(url)) return;

  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(cacheFirst(request));
    return;
  }

  event.respondWith(networkFirst(request));
});

async function cacheFirst(request) {
  const cache = await caches.open(VERSION);
  const hit = await cache.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  if (response.ok) cache.put(request, response.clone());
  return response;
}

async function networkFirst(request) {
  const cache = await caches.open(VERSION);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch (err) {
    const hit = await cache.match(request);
    if (hit) return hit;
    // A client-side route offline: the shell resolves it once it boots.
    if (request.mode === 'navigate') {
      const shell = await cache.match(SHELL);
      if (shell) return shell;
    }
    throw err;
  }
}
