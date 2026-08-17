import { expect, type Locator, type Page } from '@playwright/test';

/** The desktop tab bar lives inside the header; the mobile bar does not. */
export function desktopTab(page: Page, label: string): Locator {
  return page.locator('header').getByRole('button', { name: label, exact: true });
}

/** Clear the ADR-0005 session lock so gated tabs become reachable. */
export async function acknowledge(page: Page): Promise<void> {
  await page.getByRole('button', { name: /Acknowledge & Unlock/ }).click();
  await expect(page.getByRole('button', { name: /Acknowledge & Unlock/ })).toBeHidden();
}
