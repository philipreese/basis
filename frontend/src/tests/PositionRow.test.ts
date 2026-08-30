/// <reference types="vitest/globals" />

import { render, screen, fireEvent } from '@testing-library/svelte';
import PositionRow from '../lib/PositionRow.svelte';
import type { PortfolioObservation, ScannedPosition } from '../lib/api';

function scannedPosition(overrides: Partial<ScannedPosition> = {}): ScannedPosition {
  return {
    position_id: 'pos-1',
    underlying: 'SPY',
    strategy_type: 'BULL_PUT_SPREAD',
    contracts: 1,
    max_loss: 4.0,
    max_profit: 1.0,
    entry_premium: 1.0,
    premium_direction: 'CREDIT',
    current_value_per_share: 0.4,
    expiration_date: new Date(Date.now() + 21 * 86_400_000).toISOString().slice(0, 10),
    priority: 'P2 — REVIEW',
    action: 'MONITOR',
    reason: 'within normal range',
    math_detail: 'no thresholds tripped',
    legs: [
      { option_type: 'PUT', direction: 'SHORT', strike: 700, expiration: '2027-06-18', delta: -0.3, theta: 0.05, vega: 0.1, gamma: 0.01 },
      { option_type: 'PUT', direction: 'LONG', strike: 695, expiration: '2027-06-18', delta: -0.2, theta: 0.03, vega: 0.08, gamma: 0.01 },
    ],
    roll: null,
    close_in_flight: false,
    close_in_flight_since: null,
    ...overrides,
  };
}

function observation(positions: ScannedPosition[]): PortfolioObservation {
  return {
    scanned_positions: positions,
    greeks: { net_delta: 0, net_theta: 0, net_vega: 0, net_gamma: 0 },
    safeguards: [],
    market_state: {
      current_regime: 'CALM_BULL',
      spy_price: 600,
      spy_sma20: 590,
      vix_close: 15,
      spy_daily_return: 0,
    },
  };
}

describe('PositionRow', () => {
  it('collapses by default, showing only the at-a-glance fields', () => {
    const pos = scannedPosition();
    render(PositionRow, { props: { observation: observation([pos]) } });

    expect(screen.getByTestId('position-row-pos-1')).toBeInTheDocument();
    expect(screen.getByTestId('position-row-pos-1-pnl')).toBeInTheDocument();
    expect(screen.getByText('SPY')).toBeInTheDocument();
    expect(screen.getByText(/BULL PUT SPREAD/)).toBeInTheDocument();

    // Expanded-only detail is not rendered until tapped.
    expect(screen.queryByText('Option Legs')).not.toBeInTheDocument();
    expect(screen.queryByText('within normal range')).not.toBeInTheDocument();
  });

  it('tapping the row expands it to reveal the full detail card', async () => {
    const pos = scannedPosition();
    render(PositionRow, { props: { observation: observation([pos]) } });

    await fireEvent.click(screen.getByTestId('position-row-pos-1'));

    expect(screen.getByText('Option Legs')).toBeInTheDocument();
    expect(screen.getByText('within normal range')).toBeInTheDocument();

    // Tapping again collapses it.
    await fireEvent.click(screen.getByTestId('position-row-pos-1'));
    expect(screen.queryByText('Option Legs')).not.toBeInTheDocument();
  });

  it('shows an in-flight status chip instead of P&L when a close is already staged', () => {
    const pos = scannedPosition({ close_in_flight: true, close_in_flight_since: '2026-08-29 10:00' });
    render(PositionRow, { props: { observation: observation([pos]) } });

    expect(screen.getByTestId('position-row-pos-1-status')).toHaveTextContent('in flight');
    expect(screen.queryByTestId('position-row-pos-1-pnl')).not.toBeInTheDocument();
  });

  it('exposes the Close Position action inside the expanded P1 row', async () => {
    const pos = scannedPosition({ priority: 'P1 — CLOSE NOW' });
    const onClosePosition = vi.fn();
    render(PositionRow, { props: { observation: observation([pos]), onClosePosition } });

    await fireEvent.click(screen.getByTestId('position-row-pos-1'));
    await fireEvent.click(screen.getByRole('button', { name: /Close Position Now/ }));

    expect(onClosePosition).toHaveBeenCalledWith('pos-1');
  });
});
