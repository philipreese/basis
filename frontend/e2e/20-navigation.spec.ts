import { expect, test } from '@playwright/test';
import { desktopTab, mobileTab } from './helpers';

const TAB_LABELS = ['Home', 'Books', 'Research', 'Options lab', 'Settings'];

test('every tab is immediately reachable — no session lock (#315, #1133)', async ({ page }) => {
  await page.goto('/');

  for (const label of TAB_LABELS) {
    await expect(desktopTab(page, label)).toBeEnabled();
  }

  await desktopTab(page, 'Books').click();
  await expect(page.getByRole('heading', { name: 'Books', exact: true })).toBeVisible();
  await expect(page.getByTestId('book-filter-active')).toHaveAttribute('aria-pressed', 'true');

  await desktopTab(page, 'Research').click();
  await expect(page.getByTestId('research-brief')).toContainText('Coming with #1131');
  await expect(page.getByTestId('research-trackers')).toContainText('Trackers run outside basis; see Home app.');
  await expect(page.getByTestId('research-scorecard')).toHaveAttribute('href', /spec\/research-scorecard\.md$/);

  // #1133: Scan, Analysis, Greek limits and telemetry are sections of the lab.
  await desktopTab(page, 'Options lab').click();
  await expect(page.getByTestId('lab-practice-reviews')).toBeVisible();
  await page.getByTestId('lab-open-scan').click();
  await expect(page.getByRole('heading', { name: "What would tonight's scan do?" })).toBeVisible();
  await page.getByTestId('lab-back').click();
  await page.getByTestId('lab-open-analysis').click();
  await expect(page.getByRole('heading', { name: 'Closed Position Post-Mortems' }).or(page.getByText('No closed positions yet.')).first()).toBeVisible();
  await page.getByTestId('lab-back').click();
  await page.getByTestId('lab-open-limits').click();
  await expect(page.getByRole('heading', { name: 'Portfolio Risk & Greek Limits' })).toBeVisible();
  await page.getByTestId('lab-back').click();
  await page.getByTestId('lab-open-telemetry').click();
  await expect(page.getByRole('heading', { name: 'Market Telemetry' })).toBeVisible();

  await desktopTab(page, 'Settings').click();
  await expect(page.getByTestId('kill-switch')).toBeVisible();
  await expect(page.getByTestId('settings-mode')).toContainText('PAPER');

  await desktopTab(page, 'Home').click();
  await expect(page.getByTestId('home-money-check')).toBeVisible();
  await expect(page.getByTestId('home-share-B36')).toBeVisible();
  await expect(page.getByTestId('home-share-B38')).toBeVisible();
});

// #1133: the operator is on a phone. No tab may push the page sideways —
// a wide table or an unwrapped control row clips controls off-screen.
test.describe('phone width', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test('no tab scrolls sideways at 390px', async ({ page }) => {
    await page.goto('/');
    for (const label of ['Home', 'Books', 'Research', 'Lab', 'Settings']) {
      await mobileTab(page, label).click();
      // Let the tab's own fetches land so late content is measured too.
      await page.waitForLoadState('networkidle');
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, `${label} overflows by ${overflow}px`).toBeLessThanOrEqual(0);
    }
    // The practice and retired lists, where the wide options table lived.
    await mobileTab(page, 'Books').click();
    for (const id of ['practice', 'retired'] as const) {
      await page.getByTestId(`book-filter-${id}`).click();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, `Books/${id} overflows by ${overflow}px`).toBeLessThanOrEqual(0);
    }
  });
});
