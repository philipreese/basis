import { expect, test } from '@playwright/test';

// ADR-0008: the status strip is the ONLY place RESUME exists. If this flow
// breaks, a halted system cannot be resumed — and worse, a running system
// cannot be halted from the console. This is the pack's most important test.
test('HALT and RESUME round-trip via the status strip', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('global-state')).toContainText('GLOBAL ACTIVE');

  // HALT requires a typed reason; the confirm stays disabled without one.
  await page.getByTestId('halt-global').click();
  await expect(page.getByTestId('control-confirm')).toBeDisabled();
  await page.getByTestId('control-reason').fill('e2e drill');
  await page.getByTestId('control-confirm').click();

  await expect(page.getByTestId('global-state')).toContainText('GLOBAL HALT_ENTRIES');

  // Halts latch — resuming also demands a typed reason.
  await page.getByTestId('resume-global').click();
  await page.getByTestId('control-reason').fill('e2e drill complete');
  await page.getByTestId('control-confirm').click();

  await expect(page.getByTestId('global-state')).toContainText('GLOBAL ACTIVE');

  // The round-trip survives a reload — state lives in the backend, not the page.
  await page.reload();
  await expect(page.getByTestId('global-state')).toContainText('GLOBAL ACTIVE');
});
