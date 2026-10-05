import { expect, test } from '@playwright/test';

// #475: a live backend whose /api/executor/status 500s must never read as a
// falsely "safe" PAPER badge — the console has to say it doesn't know.
test('trading-mode badge shows unknown, not a fabricated PAPER, when the status fetch fails', async ({ page }) => {
  await page.route('**/api/executor/status', (route) => route.fulfill({ status: 500, body: 'boom' }));

  await page.goto('/');

  const badge = page.getByTestId('trading-mode-badge');
  await expect(badge).toContainText('MODE UNKNOWN');
  await expect(badge).not.toContainText('PAPER');
});

function executorStatusBody(tradingMode: 'paper' | 'live'): string {
  return JSON.stringify({
    heartbeat_at: '2026-08-21T22:00:00+00:00',
    heartbeat_age_hours: 1.0,
    stale: false,
    broker_ok: true,
    entries_placed: 0,
    closes_placed: 0,
    last_reconciliation_at: '2026-08-21T22:00:00+00:00',
    last_reconciliation_result: 'CLEAN',
    last_reconciliation_resolved: null,
    last_digest_pushed: true,
    last_urgent_pushed: null,
    trading_mode: tradingMode,
  });
}

// #1148: the PAPER side of the money check is the IBKR paper account's
// play-money balance, which never matches the ledger — a side-by-side with
// no explanation reads as an alarm. Reconciliation ("Records match") is
// unaffected, since it compares positions, not NAV.
test('money check shows the paper-account note in PAPER mode, none in LIVE', async ({ page }) => {
  await page.route('**/api/executor/status', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: executorStatusBody('paper') }),
  );

  await page.goto('/');

  await expect(page.getByTestId('home-money-check-paper-note')).toContainText(
    "Paper account — the broker's play-money balance isn't expected to match.",
  );
  await expect(page.getByTestId('home-records')).toContainText('Records match');
});

test('money check shows no paper-account note in LIVE mode', async ({ page }) => {
  await page.route('**/api/executor/status', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: executorStatusBody('live') }),
  );

  await page.goto('/');

  await expect(page.getByTestId('trading-mode-badge')).toContainText('LIVE');
  await expect(page.getByTestId('home-money-check-paper-note')).toHaveCount(0);
  await expect(page.getByTestId('home-records')).toContainText('Records match');
});
