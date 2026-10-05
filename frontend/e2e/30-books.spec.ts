import { expect, test } from '@playwright/test';
import { bookFilter, desktopTab } from './helpers';

test('Books tab filters Active / Practice / Retired over the full lab book matrix', async ({ page }) => {
  await page.goto('/');
  await desktopTab(page, 'Books').click();

  // init_db seeds the complete ADR-0009 experiment matrix (38 books after
  // the #219 sweeps, #254 regime-flip exit, the #316-#319 arms, the #816
  // B33 delta-cap arm, the #820 B34 minimum-credit floor arm, the #993
  // B35 long-vol event arm, the #1054 B36 ETF trend book, the #1079 B37
  // wide, far-dated condor arm, and the #1092 B38 turn-of-month book);
  // B00 legacy is excluded. #1088 retired 27 of them: still listed with
  // their history, marked RETIRED, 11 active. #1133 splits the 11 into the
  // two share books (Active, the default) and nine options books (Practice).
  await expect(bookFilter(page, 'active')).toHaveAttribute('aria-pressed', 'true');
  await expect(bookFilter(page, 'active')).toHaveText('Active · 2');
  await expect(bookFilter(page, 'practice')).toHaveText('Practice · 10'); // nine options books + B00
  await expect(bookFilter(page, 'retired')).toHaveText('Retired · 27');

  // Active: the share books, as cards on every viewport (#1133), each with
  // its own yardstick and stage-1 checklist — never the options Live Gate.
  const cards = page.getByTestId('books-cards');
  await expect(cards.locator('[data-testid^="book-card-B"][data-testid$="-action"]')).toHaveCount(2);
  await expect(page.getByTestId('books-table')).toHaveCount(0);
  const b36 = page.getByTestId('book-card-B36');
  await expect(b36).toContainText('Waiting for first fill');
  await expect(b36).toContainText('Stage 1 entry bar');
  await expect(b36).not.toContainText('trades');
  const b38 = page.getByTestId('book-card-B38');
  await expect(b38).toContainText('No yardstick of its own yet');
  await expect(b38).toContainText('Stage 1 entry bar');
  await expect(b38).not.toContainText('trades');

  // Practice: the options books in the desktop table, with the Live Gate.
  await bookFilter(page, 'practice').click();
  const table = page.getByTestId('books-table');
  await expect(table.locator('tbody tr')).toHaveCount(9);
  await expect(table.locator('[data-testid^="book-retired-"]')).toHaveCount(0);
  await expect(table).toContainText('B01');
  await expect(table).toContainText('B30');
  await expect(table).toContainText('B35');
  await expect(table).toContainText('B37');
  await expect(table).not.toContainText('B36');
  await expect(table).not.toContainText('B00');
  // Live Gate checklist shows current values on a fresh book — nothing eligible.
  await expect(table).toContainText('0/30 trades');
  await expect(table).not.toContainText('ELIGIBLE');

  // Retired: every retired book, still listed with its history.
  await bookFilter(page, 'retired').click();
  await expect(table.locator('tbody tr')).toHaveCount(27);
  await expect(table.locator('[data-testid^="book-retired-"]')).toHaveCount(27);
  await expect(table.getByTestId('book-retired-B12')).toBeVisible();

  // Broker and records stay one tap away; audit trail with its filters below.
  await expect(page.getByTestId('books-records')).toBeAttached();
  await expect(page.getByRole('heading', { name: 'Audit Trail' })).toBeVisible();
  await expect(page.getByTestId('audit-filter-book')).toBeVisible();
});

// #890 step 5: B00 isn't a lab book (excluded from book_summaries()/the
// table above), so it gets its own card with the Greeks/Safeguards
// workbench. #1133: it is the manual options lane, so it sits under Practice.
test('B00 gets its own card with the Greeks/Safeguards workbench, not a table row', async ({ page }) => {
  await page.goto('/');
  await desktopTab(page, 'Books').click();
  await expect(page.getByTestId('book-card-B00')).toHaveCount(0);
  await bookFilter(page, 'practice').click();

  await expect(page.getByRole('heading', { name: 'Manual Book' })).toBeVisible();
  const b00Card = page.getByTestId('book-card-B00');
  await expect(b00Card).toBeVisible();
  await expect(b00Card).not.toContainText('conditions'); // no Live Gate for the manual lane

  await page.getByTestId('book-card-B00-workbench-toggle').click();
  const detail = page.getByTestId('book-card-B00-detail');
  await expect(detail).toContainText('Net Delta');
  await expect(detail).toContainText('Net Gamma');
});
