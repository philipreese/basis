/// <reference types="vitest/globals" />

import { render, screen, fireEvent } from '@testing-library/svelte';
import BookCard from '../lib/BookCard.svelte';
import type { BookSummary, TradingControlView, LiveGateChecklist } from '../lib/api';

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
    expectancy_after_haircut: 12.5,
    expectancy_se: 3.1,
    expectancy_ok: true,
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
});
