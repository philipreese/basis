import { expect, test } from '@playwright/test';
import { desktopTab } from './helpers';

test('every tab is immediately reachable — no session lock (#315)', async ({ page }) => {
  await page.goto('/');

  for (const label of ['Overview', 'Scan', 'Books', 'Analysis', 'Settings']) {
    await expect(desktopTab(page, label)).toBeEnabled();
  }

  await desktopTab(page, 'Books').click();
  await expect(page.getByRole('heading', { name: 'Lab Books' })).toBeVisible();

  await desktopTab(page, 'Scan').click();
  await expect(page.getByRole('heading', { name: "What would tonight's scan do?" })).toBeVisible();

  await desktopTab(page, 'Overview').click();
  await expect(page.getByText('Open Positions', { exact: true })).toBeVisible();
});
