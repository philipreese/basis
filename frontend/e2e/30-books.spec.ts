import { expect, test } from '@playwright/test';
import { desktopTab } from './helpers';

test('Books tab renders the lab book matrix with the Live Gate checklist', async ({ page }) => {
  await page.goto('/');
  await desktopTab(page, 'Books').click();

  const table = page.getByTestId('books-table');
  await expect(table).toBeVisible();

  // init_db seeds the complete ADR-0009 experiment matrix (28 books after
  // the #219 sweeps and #254 regime-flip exit); B00 legacy is excluded.
  await expect(table.locator('tbody tr')).toHaveCount(28);
  await expect(table).toContainText('B01');
  await expect(table).toContainText('B28');
  await expect(table).not.toContainText('B00');

  // Live Gate checklist shows current values on a fresh book — nothing eligible.
  await expect(table).toContainText('0/30 trades');
  await expect(table).not.toContainText('ELIGIBLE');

  // Audit trail section with its filters is present.
  await expect(page.getByRole('heading', { name: 'Audit Trail' })).toBeVisible();
  await expect(page.getByTestId('audit-filter-book')).toBeVisible();
});
