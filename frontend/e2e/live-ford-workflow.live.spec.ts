/**
 * Live Ford console workflow — UI end-to-end smoke.
 *
 * Drives the real engine + API + broker through the frontend's
 * CommandConsole. Mirrors tests/smoke/test_console_ford_workflow.py but
 * validates the stack from the browser:
 *
 *   1. buy F 1 (market | ask)   → Positions panel shows baseline +1
 *   2. buy F 1 mid               → Positions panel shows baseline +2
 *   3. Pull mid trade serial from the Trades panel
 *   4. orders (builtin, cosmetic)
 *   5. close <serial> (market | bid)  → baseline +1
 *   6. sell F 1 (market | bid)   → back to baseline
 *
 * Session awareness:
 *   - Skips on weekend closure (Fri 8 PM – Sun 8 PM ET).
 *   - Market orders are only reliable during RTH. Outside RTH the helper
 *     substitutes in the aggressive limit that actually fills: ASK for
 *     a BUY, BID for a SELL / LONG close.
 *
 * Safety:
 *   - 1 share of F per order (≈ $12 exposure).
 *   - afterAll hook hits the API to flatten any F drift relative to the
 *     baseline we captured on connect.
 *
 * Launch:  make e2e-live
 */
import { test, expect, Page, APIRequestContext } from '@playwright/test';

const SYMBOL = 'F';
const QTY = 1;
const API_BASE = process.env.VITE_API_URL || 'http://127.0.0.1:8000';
const POSITION_WAIT_MS = 30_000;
const COMMAND_WAIT_MS = 45_000;

type Session = 'weekend' | 'session_break' | 'rth' | 'overnight' | 'extended';

function currentSession(): Session {
  // All times are evaluated in US/Eastern. Intl.DateTimeFormat respects DST.
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    weekday: 'short',
    hour: 'numeric',
    minute: 'numeric',
    hour12: false,
  });
  const parts = fmt.formatToParts(new Date());
  const get = (t: string) => parts.find((p) => p.type === t)?.value || '';
  const wd = get('weekday');            // 'Mon', 'Tue', ...
  const hour = Number(get('hour'));
  const min = Number(get('minute'));
  const mins = hour * 60 + min;
  const weekdayIdx = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].indexOf(wd);

  // Weekend closure: Fri 8pm ET → Sun 8pm ET
  if (weekdayIdx === 5 && mins >= 20 * 60) return 'weekend';
  if (weekdayIdx === 6) return 'weekend';
  if (weekdayIdx === 0 && mins < 20 * 60) return 'weekend';

  // 3:50 AM – 4:00 AM ET nightly session break
  if (mins >= 3 * 60 + 50 && mins < 4 * 60) return 'session_break';

  if (mins >= 9 * 60 + 30 && mins < 16 * 60) return 'rth';
  if (mins >= 16 * 60 && mins < 20 * 60) return 'extended';      // after-hours
  if (mins >= 4 * 60 && mins < 9 * 60 + 30) return 'extended';   // pre-market
  return 'overnight';
}

function fillStrategy(side: 'BUY' | 'SELL', userIntent: 'market'): string {
  // Mirror the _fill_strategy helper from the Python smoke: market works
  // cleanly only in RTH; outside RTH we need a limit that crosses the
  // spread (BUY at ASK, SELL at BID).
  if (userIntent !== 'market') return userIntent;
  if (currentSession() === 'rth') return 'market';
  return side === 'BUY' ? 'ask' : 'bid';
}

async function fordPositionQty(api: APIRequestContext): Promise<number> {
  const resp = await api.get(`${API_BASE}/api/positions`);
  if (!resp.ok()) {
    throw new Error(`GET /api/positions failed: ${resp.status()}`);
  }
  const list = await resp.json();
  if (!Array.isArray(list)) return 0;
  return list
    .filter((p: any) => p.symbol === SYMBOL)
    .reduce((acc: number, p: any) => acc + Number(p.quantity || 0), 0);
}

// Read the open F trade-group serials from the API. The Trades panel
// lives in a tab that isn't guaranteed to be mounted in the current
// layout, so we go to the authoritative source instead of the DOM.
async function openFordTradeSerials(api: APIRequestContext): Promise<number[]> {
  const resp = await api.get(`${API_BASE}/api/trades?status=OPEN`);
  if (!resp.ok()) return [];
  const list = await resp.json();
  if (!Array.isArray(list)) return [];
  return list
    .filter((t: any) => t.symbol === SYMBOL && t.status === 'OPEN')
    .map((t: any) => Number(t.serial_number))
    .filter((n) => Number.isFinite(n));
}

async function runCommand(page: Page, text: string): Promise<void> {
  const input = page.getByTestId('console-input');
  await input.click();
  await input.fill(text);
  await input.press('Enter');

  // The command row appears with data-command set — wait for it to reach a
  // terminal status. The engine's HTTP API responds before IB fills so
  // "success" here means the request completed, not that the order is
  // done. Position assertions verify the latter.
  const row = page.locator(
    `[data-testid="console-command"][data-command="${text}"]`,
  ).last();
  await expect(row).toHaveAttribute('data-status', /success|failure/, {
    timeout: COMMAND_WAIT_MS,
  });
  const status = await row.getAttribute('data-status');
  if (status !== 'success') {
    const output = await row.locator('[data-testid="console-output"]').innerText().catch(() => '');
    throw new Error(`Command "${text}" failed: ${output}`);
  }
}

async function expectFordQty(api: APIRequestContext, page: Page, target: number): Promise<void> {
  // Authoritative check: IB positions via the API. The UI push is a
  // secondary verification that the WS path also caught up.
  await expect
    .poll(async () => fordPositionQty(api), {
      timeout: POSITION_WAIT_MS,
      intervals: [500, 1000, 2000],
      message: `F position did not reach ${target} within ${POSITION_WAIT_MS}ms`,
    })
    .toBe(target);

  // The Positions panel row should reflect the same value; this catches
  // regressions in the subscribe_positions WS push.
  await expect
    .poll(
      async () => {
        const row = page.getByTestId(`position-row-${SYMBOL}`);
        if ((await row.count()) === 0) return target === 0 ? 0 : null;
        const val = await row.getAttribute('data-qty');
        return val === null ? null : Number(val);
      },
      { timeout: POSITION_WAIT_MS, intervals: [500, 1000, 2000] },
    )
    .toBe(target);
}

async function flattenFord(api: APIRequestContext, baseline: number): Promise<void> {
  const current = await fordPositionQty(api).catch(() => baseline);
  const drift = current - baseline;
  if (drift === 0) return;
  const side = drift > 0 ? 'SELL' : 'BUY';
  const qty = Math.abs(drift).toString();
  const orderType = fillStrategy(side, 'market');
  const resp = await api.post(`${API_BASE}/api/commands`, {
    data: {
      command: `${side.toLowerCase()} ${SYMBOL} ${qty} ${orderType}`,
    },
  });
  if (!resp.ok()) {
    // Last-ditch: log and continue; the user can investigate the stranded
    // position manually if this cleanup failed.
    console.error('[flatten] API rejected cleanup', resp.status(), await resp.text());
  }
}

test.describe('Ford live console workflow', () => {
  const session = currentSession();
  test.skip(session === 'weekend', 'IB weekend closure (Fri 8 PM – Sun 8 PM ET)');
  test.skip(session === 'session_break', 'IB nightly session break (3:50–4:00 AM ET)');

  let api: APIRequestContext;
  let baseline = 0;
  let midSerial: number | null = null;
  let marketSerial: number | null = null;

  test.beforeAll(async ({ playwright }) => {
    api = await playwright.request.newContext();
    baseline = await fordPositionQty(api);
  });

  test.afterAll(async () => {
    try {
      await flattenFord(api, baseline);
    } finally {
      await api.dispose();
    }
  });

  test('01 dashboard loads and WS connects', async ({ page }) => {
    await page.goto('/');
    // The Console panel is always mounted — waiting for its input
    // confirms the dashboard is interactive.
    await expect(page.getByTestId('console-input')).toBeVisible({ timeout: 15_000 });
  });

  test('02 buy F 1 market fills and position increments', async ({ page }) => {
    test.skip(session !== 'rth', `MKT orders only fill in RTH (current=${session})`);
    await page.goto('/');
    const before = await fordPositionQty(api);
    const serialsBefore = new Set(await openFordTradeSerials(api));

    await runCommand(page, `buy ${SYMBOL} ${QTY} market`);
    await expectFordQty(api, page, before + 1);

    // Record the new open F trade; the mid buy will add one more and we
    // need to tell them apart by serial. Use the API — the Trades panel
    // may not be mounted in the current layout.
    const serialsAfter = await openFordTradeSerials(api);
    const newSerials = serialsAfter.filter((s) => !serialsBefore.has(s));
    expect(newSerials.length).toBeGreaterThanOrEqual(1);
    marketSerial = Math.max(...newSerials);
  });

  test('03 buy F 1 mid fills and position increments', async ({ page }) => {
    await page.goto('/');
    const before = await fordPositionQty(api);
    const serialsBefore = new Set(await openFordTradeSerials(api));

    await runCommand(page, `buy ${SYMBOL} ${QTY} mid`);
    await expectFordQty(api, page, before + 1);

    const serialsAfter = await openFordTradeSerials(api);
    const newSerials = serialsAfter.filter((s) => !serialsBefore.has(s));
    expect(newSerials.length).toBeGreaterThanOrEqual(1);
    midSerial = Math.max(...newSerials);
  });

  test('04 orders builtin responds', async ({ page }) => {
    await page.goto('/');
    await runCommand(page, 'orders');
    // Cosmetic — any success output is fine; verify the command rendered.
    const out = page.locator(
      '[data-testid="console-command"][data-command="orders"] >> [data-testid="console-output"]',
    ).last();
    await expect(out).toBeVisible();
  });

  test('05 close mid serial decrements position', async ({ page }) => {
    test.skip(midSerial === null, 'mid serial was not captured in step 03');
    await page.goto('/');
    const before = await fordPositionQty(api);
    const strategy = fillStrategy('SELL', 'market');
    await runCommand(page, `close ${midSerial} ${strategy}`);
    await expectFordQty(api, page, before - 1);
  });

  test('06 sell F 1 market decrements position', async ({ page }) => {
    test.skip(session !== 'rth', `MKT orders only fill in RTH (current=${session})`);
    await page.goto('/');
    const before = await fordPositionQty(api);
    // Don't try to sell what we don't have. If nothing is open beyond
    // our pre-test baseline (e.g. step 03's mid buy was already closed
    // out by step 05), there's nothing meaningful to sell here — skip
    // rather than place an unwanted short.
    test.skip(before <= baseline, `no F to sell (qty=${before}, baseline=${baseline})`);
    await runCommand(page, `sell ${SYMBOL} ${QTY} market`);
    await expectFordQty(api, page, before - 1);
  });
});
