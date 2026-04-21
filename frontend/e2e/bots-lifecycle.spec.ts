/**
 * Bots lifecycle — start, force-buy, force-sell with position assertions.
 *
 * Complements bots.spec.ts. That file pins the button wiring; this one
 * pins the end-to-end lifecycle where the position line appears on
 * force-buy and disappears on force-sell, driven by bot_state WS
 * messages that mirror the real backend's state-doc broadcasts.
 *
 * Mock fixture (mock-api.mjs):
 *   - bot-alpha  — STOPPED, symbol F.   Exercises START → running.
 *   - bot-bravo  — RUNNING, symbol QQQ. Exercises FORCE BUY / FORCE SELL.
 *
 * The mock jumps through ENTRY_ORDER_PLACED / EXIT_ORDER_PLACED straight
 * to the terminal state so tests don't have to deal with the 3-4 s
 * entry-order wait. Real-backend glitches (e.g. "0 shares" flash during
 * ENTRY_ORDER_PLACED) are covered by unit-level guards in the component;
 * here we verify the steady-state rendering of an actual position.
 */
import { test, expect } from '@playwright/test';

const ALPHA = 'bot-alpha';
const BRAVO = 'bot-bravo';
const BRAVO_SYMBOL = 'QQQ';
const WS_SETTLE_MS = 15_000;

// Run serially — all three tests mutate the same mock bot fixture
// (shared with bots.spec.ts) so parallel execution trips the
// check-then-expect races the mock reset is meant to absorb.
test.describe.configure({ mode: 'serial' });

test.describe('Bots lifecycle', () => {
  test.beforeEach(async ({ page, request }) => {
    await request.post('http://localhost:5198/api/_test/reset');
    await page.goto('/');
    // Wait for initial WS snapshot — assert on both rows so we don't
    // race an early-arriving snapshot against the reset broadcast.
    await expect(
      page.locator(`[data-testid="bot-row-${ALPHA}"]`),
    ).toHaveAttribute('data-bot-status', 'stopped', { timeout: WS_SETTLE_MS });
    await expect(
      page.locator(`[data-testid="bot-row-${BRAVO}"]`),
    ).toHaveAttribute('data-bot-status', 'running', { timeout: WS_SETTLE_MS });
  });

  test('START flips a stopped bot to running; no position line while flat', async ({ page }) => {
    const row = page.locator(`[data-testid="bot-row-${ALPHA}"]`);
    const toggle = page.locator(`[data-testid="bot-toggle-${ALPHA}"]`);

    // No position line for a bot that has no position (real backend:
    // AWAITING_ENTRY_TRIGGER → PositionLine returns null).
    await expect(
      page.locator(`[data-testid="position-line-${ALPHA}"]`),
    ).toHaveCount(0);

    await expect(toggle).toHaveText('START');
    await toggle.click();

    await expect(row).toHaveAttribute('data-bot-status', 'running', { timeout: 5000 });
    await expect(toggle).toHaveText('STOP', { timeout: 5000 });

    // Still no position line — starting the bot doesn't open a position.
    await expect(
      page.locator(`[data-testid="position-line-${ALPHA}"]`),
    ).toHaveCount(0);
  });

  test('FORCE BUY opens a position; FORCE SELL closes it; position line reflects both', async ({ page, request }) => {
    const row = page.locator(`[data-testid="bot-row-${BRAVO}"]`);
    const positionLine = page.locator(`[data-testid="position-line-${BRAVO}"]`);
    const forceBuy = page.locator(`[data-testid="bot-force-buy-${BRAVO}"]`);
    const forceSell = page.locator(`[data-testid="bot-force-sell-${BRAVO}"]`);

    // Starting state: bravo is running but flat → no position line, no
    // force-sell button.
    await expect(row).toHaveAttribute('data-bot-status', 'running');
    await expect(positionLine).toHaveCount(0);
    await expect(forceSell).toHaveCount(0);
    await expect(forceBuy).toBeVisible();

    // ── FORCE BUY ──
    const buyHitsBefore = await request
      .get('http://localhost:5198/api/_test/force-buy-hits')
      .then((r) => r.json());

    await forceBuy.click();

    // Mock confirms the POST landed.
    await expect
      .poll(async () => {
        const r = await request.get('http://localhost:5198/api/_test/force-buy-hits');
        return (await r.json()).count;
      }, { timeout: 5000 })
      .toBe(buyHitsBefore.count + 1);

    // Position line should appear with qty=15, entry=$180.25 after the
    // bot_state WS push. Asserting on data attributes isolates the test
    // from CSS / layout changes.
    await expect(positionLine).toBeVisible({ timeout: 5000 });
    await expect(positionLine).toHaveAttribute('data-symbol', BRAVO_SYMBOL);
    await expect(positionLine).toHaveAttribute('data-qty', '15');
    await expect(positionLine).toHaveAttribute('data-entry', '180.25');
    await expect(positionLine).toHaveAttribute('data-position-state', 'AWAITING_EXIT_TRIGGER');

    // FORCE SELL button should now be visible (only renders when holding).
    await expect(forceSell).toBeVisible({ timeout: 5000 });

    // Headline text sanity-check — "+15 @ $180.25" under the symbol badge.
    await expect(positionLine).toContainText('QQQ');
    await expect(positionLine).toContainText('+15');
    await expect(positionLine).toContainText('$180.25');

    // ── FORCE SELL ──
    const sellHitsBefore = await request
      .get('http://localhost:5198/api/_test/force-sell-hits')
      .then((r) => r.json());

    await forceSell.click();

    await expect
      .poll(async () => {
        const r = await request.get('http://localhost:5198/api/_test/force-sell-hits');
        return (await r.json()).count;
      }, { timeout: 5000 })
      .toBe(sellHitsBefore.count + 1);

    // Position cleared → PositionLine unmounts, FORCE SELL button hides.
    await expect(positionLine).toHaveCount(0, { timeout: 5000 });
    await expect(forceSell).toHaveCount(0, { timeout: 5000 });
    // FORCE BUY still visible — bot is still running, just flat again.
    await expect(forceBuy).toBeVisible();
  });

  test('full lifecycle: START alpha, FORCE BUY, FORCE SELL — position tracks each step', async ({ page, request }) => {
    const alphaPosition = page.locator(`[data-testid="position-line-${ALPHA}"]`);
    const alphaToggle = page.locator(`[data-testid="bot-toggle-${ALPHA}"]`);
    const alphaForceBuy = page.locator(`[data-testid="bot-force-buy-${ALPHA}"]`);
    const alphaForceSell = page.locator(`[data-testid="bot-force-sell-${ALPHA}"]`);

    // Step 1: alpha stopped → START.
    await expect(alphaPosition).toHaveCount(0);
    await alphaToggle.click();
    await expect(
      page.locator(`[data-testid="bot-row-${ALPHA}"]`),
    ).toHaveAttribute('data-bot-status', 'running', { timeout: 5000 });
    // Still flat after start.
    await expect(alphaPosition).toHaveCount(0);

    // Step 2: FORCE BUY → position appears.
    await alphaForceBuy.click();
    await expect(alphaPosition).toBeVisible({ timeout: 5000 });
    await expect(alphaPosition).toHaveAttribute('data-qty', '15');
    await expect(alphaPosition).toHaveAttribute('data-symbol', 'F');

    // Step 3: FORCE SELL → position gone.
    await expect(alphaForceSell).toBeVisible();
    await alphaForceSell.click();
    await expect(alphaPosition).toHaveCount(0, { timeout: 5000 });

    // Mock recorded one hit on each path.
    const buy = await request
      .get('http://localhost:5198/api/_test/force-buy-hits')
      .then((r) => r.json());
    const sell = await request
      .get('http://localhost:5198/api/_test/force-sell-hits')
      .then((r) => r.json());
    expect(buy.count).toBe(1);
    expect(buy.last_bot_id).toBe(ALPHA);
    expect(sell.count).toBe(1);
    expect(sell.last_bot_id).toBe(ALPHA);
  });
});
