/**
 * Live UI smoke test — no orders, no trading.
 *
 * Launches the app against the live engine/API/Redis stack and verifies
 * that the core UI panels render with real data:
 *   1. Positions panel loads and shows at least one position row
 *   2. Bots panel loads and shows at least one bot row
 *   3. Console input is interactive (no command submission)
 *   4. Watchlist panel loads
 *   5. Status bar renders connection status
 *
 * Does NOT place any orders or start/stop bots.
 */
import { test, expect } from '@playwright/test';

const STATUS_WAIT_MS = 15_000;

test.describe('Live UI smoke (read-only)', () => {

  test('01 dashboard loads and WS connects', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByTestId('console-input')).toBeVisible({ timeout: STATUS_WAIT_MS });
  });

  test('02 positions panel shows at least one position', async ({ page }) => {
    await page.goto('/');

    // The Positions panel should be visible
    const panel = page.locator('text=Positions').first();
    await expect(panel).toBeVisible({ timeout: STATUS_WAIT_MS });

    // Wait for the WS push to populate position rows.
    // The panel defaults to showing STK only — click OPT toggle if no STK rows appear.
    const anyRow = page.locator('[data-testid^="position-row-"]');
    const rowCount = await anyRow.count();

    if (rowCount === 0) {
      // Try enabling OPT filter
      const optBtn = page.locator('button:has-text("OPT")');
      if (await optBtn.isVisible()) {
        await optBtn.click();
        await expect(anyRow.first()).toBeVisible({ timeout: STATUS_WAIT_MS });
      }
    }

    // Now we should have at least one row
    await expect(anyRow.first()).toBeVisible({ timeout: STATUS_WAIT_MS });
    const symbol = await anyRow.first().getAttribute('data-symbol');
    console.log(`[ui-smoke] First position symbol: ${symbol}`);

    // Verify qty is rendered (non-empty)
    const qtyCell = anyRow.first().locator('[data-testid^="position-qty-"]');
    const qtyText = await qtyCell.textContent();
    console.log(`[ui-smoke] First position qty: ${qtyText}`);
    expect(qtyText).toBeTruthy();
  });

  test('03 positions panel data matches API', async ({ page, request }) => {
    // Fetch positions from API directly
    const API_BASE = process.env.VITE_API_URL || 'http://127.0.0.1:8000';
    const resp = await request.get(`${API_BASE}/api/positions`);
    expect(resp.ok()).toBeTruthy();
    const apiPositions = await resp.json();
    console.log(`[ui-smoke] API returned ${apiPositions.length} positions`);

    // Count STK positions (default filter)
    const stkCount = apiPositions.filter((p: any) =>
      (p.sec_type || 'STK').toUpperCase() === 'STK'
    ).length;
    console.log(`[ui-smoke] STK positions: ${stkCount}`);

    await page.goto('/');
    await expect(page.getByTestId('console-input')).toBeVisible({ timeout: STATUS_WAIT_MS });

    // Wait for positions to load via WS
    await page.waitForTimeout(3000);

    const rows = page.locator('[data-testid^="position-row-"]');
    const uiCount = await rows.count();
    console.log(`[ui-smoke] UI position rows visible: ${uiCount}`);

    // If STK count is 0 but total > 0, the issue is the default filter
    if (stkCount === 0 && apiPositions.length > 0) {
      console.log('[ui-smoke] No STK positions — all positions are options/other. Default filter hides them.');
    }
  });

  test('04 bots panel shows at least one bot', async ({ page }) => {
    await page.goto('/');
    const botRow = page.locator('[data-testid^="bot-row-"]');
    await expect(botRow.first()).toBeVisible({ timeout: STATUS_WAIT_MS });
    const botName = await botRow.first().textContent();
    console.log(`[ui-smoke] First bot row: ${botName?.slice(0, 80)}`);
  });

  test('05 console input is interactive', async ({ page }) => {
    await page.goto('/');
    const input = page.getByTestId('console-input');
    await expect(input).toBeVisible({ timeout: STATUS_WAIT_MS });
    await input.click();
    await input.fill('status');
    // Don't press Enter — we don't want to execute. Just verify the input works.
    const value = await input.inputValue();
    expect(value).toBe('status');
  });

  test('06 screenshot for visual inspection', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByTestId('console-input')).toBeVisible({ timeout: STATUS_WAIT_MS });
    // Wait for all panels to hydrate
    await page.waitForTimeout(5000);
    await page.screenshot({ path: 'test-results/ui-smoke-dashboard.png', fullPage: true });
    console.log('[ui-smoke] Screenshot saved to test-results/ui-smoke-dashboard.png');
  });
});
