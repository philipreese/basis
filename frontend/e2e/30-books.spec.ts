import { expect, test } from '@playwright/test';
import { desktopTab } from './helpers';

test('Books tab renders the lab book matrix with the Live Gate checklist', async ({ page }) => {
  await page.goto('/');
  await desktopTab(page, 'Books').click();

  const table = page.getByTestId('books-table');
  await expect(table).toBeVisible();

  // init_db seeds the complete ADR-0009 experiment matrix (34 books after
  // the #219 sweeps, #254 regime-flip exit, the #316-#319 arms, the #816
  // B33 delta-cap arm, and the #820 B34 minimum-credit floor arm); B00
  // legacy is excluded.
  await expect(table.locator('tbody tr')).toHaveCount(34);
  await expect(table).toContainText('B01');
  await expect(table).toContainText('B34');
  await expect(table).not.toContainText('B00');

  // Live Gate checklist shows current values on a fresh book — nothing eligible.
  await expect(table).toContainText('0/30 trades');
  await expect(table).not.toContainText('ELIGIBLE');

  // Audit trail section with its filters is present.
  await expect(page.getByRole('heading', { name: 'Audit Trail' })).toBeVisible();
  await expect(page.getByTestId('audit-filter-book')).toBeVisible();
});

// #890 step 5: B00 isn't a lab book (excluded from book_summaries()/the
// table above), so it gets its own card with the Greeks/Safeguards
// workbench that used to render unconditionally on Overview.
test('B00 gets its own card with the Greeks/Safeguards workbench, not a table row', async ({ page }) => {
  await page.goto('/');
  await desktopTab(page, 'Books').click();

  await expect(page.getByRole('heading', { name: 'Manual Book' })).toBeVisible();
  const b00Card = page.getByTestId('book-card-B00');
  await expect(b00Card).toBeVisible();
  await expect(b00Card).not.toContainText('conditions'); // no Live Gate for the manual lane

  await page.getByTestId('book-card-B00-workbench-toggle').click();
  const detail = page.getByTestId('book-card-B00-detail');
  await expect(detail).toContainText('Net Delta');
  await expect(detail).toContainText('Net Gamma');
});
