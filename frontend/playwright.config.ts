/**
 * Playwright smoke pack (#83) — runs against the REAL stack: FastAPI on a
 * fresh temp database (scripts/e2e_backend.py) + the built frontend served
 * by `vite preview` (which inherits the /api proxy from server.proxy).
 *
 * Deliberately small: only flows where breakage is dangerous. The status
 * strip is the only place RESUME exists (ADR-0008), so its functioning is
 * an enforced check here, not prose.
 */
import { defineConfig } from '@playwright/test';

const BACKEND_PORT = 8630;
const FRONTEND_PORT = 4183;

export default defineConfig({
  testDir: './e2e',
  // Tests share one backend database; state must never interleave.
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 30_000,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    // 127.0.0.1 everywhere, never localhost: `vite preview` inherits
    // server.host from vite.config.ts, so the preview server binds IPv4 only
    // and a localhost URL depends on Node's happy-eyeballs fallback from a
    // refused ::1 — the same resolver coin-flip #971 fixed on the dev path.
    baseURL: `http://127.0.0.1:${FRONTEND_PORT}`,
    trace: 'retain-on-failure',
  },
  webServer: [
    {
      command: 'pixi run e2e-backend',
      cwd: '..',
      url: `http://127.0.0.1:${BACKEND_PORT}/openapi.json`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `npm run build && npm run preview -- --port ${FRONTEND_PORT} --strictPort`,
      url: `http://127.0.0.1:${FRONTEND_PORT}`,
      env: { VITE_API_PROXY_TARGET: `http://127.0.0.1:${BACKEND_PORT}` },
      reuseExistingServer: false,
      timeout: 180_000,
    },
  ],
});
