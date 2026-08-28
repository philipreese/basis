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

  // Overview headline (#860): fleet ledger NAV + broker NAV, two labeled
  // provenances — a fresh DB renders both cards (broker side shows "—").
  await expect(page.getByText('Open Positions', { exact: true })).toBeVisible();
  await expect(page.getByText('Fleet NAV', { exact: true })).toBeVisible();
  await expect(page.getByText('Broker NAV', { exact: true })).toBeVisible();
});
