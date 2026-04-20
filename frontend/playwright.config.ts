import { defineConfig } from '@playwright/test';

// Mock-stack config. Vite serves HTTPS via vite-plugin-basic-ssl
// (required for Web Speech API on LAN), so we speak HTTPS and skip
// cert validation — same treatment as playwright.live.config.ts.
export default defineConfig({
  testDir: './e2e',
  // Default run excludes *.live.spec.ts (they require the real stack).
  testIgnore: /.*\.live\.spec\.ts$/,
  timeout: 30000,
  retries: 0,
  use: {
    baseURL: 'https://localhost:5199',
    headless: true,
    ignoreHTTPSErrors: true,
    screenshot: 'only-on-failure',
  },
  webServer: [
    {
      // Mock API server
      command: 'node e2e/mock-api.mjs',
      port: 5198,
      reuseExistingServer: false,
    },
    {
      // Vite dev server pointing to mock API. Use url+ignoreHTTPSErrors
      // so playwright probes HTTPS rather than HTTP on the port check.
      command: 'VITE_DATA_MODE=live VITE_API_URL=http://localhost:5198/api npm run dev -- --port 5199',
      url: 'https://localhost:5199',
      ignoreHTTPSErrors: true,
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
