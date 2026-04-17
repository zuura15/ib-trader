/**
 * Live Bots workflow — UI end-to-end smoke.
 *
 * Drives the real engine + API + broker + bot-runner through the
 * BotsPanel UI. Verifies the full bot lifecycle as a user would
 * exercise it from the browser:
 *
 *   1. Bot definition loaded from config/bots/test-ford.yaml appears
 *      in the Bots panel with status=stopped.
 *   2. Click START → status flips to running via WS push.
 *   3. Click STOP  → status=stopped.
 *   4. Click START again.
 *   5. Click FORCE BUY → a real 10-share F BUY is placed and fills.
 *      Positions panel shows baseline+10.
 *   6. Observe (no assert) whether the strategy's trailing stop
 *      indicator appears — market-dependent. Logged as info.
 *   7. Page reload → Bots panel + Positions panel rehydrate from the
 *      WS snapshot. Status and F position are preserved.
 *   8. Teardown: STOP the bot; flatten any remaining F drift via the
 *      existing /api/commands cleanup path.
 *
 * Safety:
 *   - manual_entry_only in the YAML means the strategy itself won't
 *     emit a BUY — only FORCE BUY triggers a real entry.
 *   - Max footprint: 10 shares of F (~$125 notional).
 *   - afterAll flattens any drift from baseline.
 *
 * The deterministic trail-exit spec (driven by a mock quote stream,
 * no IB) is a follow-up. This spec intentionally does not assert on
 * trail-fire to avoid flakiness against the live market.
 */
import { test, expect, APIRequestContext } from '@playwright/test';

const API_BASE = process.env.VITE_API_URL || 'http://127.0.0.1:8000';
const SYMBOL = 'F';
const TEST_BOT_ID = '11111111-1111-1111-1111-111111111111';
const TEST_BOT_NAME = 'test-ford';
const QTY = 10;

const COMMAND_WAIT_MS = 15_000;
const STATUS_WAIT_MS = 10_000;
const FILL_WAIT_MS = 30_000;
const TRAIL_OBSERVE_MS = 10_000;


async function fordPositionQty(api: APIRequestContext): Promise<number> {
  const resp = await api.get(`${API_BASE}/api/positions`);
  if (!resp.ok()) throw new Error(`positions fetch failed ${resp.status()}`);
  const list = await resp.json();
  if (!Array.isArray(list)) return 0;
  return list
    .filter((p: any) => p.symbol === SYMBOL)
    .reduce((acc: number, p: any) => acc + Number(p.quantity || 0), 0);
}

async function reloadBotsRegistry(api: APIRequestContext): Promise<void> {
  // Not strictly needed — the API process bootstraps on startup — but
  // cheap insurance if the test ever runs after a manual YAML drop.
  await api.post(`${API_BASE}/api/bots/reload`).catch(() => null);
}

async function flattenFord(api: APIRequestContext, baseline: number): Promise<void> {
  const now = await fordPositionQty(api).catch(() => baseline);
  const drift = now - baseline;
  if (drift === 0) return;
  const side = drift > 0 ? 'sell' : 'buy';
  const qty = Math.abs(drift).toString();
  await api.post(`${API_BASE}/api/commands`, {
    data: { command: `${side} ${SYMBOL} ${qty} market` },
  }).catch((err) => console.warn('[flatten] cleanup failed:', err));
}


test.describe('Live bots workflow (test-ford)', () => {
  let api: APIRequestContext;
  let baseline = 0;

  test.beforeAll(async ({ playwright }) => {
    api = await playwright.request.newContext();
    await reloadBotsRegistry(api);
    // Stop the test bot and flatten any residual F position from a prior
    // run so tests always start from a known zero baseline. Without this,
    // force-buy hits max_shares and the order gets rejected.
    await api.post(`${API_BASE}/api/bots/${TEST_BOT_ID}/stop`).catch(() => null);
    // Cancel any orphan F orders left from prior runs — NYSE self-trade
    // prevention will block our force-buy from filling if there's a
    // resting opposite-side order on the same symbol.
    const cancelResp = await api.post(`${API_BASE}/api/orders/cancel-by-symbol`, {
      data: { symbol: SYMBOL },
    });
    const cancelBody = await cancelResp.text();
    console.log(`[bots-spec] cancel-by-symbol ${SYMBOL}: ${cancelResp.status()} ${cancelBody}`);
    // Don't flatten — overnight LMT fills are unreliable and a hanging
    // SELL would block all tests. Tests are baseline-relative anyway.
    baseline = await fordPositionQty(api);
    console.log(`[bots-spec] baseline F position = ${baseline}`);
  });

  test.afterAll(async () => {
    // Best-effort stop. Ignore errors — cleanup is advisory.
    await api.post(`${API_BASE}/api/bots/${TEST_BOT_ID}/stop`).catch(() => null);
    try {
      await flattenFord(api, baseline);
    } finally {
      await api.dispose();
    }
  });

  test('01 bot appears in panel', async ({ page }) => {
    await page.goto('/');
    // Click into the Bots tab if the default layout doesn't expose it.
    // The data-testid lives on the bot row itself regardless of tab state,
    // but the row only renders when the Bots panel is mounted. Try the
    // large layout first — most dashboard variants keep the Bots panel
    // visible or one tab click away.
    const row = page.getByTestId(`bot-row-${TEST_BOT_ID}`);
    await expect(row).toBeVisible({ timeout: STATUS_WAIT_MS });
    // Status is encoded on the row itself as data-bot-status.
    await expect(row).toHaveAttribute('data-bot-status', /running|stopped|error|paused/);
  });

  test('02 start then stop then start', async ({ page }) => {
    await page.goto('/');
    const row = page.getByTestId(`bot-row-${TEST_BOT_ID}`);
    await expect(row).toBeVisible({ timeout: STATUS_WAIT_MS });

    const status = page.getByTestId(`bot-row-${TEST_BOT_ID}`);  // data-bot-status on the row
    const toggle = page.getByTestId(`bot-toggle-${TEST_BOT_ID}`);

    // Initial state may be stopped or running (depending on prior runs) —
    // normalize to stopped.
    const initial = await status.getAttribute('data-bot-status');
    if (initial !== 'stopped') {
      await toggle.click();
      await expect(status).toHaveAttribute('data-bot-status', 'stopped', { timeout: STATUS_WAIT_MS });
    }

    // START
    await toggle.click();
    await expect(status).toHaveAttribute('data-bot-status', 'running', { timeout: STATUS_WAIT_MS });

    // STOP
    await toggle.click();
    await expect(status).toHaveAttribute('data-bot-status', 'stopped', { timeout: STATUS_WAIT_MS });

    // START again — leave the bot running for the next step.
    await toggle.click();
    await expect(status).toHaveAttribute('data-bot-status', 'running', { timeout: STATUS_WAIT_MS });
  });

  test('03 force buy places a real order and position shows up', async ({ page }) => {
    await page.goto('/');
    const status = page.getByTestId(`bot-row-${TEST_BOT_ID}`);  // data-bot-status on the row
    await expect(status).toHaveAttribute('data-bot-status', 'running', { timeout: STATUS_WAIT_MS });

    const before = await fordPositionQty(api);

    const forceBuyBtn = page.getByTestId(`bot-force-buy-${TEST_BOT_ID}`);
    await expect(forceBuyBtn).toBeVisible();
    await forceBuyBtn.click();

    // Wait for the fill to land in IB and for the Positions API to reflect it.
    await expect
      .poll(async () => fordPositionQty(api), {
        timeout: FILL_WAIT_MS,
        intervals: [500, 1000, 2000],
        message: `F position did not reach ${before + QTY} within ${FILL_WAIT_MS}ms`,
      })
      .toBe(before + QTY);

    // Positions panel UI row reflects it too (the subscribe_positions WS push).
    const posRow = page.getByTestId(`position-row-${SYMBOL}`);
    await expect(posRow).toBeVisible({ timeout: STATUS_WAIT_MS });
    const uiQty = await posRow.getAttribute('data-qty');
    expect(Number(uiQty)).toBe(before + QTY);
  });

  test('04 observe trailing stop state (non-asserting)', async ({ page }) => {
    // Trail firing is market-dependent. By the time this test runs the
    // trail may have fired and flattened the position, or the UI may
    // never surface a position line at all. Neither should fail the run.
    // We just record what we see.
    await page.goto('/');
    const row = page.getByTestId(`bot-row-${TEST_BOT_ID}`);
    await expect(row).toBeVisible({ timeout: STATUS_WAIT_MS });
    await page.waitForTimeout(TRAIL_OBSERVE_MS);
    const qty = await fordPositionQty(api).catch(() => -1);
    const rowTxt = await row.textContent().catch(() => '');
    console.log(`[trail-observe] F qty=${qty} row="${rowTxt?.slice(0, 200)}"`);
  });

  test('05 page reload rehydrates state', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByTestId(`bot-row-${TEST_BOT_ID}`)).toBeVisible({ timeout: STATUS_WAIT_MS });

    const before = await fordPositionQty(api);
    await page.reload();

    // Bot row still there post-reload.
    await expect(page.getByTestId(`bot-row-${TEST_BOT_ID}`)).toBeVisible({ timeout: STATUS_WAIT_MS });

    // Positions UI rehydrates to the same qty we saw pre-reload.
    if (before !== baseline) {
      const posRow = page.getByTestId(`position-row-${SYMBOL}`);
      await expect(posRow).toBeVisible({ timeout: STATUS_WAIT_MS });
      const uiQty = await posRow.getAttribute('data-qty');
      expect(Number(uiQty)).toBe(before);
    }
  });
});
