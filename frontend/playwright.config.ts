import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 30000,
  retries: 0,
  use: {
    baseURL: 'http://localhost:5199',
    headless: true,
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
      // Vite dev server pointing to mock API
      command: 'VITE_DATA_MODE=live VITE_API_URL=http://localhost:5198/api npm run dev -- --port 5199',
      port: 5199,
      reuseExistingServer: false,
    },
  ],
});
