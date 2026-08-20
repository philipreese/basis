import { expect, test } from '@playwright/test';

// #475: a live backend whose /api/executor/status 500s must never read as a
// falsely "safe" PAPER badge — the console has to say it doesn't know.
test('trading-mode badge shows unknown, not a fabricated PAPER, when the status fetch fails', async ({ page }) => {
  await page.route('**/api/executor/status', (route) => route.fulfill({ status: 500, body: 'boom' }));

  await page.goto('/');

  const badge = page.getByTestId('trading-mode-badge');
  await expect(badge).toContainText('MODE UNKNOWN');
  await expect(badge).not.toContainText('PAPER');

  // The Overview tab's MetricCard must not fabricate PAPER either.
  await expect(page.getByText('Trading Mode', { exact: true })).toBeVisible();
  await expect(page.getByText('MODE UNKNOWN', { exact: true }).first()).toBeVisible();
});
