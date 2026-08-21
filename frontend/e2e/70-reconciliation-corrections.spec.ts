import { expect, test } from '@playwright/test';
import { desktopTab } from './helpers';

// #480: the three correction forms move book cash and are the highest-
// stakes untested UI in the app. The DRIFT run, PARTIAL order, and the
// live SUBMITTED close order below are seeded by scripts/e2e_backend.py
// (_seed_e2e_fixtures) — there is no POST /api/orders or POST
// /api/reconciliation endpoint by design (orders are executor-only;
// reconciliation runs are written by the nightly sync), so this state is
// unreachable through any other real-backend path.

const ACK_CANCEL_POSITION = {
  id: 'e2e-pos-ack-cancel',
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
  current_value_per_share: 0.5,
  contracts: 1,
  max_profit: 1.0,
  max_loss: 4.0,
  notes: 'e2e seed — has a live SUBMITTED close order (basis:B01:e2e_live_close_1:close)',
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

test('reconciliation DRIFT panel renders the seeded drift and correction forms are reachable', async ({ page }) => {
  await page.goto('/');
  await desktopTab(page, 'Books').click();

  const panel = page.getByTestId('reconciliation-drift');
  await expect(panel).toBeVisible();
  await expect(panel).toContainText('Reconciliation DRIFT');
  await expect(panel).toContainText('ORPHAN: AAPL261016C00230000');
  await expect(panel).toContainText('broker=1');
  await expect(panel).toContainText('expected=0');
});

test('external close refuses an uncancelled live order, then succeeds with acknowledge_cancelled', async ({ page, request }) => {
  const created = await request.post('/api/positions', { data: ACK_CANCEL_POSITION });
  expect(created.ok()).toBeTruthy();

  await page.goto('/');
  await desktopTab(page, 'Books').click();
  await page.getByTestId('recon-open-external-close').click();

  await page.getByTestId('recon-close-position').fill(ACK_CANCEL_POSITION.id);
  await page.getByTestId('recon-close-value').fill('0.50');
  await page.getByTestId('recon-close-reason').fill('e2e: closed by hand at IBKR');

  // Refusal path (#407): a live SUBMITTED order on the position blocks the
  // close until the operator explicitly acknowledges it's cancelled at the
  // broker — never submit acknowledge_cancelled by default.
  await expect(page.getByTestId('recon-close-ack')).not.toBeChecked();
  await page.getByTestId('recon-close-submit').click();
  await expect(page.getByText(/External close failed.*live broker order/)).toBeVisible();

  // Re-submit with the box checked — now succeeds.
  await page.getByTestId('recon-close-ack').check();
  await page.getByTestId('recon-close-submit').click();
  await expect(page.getByText(/External close recorded/)).toBeVisible();
  await expect(page.getByTestId('recon-close-position')).toBeHidden(); // form closed
});

test('cash adjustment form applies a signed correction to a book', async ({ page }) => {
  await page.goto('/');
  await desktopTab(page, 'Books').click();
  await page.getByTestId('recon-open-cash').click();

  await page.getByTestId('recon-cash-book').fill('B01');
  await page.getByTestId('recon-cash-delta').fill('-12.50');
  await page.getByTestId('recon-cash-reason').fill('e2e: assignment fee missing from fills');
  await page.getByTestId('recon-cash-submit').click();

  await expect(page.getByText(/B01 cash adjusted/)).toBeVisible();
});

test('partial-order release clears the PARTIAL latch', async ({ page }) => {
  await page.goto('/');
  await desktopTab(page, 'Books').click();
  await page.getByTestId('recon-open-partial').click();

  await page.getByTestId('recon-partial-ref').fill('basis:B01:e2e_partial_1:open');
  await page.getByTestId('recon-partial-reason').fill('e2e: remainder cancelled at IBKR');
  await page.getByTestId('recon-partial-submit').click();

  await expect(page.getByText(/e2e_partial_1.*CANCELLED.*encumbrance released/)).toBeVisible();
});

test('marking the drift run resolved leaves entries halted (a separate RESUME act)', async ({ page }) => {
  await page.goto('/');
  await desktopTab(page, 'Books').click();
  await page.getByTestId('recon-open-resolve').click();

  await page.getByTestId('recon-resolve-text').fill('e2e: all corrections applied, cash and positions match');
  await page.getByTestId('recon-resolve-submit').click();

  await expect(page.getByText(/Drift marked resolved.*stay halted/)).toBeVisible();
});

// #480: the digest-UNDELIVERED badge — a composed digest that failed to push
// is as invisible an outage as a fetch failure unless surfaced explicitly.
// No real code path in this fresh e2e stack ever composes a digest, so the
// backend response is mocked here — the same established pattern as
// 60-trading-mode.spec.ts's fetch-failure test.
test('status strip shows digest UNDELIVERED when the last push failed', async ({ page }) => {
  await page.route('**/api/executor/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        heartbeat_at: '2026-08-21T22:00:00+00:00',
        heartbeat_age_hours: 1.0,
        stale: false,
        broker_ok: true,
        entries_placed: 0,
        closes_placed: 0,
        last_reconciliation_at: '2026-08-21T22:00:00+00:00',
        last_reconciliation_result: 'CLEAN',
        last_reconciliation_resolved: null,
        last_digest_pushed: false,
        last_urgent_pushed: null,
        trading_mode: 'paper',
      }),
    }),
  );

  await page.goto('/');
  await expect(page.getByTestId('digest-status')).toBeVisible();
  await expect(page.getByTestId('digest-status')).toContainText('digest UNDELIVERED');
});
