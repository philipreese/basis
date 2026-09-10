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
}

function loadWorker(): Harness {
  const handlers = new Map<string, Handler>();
  const cache = new Map<string, Response>();
  const fetchMock = vi.fn(async (req: Request | string) => {
    const url = typeof req === 'string' ? req : req.url;
    return new Response(`network:${url}`, { status: 200 });
  });

  const keyOf = (req: Request | string) => (typeof req === 'string' ? req : req.url);
  const cacheApi = {
    async match(req: Request | string) {
      return cache.get(keyOf(req));
    },
    async put(req: Request | string, res: Response) {
      cache.set(keyOf(req), res);
    },
    async add(req: string) {
      cache.set(new URL(req, 'https://basis.test').toString(), new Response(`precached:${req}`));
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
  return { handlers, cache, fetchMock };
}

function fireFetch(h: Harness, url: string, init: RequestInit = {}) {
  const request = new Request(url, init);
  let responded: Promise<Response> | null = null;
  const event = {
    request,
    respondWith: (p: Promise<Response>) => {
      responded = p;
    },
  };
  h.handlers.get('fetch')!(event);
  return { responded: responded as Promise<Response> | null, request };
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

  describe('hashed assets are cache-first', () => {
    it('serves from network on a miss and stores it', async () => {
      const { responded } = fireFetch(h, 'https://basis.test/assets/index-abc123.js');
      await responded;
      expect(h.fetchMock).toHaveBeenCalledTimes(1);
      expect(h.cache.has('https://basis.test/assets/index-abc123.js')).toBe(true);
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

    it('falls back to cache when the network fails', async () => {
      h.cache.set('https://basis.test/', new Response('cached shell'));
      h.fetchMock.mockRejectedValueOnce(new Error('offline'));
      const { responded } = fireFetch(h, 'https://basis.test/');
      expect(await (await responded!).text()).toBe('cached shell');
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
