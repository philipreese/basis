/// <reference types="vitest/globals" />

import { render, screen, fireEvent, within } from '@testing-library/svelte';
import PracticeReviewsPanel from '../lib/PracticeReviewsPanel.svelte';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function attention(practice: unknown[]): unknown {
  return {
    generated_at: '2026-10-05T12:00:00+00:00', status: 'ok', headline: 'All clear', problem_count: 0,
    sentinel_halt: false, halts: [], p1_actions: [], practice_reviews: practice, reconciliation_drift: null,
    partial_orders: [], flex_discrepancies: [], delivery_gaps: [], broker_errors: [], unresolved_urgent_events: [],
  };
}

const review = {
  position_id: 'r1', book_id: 'B12', underlying: 'XSP', strategy_type: 'BEAR_CALL_SPREAD',
  priority: 'P2 — REVIEW', reason: 'Regime conflict', close_in_flight: false,
  action: { kind: 'close_position', label: 'Close now', requires_reason: false, endpoint: '/api/positions/r1/close', target: { position_id: 'r1' } },
};

describe('PracticeReviewsPanel (#1133)', () => {
  afterEach(() => vi.restoreAllMocks());

  it('lists the practice-book flags, folded, each with its close action', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(attention([review])));
    const onClosePosition = vi.fn();
    render(PracticeReviewsPanel, { props: { onClosePosition } });

    const panel = await screen.findByTestId('lab-practice-reviews');
    expect(await within(panel).findByText('1, advisory')).toBeInTheDocument();
    expect(screen.queryByTestId('lab-practice-review-rows')).not.toBeInTheDocument();
    await fireEvent.click(within(panel).getByRole('button', { name: /Show the flags/ }));
    expect(screen.getByTestId('lab-practice-review-rows').children).toHaveLength(1);
    await fireEvent.click(screen.getByTestId('attention-item-review:r1-action'));
    expect(onClosePosition).toHaveBeenCalledWith('r1');
  });

  it('says so when there are none, and when the fetch fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(jsonResponse(attention([])));
    const { unmount } = render(PracticeReviewsPanel);
    expect(await screen.findByText('No review flags on the practice books.')).toBeInTheDocument();
    unmount();

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ detail: 'boom' }, 500));
    render(PracticeReviewsPanel);
    expect(await screen.findByText('failed to load')).toBeInTheDocument();
  });
});
