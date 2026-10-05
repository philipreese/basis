import { type Locator, type Page } from '@playwright/test';

/** The desktop tab bar lives inside the header; the mobile bar does not. */
export function desktopTab(page: Page, label: string): Locator {
  return page.locator('header').getByRole('button', { name: label, exact: true });
}

/** The phone bottom bar (#1133): the one <nav> labelled "Main" outside the header. */
export function mobileTab(page: Page, label: string): Locator {
  return page.getByRole('navigation', { name: 'Main' }).getByRole('button', { name: label, exact: true });
}

/** Books tab filters (#1133): Active / Practice / Retired, defaulting to Active. */
export function bookFilter(page: Page, id: 'active' | 'practice' | 'retired'): Locator {
  return page.getByTestId(`book-filter-${id}`);
}
