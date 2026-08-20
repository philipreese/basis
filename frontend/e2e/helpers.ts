import { type Locator, type Page } from '@playwright/test';

/** The desktop tab bar lives inside the header; the mobile bar does not. */
export function desktopTab(page: Page, label: string): Locator {
  return page.locator('header').getByRole('button', { name: label, exact: true });
}
