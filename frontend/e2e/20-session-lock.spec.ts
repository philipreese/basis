import { expect, test } from '@playwright/test';
import { acknowledge, desktopTab } from './helpers';

test('session lock gates navigation until acknowledged; re-lock restores it', async ({ page }) => {
  await page.goto('/');

  // Gated tabs are disabled before acknowledgement (ADR-0005).
  for (const label of ['Opportunities', 'Performance', 'Books', 'Settings']) {
    await expect(desktopTab(page, label)).toBeDisabled();
  }

  await acknowledge(page);
  for (const label of ['Opportunities', 'Performance', 'Books', 'Settings']) {
    await expect(desktopTab(page, label)).toBeEnabled();
  }

  await desktopTab(page, 'Books').click();
  await expect(page.getByRole('heading', { name: 'Lab Books' })).toBeVisible();

  // Re-lock returns to review mode and re-gates everything.
  await page.getByRole('button', { name: /Re-lock/ }).click();
  await expect(desktopTab(page, 'Books')).toBeDisabled();
  await expect(page.getByText('Open Positions', { exact: true })).toBeVisible(); // back on scanner
});
