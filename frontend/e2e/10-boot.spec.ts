import { expect, test } from '@playwright/test';

test('app boots against a fresh database with Layer A and the status strip', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: 'basis' })).toBeVisible();

  // Status strip (#73): PAPER badge and the seeded ACTIVE global control.
  const strip = page.getByTestId('status-strip');
  await expect(strip).toContainText('PAPER');
  await expect(page.getByTestId('global-state')).toContainText('GLOBAL ACTIVE');

  // Executor has never run on a fresh DB — staleness must be honest, not green.
  await expect(page.getByTestId('executor-age')).toContainText('never');

  // Layer A account overview renders from the seeded config.
  await expect(page.getByText('Open Positions', { exact: true })).toBeVisible();
  await expect(page.getByText('Total NAV', { exact: true })).toBeVisible();
});
