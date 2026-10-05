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

  // Home's money check (#860, #1133): fleet ledger NAV + broker NAV, two
  // labeled provenances — a fresh DB renders both (broker side shows "—").
  const money = page.getByTestId('home-money-check');
  await expect(money).toContainText('Fleet NAV');
  await expect(money).toContainText('Broker NAV');
  await expect(page.getByTestId('home-broker-nav')).toHaveText('—');
  // The e2e fixture seeds a DRIFT run (scripts/e2e_backend.py): the money
  // check must name it and link to Books, never read "Records match".
  await expect(page.getByTestId('home-records')).toContainText('Reconciliation DRIFT');
  await expect(page.getByTestId('home-records')).not.toContainText('Records match');
});
