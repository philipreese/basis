import { configDefaults, defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";

// Overridable so the Playwright smoke pack can point the built frontend at
// its own fresh-DB backend (playwright.config.ts) instead of the dev server.
// The backend binds IPv4 by construction — pixi.toml's `server` task passes
// `--host 127.0.0.1` — while Node 17+ resolves localhost to ::1 first, costing
// ~2 s in autoSelectFamily timeout per request (measured 2026-09-07).
const apiTarget = process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
    plugins: [svelte(), tailwindcss()],
    // Vitest runs under Node, whose default package.json "exports"
    // resolution picks Svelte's server build — component tests then hit
    // "mount(...) is not available on the server". Forcing the browser
    // condition under vitest is the documented fix (svelte.dev testing docs).
    resolve: process.env.VITEST ? { conditions: ["browser"] } : undefined,
    server: {
        // Pin IPv4: Node 24 can bind localhost to ::1 only, refusing IPv4
        // clients and the tailnet proxy (measured 2026-09-07 01:25 ET).
        host: "127.0.0.1",
        port: 5173,
        // An extra hostname the dev server may be reached through (e.g. a
        // tailnet HTTPS proxy so RESUME works away from the desk). The value
        // is machine-local — never commit it; the server binds 127.0.0.1
        // for IPv4 clients, and host-checking stays on for everything else.
        allowedHosts: process.env.VITE_EXTRA_ALLOWED_HOST ? [process.env.VITE_EXTRA_ALLOWED_HOST] : [],
        proxy: {
            "/api": {
                target: apiTarget,
                changeOrigin: true,
            },
        },
    },
    test: {
        environment: "jsdom",
        globals: true,
        setupFiles: ["./src/tests/setup.ts"],
        // Playwright specs are not vitest tests
        exclude: [...configDefaults.exclude, "e2e/**"],
    },
});
