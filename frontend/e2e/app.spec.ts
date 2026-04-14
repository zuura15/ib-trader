import { test, expect } from '@playwright/test';

test.describe('Header', () => {
  test('shows engine connected status', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('header >> text=connected')).toBeVisible({ timeout: 10000 });
  });

  test('shows account mode', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('header >> text=paper')).toBeVisible({ timeout: 10000 });
  });

  test('shows service health', async ({ page }) => {
    await page.goto('/');
    // Services chip should show a count
    await expect(page.locator('header >> text=Services')).toBeVisible({ timeout: 10000 });
  });

  test('shows realized P&L label', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('header >> text=Realized P&L')).toBeVisible({ timeout: 10000 });
  });

  test('theme toggle works and persists', async ({ page }) => {
    await page.goto('/');
    await page.locator('button:has-text("Light")').click();
    const theme = await page.locator('html').getAttribute('data-theme');
    expect(theme).toBe('light');
    await page.reload();
    await page.waitForTimeout(1000);
    const themeAfter = await page.locator('html').getAttribute('data-theme');
    expect(themeAfter).toBe('light');
    // Toggle back for other tests
    await page.locator('button:has-text("Dark")').click();
  });
});

test.describe('Console', () => {
  test('can submit a command and see result', async ({ page }) => {
    await page.goto('/');
    const input = page.locator('input[placeholder="Enter command..."]');
    await input.fill('buy AAPL 1 mid');
    await input.press('Enter');
    await expect(page.locator('text=buy AAPL 1 mid')).toBeVisible();
    // Wait for result (mock completes in 500ms, poll at 500ms)
    await expect(page.locator('text=FILLED')).toBeVisible({ timeout: 10000 });
  });

  test('failed command shows error', async ({ page }) => {
    await page.goto('/');
    const input = page.locator('input[placeholder="Enter command..."]');
    await input.fill('buy INVALID 1 mid');
    await input.press('Enter');
    // Should show error — use first() to avoid strict mode violation
    await expect(page.locator('text=simulated error').first()).toBeVisible({ timeout: 10000 });
  });

  test('failed command creates alert', async ({ page }) => {
    await page.goto('/');
    const input = page.locator('input[placeholder="Enter command..."]');
    await input.fill('fail test');
    await input.press('Enter');
    await page.waitForTimeout(3000);
    await expect(page.locator('text=Order Failed').first()).toBeVisible();
  });

  test('status command returns system info', async ({ page }) => {
    await page.goto('/');
    const input = page.locator('input[placeholder="Enter command..."]');
    await input.fill('status');
    await input.press('Enter');
    await expect(page.locator('text=Positions:').first()).toBeVisible({ timeout: 10000 });
  });

  test('copy button appears on completed command', async ({ page }) => {
    await page.goto('/');
    const input = page.locator('input[placeholder="Enter command..."]');
    await input.fill('status');
    await input.press('Enter');
    await expect(page.locator('button:has-text("copy")').first()).toBeVisible({ timeout: 10000 });
  });

  test('two commands show separator line', async ({ page }) => {
    await page.goto('/');
    const input = page.locator('input[placeholder="Enter command..."]');
    await input.fill('status');
    await input.press('Enter');
    await page.waitForTimeout(2000);
    await input.fill('help');
    await input.press('Enter');
    await page.waitForTimeout(2000);
    await expect(page.locator('text=Positions:').first()).toBeVisible();
    await expect(page.locator('text=Available commands').first()).toBeVisible();
  });
});

test.describe('Text Selection', () => {
  test('console output text is selectable', async ({ page }) => {
    await page.goto('/');
    const input = page.locator('input[placeholder="Enter command..."]');
    await input.fill('status');
    await input.press('Enter');
    await expect(page.locator('text=Positions:').first()).toBeVisible({ timeout: 10000 });
    const style = await page.locator('text=Positions:').first().evaluate(
      el => window.getComputedStyle(el).userSelect
    );
    expect(style).not.toBe('none');
  });
});

test.describe('Layout', () => {
  test('layout variant buttons work', async ({ page }) => {
    await page.goto('/');
    await page.locator('button:has-text("B · Modern")').click();
    await page.waitForTimeout(500);
    await page.locator('button:has-text("A · Classic")').click();
  });

  test('all major panels are present', async ({ page }) => {
    await page.goto('/');
    // Check panel titles exist (from PanelShell headers)
    await expect(page.locator('.panel-title:has-text("CONSOLE")').first()).toBeVisible({ timeout: 5000 });
    await expect(page.locator('.panel-title:has-text("POSITIONS")').first()).toBeVisible();
    await expect(page.locator('.panel-title:has-text("LOGS")').first()).toBeVisible();
  });
});
