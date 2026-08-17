import { expect, test } from '@playwright/test';
import { acknowledge, desktopTab } from './helpers';

test('Books tab renders the six lab books with the Live Gate checklist', async ({ page }) => {
  await page.goto('/');
  await acknowledge(page);
  await desktopTab(page, 'Books').click();

  const table = page.getByTestId('books-table');
  await expect(table).toBeVisible();

  // init_db seeds B01–B06 (V0/V1/V2 × XSP/SPY); B00 legacy is excluded.
  await expect(table.locator('tbody tr')).toHaveCount(6);
  await expect(table).toContainText('B01');
  await expect(table).toContainText('B06');
  await expect(table).not.toContainText('B00');

  // Live Gate checklist shows current values on a fresh book — nothing eligible.
  await expect(table).toContainText('0/30 trades');
  await expect(table).not.toContainText('ELIGIBLE');

  // Audit trail section with its filters is present.
  await expect(page.getByRole('heading', { name: 'Audit Trail' })).toBeVisible();
  await expect(page.getByTestId('audit-filter-book')).toBeVisible();
});
