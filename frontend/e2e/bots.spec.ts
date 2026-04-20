/**
 * Bots panel — start and force-buy button smoke tests.
 *
 * Runs against the mock API (see mock-api.mjs). The mock mutates
 * bot status on lifecycle POSTs and pushes WebSocket diffs on the
 * `bots` channel; the tests assert on both the HTTP round-trip and
 * the rendered status attribute after the diff lands.
 *
 * Two fixture bots:
 *   - bot-alpha  — starts STOPPED. Exercises the START path.
 *   - bot-bravo  — starts RUNNING. Exercises FORCE BUY (button only
 *                  renders while running).
 *
 * The default layout (variant A, "Classic") renders BotsPanel in its
 * compact table form. The button text there is "FORCE" (not "FORCE
 * BUY") — we assert on the test-id, not the label, to stay robust
 * across large/compact variants.
 */
import { test, expect } from '@playwright/test';

const ALPHA = 'bot-alpha';
const BRAVO = 'bot-bravo';
const WS_SETTLE_MS = 15_000;   // WS snapshot after connect on cold start

test.describe('Bots panel', () => {
  test.beforeEach(async ({ page, request }) => {
    // Reset shared mock fixture so tests are independent of order.
    await request.post('http://localhost:5198/api/_test/reset');
    await page.goto('/');
    // Wait for the initial WS snapshot to populate the bots store.
    // Asserting on both rows with their expected initial status guards
    // against the WS snapshot arriving before the test-reset broadcast.
    await expect(
      page.locator(`[data-testid="bot-row-${ALPHA}"]`),
    ).toHaveAttribute('data-bot-status', 'stopped', { timeout: WS_SETTLE_MS });
    await expect(
      page.locator(`[data-testid="bot-row-${BRAVO}"]`),
    ).toHaveAttribute('data-bot-status', 'running', { timeout: WS_SETTLE_MS });
  });

  test('renders both fixture bots with correct initial status', async ({ page }) => {
    await expect(
      page.locator(`[data-testid="bot-row-${ALPHA}"]`),
    ).toHaveAttribute('data-bot-status', 'stopped');
    await expect(
      page.locator(`[data-testid="bot-row-${BRAVO}"]`),
    ).toHaveAttribute('data-bot-status', 'running');
  });

  test('START button flips a stopped bot to running via WS diff', async ({ page }) => {
    const row = page.locator(`[data-testid="bot-row-${ALPHA}"]`);
    const toggle = page.locator(`[data-testid="bot-toggle-${ALPHA}"]`);

    await expect(toggle).toHaveText('START');
    await expect(row).toHaveAttribute('data-bot-status', 'stopped');

    await toggle.click();

    // After the POST + WS diff, the row's data-bot-status should flip
    // to running and the toggle should read STOP.
    await expect(row).toHaveAttribute('data-bot-status', 'running', { timeout: 5000 });
    await expect(toggle).toHaveText('STOP', { timeout: 5000 });
  });

  test('FORCE BUY button only renders while running and fires the API', async ({ page, request }) => {
    // Alpha starts stopped — no FORCE BUY button.
    await expect(
      page.locator(`[data-testid="bot-force-buy-${ALPHA}"]`),
    ).toHaveCount(0);

    // Bravo starts running — FORCE BUY is visible.
    const forceBuy = page.locator(`[data-testid="bot-force-buy-${BRAVO}"]`);
    await expect(forceBuy).toBeVisible();

    // Record the mock's hit counter before the click so the assert is
    // resilient to other tests touching the shared mock process.
    const before = await request
      .get('http://localhost:5198/api/_test/force-buy-hits')
      .then((r) => r.json());

    await forceBuy.click();

    // Poll the mock until the POST lands. Mock resolves immediately so
    // this completes well under the 5s ceiling.
    await expect
      .poll(
        async () => {
          const r = await request.get('http://localhost:5198/api/_test/force-buy-hits');
          const body = await r.json();
          return body.count;
        },
        { timeout: 5000 },
      )
      .toBe(before.count + 1);

    const afterHits = await request
      .get('http://localhost:5198/api/_test/force-buy-hits')
      .then((r) => r.json());
    expect(afterHits.last_bot_id).toBe(BRAVO);
  });
});
