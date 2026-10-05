/// <reference types="vitest/globals" />

import { render, screen, fireEvent } from '@testing-library/svelte';
import ShareBookSummary from '../lib/ShareBookSummary.svelte';
import type { BookSummary } from '../lib/api';

// Only the fields the card reads; no dollar figures (marks/values stay null).
function shareBook(overrides: Partial<BookSummary> = {}): BookSummary {
  return {
    id: 'B36',
    name: 'ETF trend',
    status: 'RUNNING',
    book_kind: 'share',
    control_state: 'ACTIVE',
    trend_yardstick: null,
    share_holdings: [],
    stage1_entry_bar: {
      stake: null,
      live_authority: null,
      era_start: '2026-10-05',
      trading_days: 0,
      trading_days_required: 15,
      filled_orders: 0,
      conditions: [
        { key: 'stage1_not_retired', label: 'not retired', status: 'ok', detail: '' },
        { key: 'stage1_zero_breaches', label: 'zero breaches', status: 'ok', detail: '' },
        { key: 'stage1_paper_days', label: '15 trading days with a fill', status: 'fail', detail: '0 of 15' },
        { key: 'stage1_operator_sign_off', label: 'sign-off', status: 'not_yet_evaluated', detail: 'no workflow' },
      ],
      claimable: false,
    },
    ...overrides,
  } as BookSummary;
}

describe('ShareBookSummary (#1133)', () => {
  it('shows stage-1 progress and the next unmet step', () => {
    render(ShareBookSummary, { props: { book: shareBook() } });
    expect(screen.getByTestId('home-share-B36-stage1')).toHaveTextContent('2 of 4');
    expect(screen.getByText('Next: 15 trading days with a fill')).toBeInTheDocument();
    expect(screen.getByTestId('home-share-B36-state')).toHaveTextContent('ACTIVE');
  });

  it('says "none yet" for no holdings and "no mark yet" for an unmarked one, never a zero', () => {
    const { unmount } = render(ShareBookSummary, { props: { book: shareBook() } });
    expect(screen.getByTestId('home-share-B36-value')).toHaveTextContent('none yet');
    expect(screen.queryByTestId('home-share-B36-holdings')).not.toBeInTheDocument();
    unmount();

    render(ShareBookSummary, {
      props: { book: shareBook({ share_holdings: [{ symbol: 'SCHB', quantity: 3, mark: null, mark_date: null, value: null }] }) },
    });
    expect(screen.getByTestId('home-share-B36-value')).toHaveTextContent('no mark yet');
    expect(screen.getByTestId('home-share-B36-holdings')).toHaveTextContent('SCHB 3');
  });

  it('shows a halted state and opens the book on tap', async () => {
    const onOpen = vi.fn();
    render(ShareBookSummary, { props: { book: shareBook({ id: 'B38', control_state: 'HALT_ENTRIES' }), onOpen } });
    expect(screen.getByTestId('home-share-B38-state')).toHaveTextContent('HALT ENTRIES');
    await fireEvent.click(screen.getByTestId('home-share-B38-open'));
    expect(onOpen).toHaveBeenCalledOnce();
  });
});
