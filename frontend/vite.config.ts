/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Dev: the browser talks only to the Vite origin and calls relative /api/... URLs; the dev
// server forwards them, so the UI never calls a provider directly and no CORS is needed.
// Prod: FastAPI serves dist/ (and the reverse proxy forwards /api/vouchers/).
//
// The proxy targets come from frontend/.env (see .env.example). They have no VITE_ prefix, so
// they stay on the dev server and are never compiled into the browser bundle.
const PROXY_KEYS = ["DASHBOARD_BACKEND_URL", "ADHYAAPAK_BACKEND_URL"] as const;

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const missing = PROXY_KEYS.filter((k) => !env[k]);
  if (command === "serve" && mode !== "test" && missing.length)
    throw new Error(`Set ${missing.join(" and ")} in frontend/.env (copy frontend/.env.example).`);
  return {
    plugins: [react()],
    server: {
      port: 5173,
      // Order matters: the more specific /api/v1 (this dashboard) must come before /api
      // (the AdhyaapakAI backend, whose CORS only allows the app's own origins).
      proxy: {
        "/api/v1": { target: env.DASHBOARD_BACKEND_URL, changeOrigin: false },
        // Target from ADHYAAPAK_BACKEND_URL: live https://api.adhyaapak.ai (local: http://127.0.0.1:8001).
        // secure: verify the live server's TLS certificate.
        "/api": { target: env.ADHYAAPAK_BACKEND_URL, changeOrigin: true, secure: true },
      },
    },
    build: { sourcemap: false, chunkSizeWarningLimit: 900 },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
      css: false,
      restoreMocks: true,
      // Tests use a fixed API path (all requests are mocked) instead of the local .env.
      env: { VITE_ADHYAAPAK_API_BASE: "/api" },
      coverage: {
        provider: "v8",
        include: ["src/**/*.{ts,tsx}"],
        exclude: ["src/**/*.test.{ts,tsx}", "src/test/**", "src/main.tsx", "src/vite-env.d.ts"],
        reporter: ["text-summary", "text", "html"],
        reportsDirectory: "../reports/frontend-coverage",
      },
    },
  };
});
