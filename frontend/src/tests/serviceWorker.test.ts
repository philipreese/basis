/// <reference types="vitest/globals" />
import { describe, it, expect, beforeEach, vi } from 'vitest';
// The REAL worker, read through Vite's ?raw rather than node:fs — keeps this
// test free of @types/node while still exercising the shipped file.
import swSource from '../../public/sw.js?raw';

/**
 * #1023: the service worker's routing, exercised against the REAL
 * public/sw.js rather than a copy — a worker whose behaviour drifted from
 * its test would be invisible, since nothing else reads this file.
 *
 * The invariant worth this harness is the /api one. A cached HALT state or
 * Live Gate row is not a degraded experience, it is a wrong one, and an
 * operator deciding from stale numbers is the exact failure supervision.md
 * exists to prevent. "It didn't cache" is unobservable from the outside, so
 * it gets asserted here.
 */

type Handler = (event: unknown) => void;

interface Harness {
  handlers: Map<string, Handler>;
  cache: Map<string, Response>;
  fetchMock: ReturnType<typeof vi.fn>;
  /** Promises the worker asked the browser to keep it alive for. */
  kept: Promise<unknown>[];
}

function loadWorker(): Harness {
  const handlers = new Map<string, Handler>();
  const cache = new Map<string, Response>();
  const fetchMock = vi.fn(async (req: Request | string) => {
    const url = typeof req === 'string' ? req : req.url;
    return new Response(`network:${url}`, { status: 200 });
  });

  // The real Cache API resolves a relative URL against the worker scope, so
  // cache.match('/index.html') finds what cache.add('/index.html') stored.
  // Without this the harness misses on exactly the lookup install does.
  const keyOf = (req: Request | string) =>
    typeof req === 'string' ? new URL(req, 'https://basis.test').toString() : req.url;
  const cacheApi = {
    async match(req: Request | string) {
      return cache.get(keyOf(req));
    },
    // Async on purpose (#1030): the real Cache.put resolves on a later
    // tick, so a worker that neither awaits it nor wraps it in
    // event.waitUntil can be terminated before the write lands. The first
    // version of this harness used a synchronous Map.set, which made a
    // fire-and-forget put look like it always worked -- and it shipped a
    // service worker that dropped the 258 kB entry bundle in the field.
    async put(req: Request | string, res: Response) {
      await Promise.resolve();
      cache.set(keyOf(req), res);
    },
    async add(req: string) {
      // The shell has to come back as real HTML: install parses it for the
      // /assets/ URLs it references (#1030), so a marker string would make
      // that discovery silently find nothing.
      const body =
        req === '/index.html'
          ? '<!doctype html><script type="module" src="/assets/index-abc123.js"></script>' +
            '<link rel="stylesheet" href="/assets/index-abc123.css">'
          : `precached:${req}`;
      cache.set(keyOf(req), new Response(body));
    },
  };
  const scope = {
    location: { origin: 'https://basis.test' },
    addEventListener: (type: string, fn: Handler) => handlers.set(type, fn),
    skipWaiting: vi.fn(async () => undefined),
    clients: { claim: vi.fn(async () => undefined) },
    caches: {
      open: async () => cacheApi,
      keys: async () => ['basis-v1', 'basis-v0'],
      delete: vi.fn(async () => true),
    },
    fetch: fetchMock,
    URL,
    Response,
    Promise,
    console,
  };

  // Classic workers run against a global `self`; give them one.
  new Function('self', 'caches', 'fetch', 'URL', 'Response', swSource)(
    scope,
    scope.caches,
    fetchMock,
    URL,
    Response,
  );
  return { handlers, cache, fetchMock, kept: [] };
}

function fireFetch(h: Harness, url: string, init: RequestInit = {}) {
  const request = new Request(url, init);
  let responded: Promise<Response> | null = null;
  const event = {
    request,
    respondWith: (p: Promise<Response>) => {
      responded = p;
    },
    waitUntil: (p: Promise<unknown>) => {
      h.kept.push(p);
    },
  };
  h.handlers.get('fetch')!(event);
  return { responded: responded as Promise<Response> | null, request };
}

/** Everything the worker asked to be kept alive for, as the browser would. */
async function settleKeptWork(h: Harness) {
  await Promise.allSettled(h.kept);
}

describe('basis service worker', () => {
  let h: Harness;
  beforeEach(() => {
    h = loadWorker();
  });

  describe('API requests are never intercepted', () => {
    it.each(['/api/portfolio/overview', '/api/trading-control', '/api'])(
      'leaves %s entirely to the browser',
      (path) => {
        const { responded } = fireFetch(h, `https://basis.test${path}`);
        // Not respondWith'd at all — the browser behaves as if no worker
        // were installed, so nothing can serve a stale control state.
        expect(responded).toBeNull();
      },
    );

    it('never writes an API response into the cache', async () => {
      fireFetch(h, 'https://basis.test/api/portfolio/overview');
      expect([...h.cache.keys()].filter((k) => k.includes('/api/'))).toEqual([]);
    });
  });

  describe('install precaches the shell AND the bundle it needs', () => {
    async function runInstall() {
      let kept: Promise<unknown> | null = null;
      h.handlers.get('install')!({ waitUntil: (p: Promise<unknown>) => (kept = p) });
      await kept;
    }

    it('caches the shell', async () => {
      await runInstall();
      expect(h.cache.has('https://basis.test/index.html')).toBe(true);
    });

    it("caches the shell's OWN script, discovered from its markup", async () => {
      // The bug this exists for: runtime caching alone never guaranteed the
      // entry bundle was fetched through the worker, so offline the shell
      // rendered to a blank page while its script failed. Discovery from the
      // shell makes the precache complete by construction -- no build-time
      // manifest to keep in sync and forget.
      await runInstall();
      expect(h.cache.has('https://basis.test/assets/index-abc123.js')).toBe(true);
    });

    it("caches the shell's stylesheet too", async () => {
      await runInstall();
      expect(h.cache.has('https://basis.test/assets/index-abc123.css')).toBe(true);
    });
  });

  describe('hashed assets are cache-first', () => {
    it('serves from network on a miss and stores it, keeping the worker alive to finish', async () => {
      const { responded } = fireFetch(h, 'https://basis.test/assets/index-abc123.js');
      await responded;
      expect(h.fetchMock).toHaveBeenCalledTimes(1);
      // The write must be registered with waitUntil (#1030). Without it the
      // browser may kill the worker before the body is stored -- which is
      // how the entry bundle went missing offline while the smaller
      // stylesheet survived.
      expect(h.kept.length).toBeGreaterThan(0);
      await settleKeptWork(h);
      expect(h.cache.has('https://basis.test/assets/index-abc123.js')).toBe(true);
    });

    it('registers the write with waitUntil rather than firing and forgetting', async () => {
      const { responded } = fireFetch(h, 'https://basis.test/assets/index-abc123.js');
      await responded;
      // Asserted BEFORE settling: a fire-and-forget put leaves nothing here,
      // and the cache entry would then depend on the worker outliving the
      // response by luck.
      expect(h.kept).toHaveLength(1);
    });

    it('serves from cache on a hit without touching the network', async () => {
      h.cache.set('https://basis.test/assets/index-abc123.js', new Response('cached'));
      const { responded } = fireFetch(h, 'https://basis.test/assets/index-abc123.js');
      expect(await (await responded!).text()).toBe('cached');
      expect(h.fetchMock).not.toHaveBeenCalled();
    });
  });

  describe('the shell is network-first', () => {
    it('prefers the network so a deploy is picked up', async () => {
      h.cache.set('https://basis.test/', new Response('stale shell'));
      const { responded } = fireFetch(h, 'https://basis.test/');
      expect(await (await responded!).text()).toContain('network:');
    });

    it('keeps the worker alive for the shell write too', async () => {
      const { responded } = fireFetch(h, 'https://basis.test/');
      await responded;
      expect(h.kept).toHaveLength(1);
    });

    it('falls back to cache when the network fails', async () => {
      h.cache.set('https://basis.test/', new Response('cached shell'));
      h.fetchMock.mockRejectedValueOnce(new Error('offline'));
      const { responded } = fireFetch(h, 'https://basis.test/');
      expect(await (await responded!).text()).toBe('cached shell');
    });

    it('does not wait out a hung network when it has a cached shell', async () => {
      // The bug this exists for (#1030 round 2): offline, fetch() does not
      // fail -- it STALLS on connection timeout, 30-90s on a phone in
      // airplane mode. Network-first meant waiting all of it to reach a file
      // already on disk, on the surface you open to halt trading.
      vi.useFakeTimers();
      try {
        h.cache.set('https://basis.test/', new Response('cached shell'));
        h.fetchMock.mockImplementationOnce(() => new Promise(() => {})); // never settles
        const { responded } = fireFetch(h, 'https://basis.test/');
        await vi.advanceTimersByTimeAsync(3000);
        expect(await (await responded!).text()).toBe('cached shell');
      } finally {
        vi.useRealTimers();
      }
    });

    it('still waits indefinitely when there is nothing cached to serve', async () => {
      // Nothing to gain by giving up early: a timeout here would turn a slow
      // first load into a failed one.
      vi.useFakeTimers();
      try {
        let release: (r: Response) => void = () => {};
        h.fetchMock.mockImplementationOnce(() => new Promise((res) => (release = res)));
        const { responded } = fireFetch(h, 'https://basis.test/');
        await vi.advanceTimersByTimeAsync(10_000);
        release(new Response('late but fine', { status: 200 }));
        expect(await (await responded!).text()).toBe('late but fine');
      } finally {
        vi.useRealTimers();
      }
    });
  });

  describe('scope', () => {
    it('ignores non-GET requests, so a HALT POST is never replayed', () => {
      const { responded } = fireFetch(h, 'https://basis.test/api/trading-control', { method: 'POST' });
      expect(responded).toBeNull();
    });

    it('ignores cross-origin requests', () => {
      const { responded } = fireFetch(h, 'https://example.com/thing.js');
      expect(responded).toBeNull();
    });
  });
});
