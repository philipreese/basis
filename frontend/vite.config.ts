import { configDefaults, defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";

// Overridable so the Playwright smoke pack can point the built frontend at
// its own fresh-DB backend (playwright.config.ts) instead of the dev server.
const apiTarget = process.env.VITE_API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
    plugins: [svelte(), tailwindcss()],
    // Vitest runs under Node, whose default package.json "exports"
    // resolution picks Svelte's server build — component tests then hit
    // "mount(...) is not available on the server". Forcing the browser
    // condition under vitest is the documented fix (svelte.dev testing docs).
    resolve: process.env.VITEST ? { conditions: ["browser"] } : undefined,
    server: {
        port: 5173,
        // An extra hostname the dev server may be reached through (e.g. a
        // tailnet HTTPS proxy so RESUME works away from the desk). The value
        // is machine-local — never commit it; the server binds localhost
        // either way, and host-checking stays on for everything else.
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
