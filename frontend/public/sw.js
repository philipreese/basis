/* basis service worker (#1023, fixed #1030) — offline SHELL only, never data.
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
 *
 * #1030 — two bugs the first version shipped with, both found only by pulling
 * the plug on a real browser:
 *
 * 1. Runtime cache writes were fire-and-forget. `cache.put(...)` returns a
 *    promise; not awaiting it and not wrapping it in event.waitUntil lets the
 *    browser kill the worker as soon as the response is returned, and a large
 *    body loses that race where a small one wins. Observed exactly that: the
 *    46 kB stylesheet cached, the 258 kB entry bundle did not, and offline
 *    the shell rendered to a blank page while its own script 404ed.
 *
 * 2. Runtime caching alone cannot guarantee the shell's OWN bundle is
 *    present. The first controlled load is not the first load, so nothing
 *    promises the entry chunk was ever fetched through this worker. Now the
 *    install step reads the shell it just cached and precaches the
 *    /assets/ URLs the shell itself references — complete by construction,
 *    no build-time manifest to keep in sync.
 */

// v2 (#1030): bumping the name discards v1's incomplete cache, which holds a
// shell whose bundle is missing. Serving that forever would be worse than
// having no worker at all.
const VERSION = 'basis-v2';
const SHELL = '/index.html';
// Stable-named files. The hashed bundles are discovered from the shell below
// rather than listed, because listing them would mean editing this file on
// every build and forgetting once is a blank console.
// How long the shell waits on the network before serving the copy we already
// have (#1030 round 2). Offline, fetch() does not fail fast: a phone in
// airplane mode against a tailnet hostname stalls on connection timeout for
// 30-90 seconds, and network-first meant waiting all of it to reach a file
// already on disk. On the console -- the surface you open to HALT trading --
// that is the worst possible place to spend a minute.
//
// The trade: a deploy now takes one extra launch to appear, because a slow
// response serves the cached shell while the fresh one lands in the cache
// behind it. Worth it, and NOT a correctness risk: /api is never cached, so
// every number on screen is still live or visibly absent. Only the app
// skeleton can lag, and a cached shell's asset hashes are cached beside it,
// so it stays self-consistent.
const SHELL_NETWORK_TIMEOUT_MS = 2500;
const STATIC = [SHELL, '/manifest.webmanifest', '/favicon.svg', '/icon-192.png', '/icon-512.png'];

/** The /assets/ URLs the cached shell references — its own script and styles. */
async function shellAssetUrls(cache) {
  const cached = await cache.match(SHELL);
  if (!cached) return [];
  const html = await cached.text();
  const urls = new Set();
  for (const match of html.matchAll(/["'](\/assets\/[^"']+)["']/g)) urls.add(match[1]);
  return [...urls];
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(VERSION);
      // allSettled, not addAll: one bad URL must not reject the whole install
      // and leave no worker at all. A missing extra degrades; a missing
      // worker does not.
      await Promise.allSettled(STATIC.map((url) => cache.add(url)));
      const assets = await shellAssetUrls(cache);
      await Promise.allSettled(assets.map((url) => cache.add(url)));
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
    event.respondWith(cacheFirst(event));
    return;
  }

  event.respondWith(networkFirst(event));
});

/** Store a response without racing worker shutdown (#1030). */
function keepAlive(event, promise) {
  // Without this the worker may be terminated mid-write and the entry is
  // silently lost -- the exact failure that shipped in v1.
  event.waitUntil(promise);
  return promise;
}

async function cacheFirst(event) {
  const cache = await caches.open(VERSION);
  const hit = await cache.match(event.request);
  if (hit) return hit;
  const response = await fetch(event.request);
  if (response.ok) keepAlive(event, cache.put(event.request, response.clone()));
  return response;
}

/** Reject after *ms* so a dead network cannot hold the shell hostage. */
function timeout(ms) {
  return new Promise((_resolve, reject) => setTimeout(() => reject(new Error('sw-timeout')), ms));
}

async function networkFirst(event) {
  const cache = await caches.open(VERSION);
  const cached =
    (await cache.match(event.request)) ||
    // A client-side route offline: the shell resolves it once it boots.
    (event.request.mode === 'navigate' ? await cache.match(SHELL) : undefined);

  const network = fetch(event.request).then((response) => {
    if (response.ok) keepAlive(event, cache.put(event.request, response.clone()));
    return response;
  });

  if (!cached) {
    // Nothing to fall back to, so there is nothing to gain by giving up
    // early -- wait for whatever the network eventually says.
    return network;
  }

  // Let the network finish and refresh the cache even if we stop waiting on
  // it, so the next launch has the current deploy.
  keepAlive(event, network.catch(() => undefined));
  try {
    return await Promise.race([network, timeout(SHELL_NETWORK_TIMEOUT_MS)]);
  } catch {
    return cached;
  }
}
