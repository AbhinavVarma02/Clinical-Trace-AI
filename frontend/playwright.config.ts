import { existsSync } from "node:fs";
import { defineConfig, devices } from "@playwright/test";

// Every invocation owns a fresh backend/database. Never attach to a user's server.
const port = Number(process.env.E2E_PORT ?? 8765);
const localPython = process.platform === "win32" ? "../venv/Scripts/python.exe" : "../venv/bin/python";
const python = process.env.E2E_PYTHON ?? (existsSync(localPython) ? localPython : "python");
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    colorScheme: "light",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "tablet", use: { ...devices["iPad (gen 7)"], defaultBrowserType: "chromium" } },
    { name: "mobile", use: { ...devices["iPhone 13"], defaultBrowserType: "chromium" } },
  ],
  webServer: {
    command: `"${python}" e2e/server.py ${port}`,
    url: `http://127.0.0.1:${port}/api/v2/system/status`,
    reuseExistingServer: false,
    timeout: 90_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 5000 },
  },
});
