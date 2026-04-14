import { defineConfig } from '@playwright/test';

// Live-stack config.
//
// Assumes Redis + ib-engine + ib-api are already running (the orchestrator
// script scripts/e2e-live.sh ensures this before handing off). We only need
// Playwright to manage the Vite dev server and point it at the real API on
// http://127.0.0.1:8000.
//
// Run via `make e2e-live` (preferred — handles startup / teardown) or
// manually with:
//   IB_TRADER_E2E_KEEP_RUNNING=1 scripts/e2e-live.sh
// to iterate without spinning services up each time.

// The live stack's API / WS endpoints are reached through Vite's dev-server
// proxy (see vite.config.ts). We intentionally don't set VITE_API_URL /
// VITE_WS_URL — the frontend uses its default `/api` and `/ws`, which the
// proxy forwards to ib-api on :8000. Mirrors the `make dev` configuration
// so the test exercises the same code path a developer does.
const VITE_PORT = Number(process.env.VITE_PORT || 5199);

// Vite's dev server runs over HTTPS (vite-plugin-basic-ssl — required for
// Web Speech API on LAN). Playwright speaks HTTPS and skips cert validation
// since it's a self-signed dev cert.
const BASE_URL = `https://localhost:${VITE_PORT}`;

export default defineConfig({
  testDir: './e2e',
  // Only the tests explicitly tagged for the live stack.
  testMatch: /.*\.live\.spec\.ts$/,
  // Live-broker workflows are slow (IB fill latency + WS roundtrip). Give
  // each test a generous ceiling; the spec itself sets per-step waits.
  timeout: 180_000,
  retries: 0,
  fullyParallel: false,      // orders placed against a real account — never in parallel
  workers: 1,
  use: {
    baseURL: BASE_URL,
    headless: true,
    ignoreHTTPSErrors: true,  // dev cert from vite-plugin-basic-ssl
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `VITE_DATA_MODE=live npm run dev -- --port ${VITE_PORT}`,
    url: BASE_URL,                    // https probe, not a plain TCP port check
    ignoreHTTPSErrors: true,
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
