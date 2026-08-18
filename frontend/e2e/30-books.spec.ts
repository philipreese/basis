import { expect, test } from '@playwright/test';
import { acknowledge, desktopTab } from './helpers';

test('Books tab renders the lab book matrix with the Live Gate checklist', async ({ page }) => {
  await page.goto('/');
  await acknowledge(page);
  await desktopTab(page, 'Books').click();

  const table = page.getByTestId('books-table');
  await expect(table).toBeVisible();

  // init_db seeds the ADR-0009 experiment matrix (17 books today; B18+
  // arrive with their enabling PRs); B00 legacy is excluded.
  await expect(table.locator('tbody tr')).toHaveCount(17);
  await expect(table).toContainText('B01');
  await expect(table).toContainText('B17');
  await expect(table).not.toContainText('B00');

  // Live Gate checklist shows current values on a fresh book — nothing eligible.
  await expect(table).toContainText('0/30 trades');
  await expect(table).not.toContainText('ELIGIBLE');

  // Audit trail section with its filters is present.
  await expect(page.getByRole('heading', { name: 'Audit Trail' })).toBeVisible();
  await expect(page.getByTestId('audit-filter-book')).toBeVisible();
});
