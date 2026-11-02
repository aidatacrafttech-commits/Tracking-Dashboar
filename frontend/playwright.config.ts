import { defineConfig } from "@playwright/test";

const port = Number(process.env.E2E_PORT ?? 8187);
const python = process.platform === "win32" ? "../backend/venv/Scripts/python.exe" : "../backend/venv/bin/python";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    channel: "chrome", // the locally installed Google Chrome; no browser download
    trace: "off",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    { name: "tablet", use: { viewport: { width: 834, height: 1112 }, hasTouch: true } },
    { name: "mobile", use: { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true } },
  ],
  webServer: {
    // Real backend + built UI on a throwaway fixture database; polling disabled.
    command: `"${python}" e2e/serve_e2e.py`,
    url: `http://127.0.0.1:${port}/api/v1/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: { E2E_PORT: String(port) },
  },
});
