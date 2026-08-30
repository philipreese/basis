import { expect, test } from '@playwright/test';

// ADR-0008: the console is the ONLY place RESUME exists. If this flow
// breaks, a halted system cannot be resumed. This is the pack's most
// important test.
//
// #890 moved RESUME out of the status strip's inline form and into the
// attention verdict block (AttentionBlock/AttentionItem) — every halt is
// now its own row with its own inline reason form. The strip itself is
// read-only, so this test seeds the HALT directly against the backend
// (the same POST /api/trading-control the old strip form used to make) and
// exercises the RESUME half through the real console UI.
test('HALT and RESUME round-trip via the attention verdict block', async ({ page, request, baseURL }) => {
  await page.goto('/');
  await expect(page.getByTestId('global-state')).toContainText('GLOBAL ACTIVE');

  await request.post(`${baseURL}/api/trading-control`, {
    data: { scope: 'GLOBAL', state: 'HALT_ENTRIES', reason: 'e2e drill' },
  });
  await page.reload();
  await expect(page.getByTestId('global-state')).toContainText('GLOBAL HALT_ENTRIES');

  // RESUME requires a typed reason; the confirm stays disabled without one.
  await page.getByTestId('attention-item-halt:GLOBAL-action').click();
  await expect(page.getByTestId('attention-item-halt:GLOBAL-confirm')).toBeDisabled();
  await page.getByTestId('attention-item-halt:GLOBAL-reason').fill('e2e drill complete');
  await page.getByTestId('attention-item-halt:GLOBAL-confirm').click();

  // The GLOBAL row drops off the verdict block once resumed — not asserting
  // "All clear" here, since the shared e2e fixture DB also seeds an
  // unrelated DRIFT run and PARTIAL order (70-reconciliation-corrections.spec.ts)
  // that stay unresolved at this point in the run.
  await expect(page.getByTestId('attention-item-halt:GLOBAL')).not.toBeVisible();

  // The round-trip survives a reload — state lives in the backend, not the page.
  await page.reload();
  await expect(page.getByTestId('global-state')).toContainText('GLOBAL ACTIVE');
});

// #890/#892: BookCard now carries its own inline halt/resume form (the same
// per-item convention AttentionItem established) instead of the old single
// form shared across the whole Lab Books section — so a below-the-fold
// book's form opens inside the card that was tapped and never needs a
// scroll-into-view band-aid to reach the viewport.
test.describe('book-level control on a phone viewport', () => {
  test.use({ viewport: { width: 412, height: 915 }, hasTouch: true });

  test('tapping a below-the-fold book control opens its form inline, and the round trip completes', async ({ page }) => {
    await page.goto('/');
    // The mobile bottom bar (a <nav>, outside the header) carries the tab
    // buttons at this width; the desktop header bar is display:none here.
    await page.locator('nav').getByRole('button', { name: 'Books' }).click();
    // < 768px: cards replace the table entirely (#890 §2).
    await expect(page.getByTestId('books-cards')).toBeVisible();
    await expect(page.getByTestId('books-table')).toBeHidden();

    // B30 sits ~30 cards down — genuinely below the fold at 915px.
    const haltButton = page.getByTestId('book-card-B30-action');
    await haltButton.scrollIntoViewIfNeeded();
    await haltButton.click();

    // The regression this replaces: the reason input must be INSIDE the
    // viewport right after the tap, with no scroll-into-view needed — it's
    // already inline in the card that was tapped.
    const box = await page.getByTestId('book-card-B30-reason').boundingBox();
    expect(box).not.toBeNull();
    expect(box!.y).toBeGreaterThanOrEqual(0);
    expect(box!.y + box!.height).toBeLessThanOrEqual(915);

    await page.getByTestId('book-card-B30-reason').fill('e2e mobile drill');
    await page.getByTestId('book-card-B30-confirm').click();
    await expect(page.getByTestId('book-card-B30-action')).toHaveText('HALT');

    // Resume the same way — halts latch, so the drill must not leave B30 halted.
    const resumeButton = page.getByTestId('book-card-B30-action');
    await resumeButton.scrollIntoViewIfNeeded();
    await resumeButton.click();
    const resumeBox = await page.getByTestId('book-card-B30-reason').boundingBox();
    expect(resumeBox!.y).toBeGreaterThanOrEqual(0);
    expect(resumeBox!.y + resumeBox!.height).toBeLessThanOrEqual(915);
    await page.getByTestId('book-card-B30-reason').fill('e2e mobile drill complete');
    await page.getByTestId('book-card-B30-confirm').click();
    await expect(page.getByTestId('book-card-B30-action')).toHaveText('RESUME');
  });
});
