/// <reference types="vitest/globals" />

import { render, screen, fireEvent } from '@testing-library/svelte';
import BookCard from '../lib/BookCard.svelte';
import type { BookSummary, TradingControlView, LiveGateChecklist, PortfolioObservation } from '../lib/api';

function observation(overrides: Partial<PortfolioObservation> = {}): PortfolioObservation {
  return {
    scanned_positions: [],
    greeks: { net_delta: 5, net_theta: 1, net_vega: 2, net_gamma: 0.1 },
    safeguards: [],
    market_state: {
      current_regime: 'CALM_BULL', spy_price: 550, spy_sma20: 545, vix_close: 14,
      underlying_ivrs: {}, spy_daily_return: 0, catalyst_dates: [],
      regime_scores: {}, underlying_prices: {}, underlying_sma20: {},
    },
    ...overrides,
  };
}

function liveGate(overrides: Partial<LiveGateChecklist> = {}): LiveGateChecklist {
  return {
    closed_trades: 10,
    closed_trades_required: 30,
    trades_ok: false,
    months_elapsed: 1.5,
    months_required: 3,
    months_ok: false,
    breaches: 0,
    breaches_ok: true,
    era_start: '2026-04-01',
    expectancy_after_haircut: 12.5,
    expectancy_se: 3.1,
    expectancy_ok: true,
    stress_episode_ok: false,
    stress_episode_check: {
      window_start: '2026-04-01',
      window_end: '2026-08-18',
      peak_vix_close: 18,
      max_spy_drawdown_pct: 1.2,
      episode_dates: 0,
      episode_while_position_open: false,
      episode_while_deployed: false,
      deployment_fraction_required: 0.5,
      normal_deployment: 200,
      required_deployment: 100,
      episode_deployment: null,
      max_adverse_excursion: null,
      ok: false,
    },
    benchmark_ok: true,
    benchmark_check: {
      window_start: '2026-04-01',
      window_end: '2026-08-18',
      book_return_pct: 0.5,
      spy_return_pct: -0.2,
      spy_start_date: '2026-04-01',
      spy_end_date: '2026-08-18',
      ok: true,
    },
    additional_conditions: [],
    tail_magnitude_check: {
      largest_adverse_move: 100,
      multiplier: 3,
      hypothetical_tail_loss: 300,
      informational: true,
    },
    eligible: false,
    as_raced_config_hash: 'abc12345',
    ...overrides,
  };
}

function book(overrides: Partial<BookSummary> = {}): BookSummary {
  return {
    id: 'B04',
    name: 'B04',
    status: 'RUNNING',
    engine_variant: 'default',
    underlying: 'SPY',
    config_hash: 'abc12345',
    config_version: 1,
    starting_capital: 10000,
    cash_balance: 10500,
    last_mtm: null,
    pnl: 500,
    closed_trades: 10,
    win_rate: 0.6,
    expectancy_after_haircut: 12.5,
    expectancy_se: 3.1,
    max_drawdown: 200,
    deployed_pct: 40,
    open_positions: 2,
    max_positions: 5,
    control_state: 'ACTIVE',
    live_gate: liveGate(),
    tail_hedge_metrics: null,
    ...overrides,
  };
}

function control(entries: TradingControlView['controls'] = []): TradingControlView {
  return { controls: entries, sentinel_halt: false };
}

describe('BookCard', () => {
  it('shows the at-a-glance fields and a HALT action for an active book', () => {
    render(BookCard, {
      props: { book: book(), control: control(), onSelect: vi.fn(), onControlChanged: vi.fn() },
    });

    expect(screen.getByTestId('book-card-B04')).toBeInTheDocument();
    expect(screen.getByTestId('book-card-B04-action')).toHaveTextContent('HALT');
    expect(screen.getByText(/\+500/)).toBeInTheDocument();
    expect(screen.getByText('2/5 pos')).toBeInTheDocument();
  });

  it('opens an inline reason form on tap, never a shared form, with submit disabled on empty reason', async () => {
    render(BookCard, {
      props: { book: book(), control: control(), onSelect: vi.fn(), onControlChanged: vi.fn() },
    });

    await fireEvent.click(screen.getByTestId('book-card-B04-action'));

    expect(screen.getByTestId('book-card-B04-form')).toBeInTheDocument();
    // The action button is replaced by the form, not layered alongside it.
    expect(screen.queryByTestId('book-card-B04-action')).not.toBeInTheDocument();

    const confirm = screen.getByTestId('book-card-B04-confirm');
    const reasonInput = screen.getByTestId('book-card-B04-reason');
    expect(confirm).toBeDisabled();

    await fireEvent.input(reasonInput, { target: { value: 'reviewed, halting' } });
    expect(confirm).toBeEnabled();

    await fireEvent.input(reasonInput, { target: { value: '   ' } });
    expect(confirm).toBeDisabled();
  });

  it('tapping the card body selects it without opening the control form', async () => {
    const onSelect = vi.fn();
    render(BookCard, {
      props: { book: book(), control: control(), onSelect, onControlChanged: vi.fn() },
    });

    await fireEvent.click(screen.getByTestId('book-card-B04'));

    expect(onSelect).toHaveBeenCalledOnce();
    expect(screen.queryByTestId('book-card-B04-form')).not.toBeInTheDocument();
  });

  it('shows a RESUME action and the halt reason for a halted book', () => {
    render(BookCard, {
      props: {
        book: book({ control_state: 'HALT_ENTRIES' }),
        control: control([{ scope: 'B04', state: 'HALT_ENTRIES', reason: 'drift', actor: 'system', changed_at: '2026-08-29T10:00:00+00:00' }]),
        onSelect: vi.fn(),
        onControlChanged: vi.fn(),
      },
    });

    expect(screen.getByTestId('book-card-B04-action')).toHaveTextContent('RESUME');
    expect(screen.getByTestId('book-halt-reason-B04')).toHaveTextContent('drift');
  });

  it('expands the gate detail on tap without triggering card selection', async () => {
    const onSelect = vi.fn();
    render(BookCard, {
      props: { book: book(), control: control(), onSelect, onControlChanged: vi.fn() },
    });

    await fireEvent.click(screen.getByTestId('book-card-B04-gate-toggle'));

    expect(screen.getByTestId('book-card-B04-detail')).toBeInTheDocument();
    expect(onSelect).not.toHaveBeenCalled();
  });

  // #890 step 5: B00 has no BookSummary row (book_summaries() excludes the
  // manual lane), so it renders through the same card via a minimal
  // { id, control_state } shape and shows the relocated Greeks/Safeguards
  // workbench instead of the gate-conditions detail.
  it('renders B00 as a manual-lane card with a Workbench toggle, not gate conditions', async () => {
    render(BookCard, {
      props: {
        book: { id: 'B00', control_state: 'ACTIVE' },
        control: control(),
        onSelect: vi.fn(),
        onControlChanged: vi.fn(),
        observation: observation(),
        maxNetDelta: 10, maxNetVega: 10, maxNetGamma: 10,
      },
    });

    expect(screen.getByTestId('book-card-B00')).toBeInTheDocument();
    expect(screen.queryByTestId('book-card-B00-gate-toggle')).not.toBeInTheDocument();

    await fireEvent.click(screen.getByTestId('book-card-B00-workbench-toggle'));

    const detail = screen.getByTestId('book-card-B00-detail');
    expect(detail).toBeInTheDocument();
    expect(screen.getByText(/Net Delta/)).toBeInTheDocument();
  });

  it('flags the B00 Workbench toggle when a net Greek exceeds its limit', () => {
    render(BookCard, {
      props: {
        book: { id: 'B00', control_state: 'ACTIVE' },
        control: control(),
        onSelect: vi.fn(),
        onControlChanged: vi.fn(),
        observation: observation({ greeks: { net_delta: 999, net_theta: 1, net_vega: 2, net_gamma: 0.1 } }),
        maxNetDelta: 10, maxNetVega: 10, maxNetGamma: 10,
      },
    });

    expect(screen.getByTestId('book-card-B00-workbench-toggle')).toHaveTextContent('LIMIT EXCEEDED');
  });
});
