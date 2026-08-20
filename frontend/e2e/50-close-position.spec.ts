import { expect, test } from '@playwright/test';

// A credit spread marked at 60% profit trips the P1 profit-target rule, so
// the position deterministically surfaces in the "close now" panel.
const P1_POSITION = {
  id: 'e2e-pos-1',
  underlying: 'SPY',
  strategy_type: 'BULL_PUT_SPREAD',
  legs: [
    { option_type: 'PUT', direction: 'SHORT', strike: 700, expiration: '2027-06-18', delta: -0.3, theta: 0.05, vega: 0.1, gamma: 0.01 },
    { option_type: 'PUT', direction: 'LONG', strike: 695, expiration: '2027-06-18', delta: -0.2, theta: 0.03, vega: 0.08, gamma: 0.01 },
  ],
  entry_date: '2026-08-01',
  expiration_date: '2027-06-18',
  entry_premium: 1.0,
  premium_direction: 'CREDIT',
  current_value_per_share: 0.4,
  contracts: 1,
  max_profit: 1.0,
  max_loss: 4.0,
  notes: 'e2e seed',
  rolls: 0,
  status: 'OPEN',
  journal: {
    core_thesis_rationale: 'e2e smoke seed position',
    structural_invalidation: 'n/a — synthetic test position',
    expected_underlying_move_pct: 1.0,
    pre_trade_emotional_state: 'Calm',
    pre_trade_confidence_rating: 3,
  },
};

test('close-position flow completes end to end', async ({ page, request }) => {
  // Seed one OPEN position through the real API (journal is mandatory).
  const created = await request.post('/api/positions', { data: P1_POSITION });
  expect(created.ok()).toBeTruthy();

  await page.goto('/');

  // The profit-target P1 renders above the fold with a close action.
  await page.getByRole('button', { name: /Close.*Now/ }).first().click();

  // Close modal: value, trigger, and move are all required.
  await expect(page.getByRole('heading', { name: 'Close Position' })).toBeVisible();
  await page.getByPlaceholder('e.g. 12.50').fill('0.40');
  await page.locator('select').selectOption('PROFIT_TARGET');
  await page.getByPlaceholder('e.g. -1.5').fill('1.0');
  await page.getByRole('button', { name: /Confirm Close/ }).click();

  // Post-mortem toast confirms the WIN and the realized P&L.
  await expect(page.getByText(/Position closed\. Outcome: WIN/)).toBeVisible();
  await expect(page.getByRole('button', { name: /Close.*Now/ })).toBeHidden();
});
