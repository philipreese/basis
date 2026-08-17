import { configDefaults, defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";

// Overridable so the Playwright smoke pack can point the built frontend at
// its own fresh-DB backend (playwright.config.ts) instead of the dev server.
const apiTarget = process.env.VITE_API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
    plugins: [svelte(), tailwindcss()],
    server: {
        port: 5173,
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
        // Playwright specs are not vitest tests
        exclude: [...configDefaults.exclude, "e2e/**"],
    },
});
