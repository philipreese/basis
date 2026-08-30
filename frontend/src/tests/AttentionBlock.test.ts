/// <reference types="vitest/globals" />

import { render, screen, fireEvent, within } from '@testing-library/svelte';
import AttentionBlock from '../lib/AttentionBlock.svelte';
import type { AttentionResponse } from '../lib/api';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Three actionable items (a halt, a P1 close, a partial order) plus one
 * ACKNOWLEDGE_ONLY delivery gap — problem_count mirrors the backend's own
 * rule (attention.py: every action.kind != 'acknowledge_only'). */
function seededAttention(overrides: Partial<AttentionResponse> = {}): AttentionResponse {
  return {
    generated_at: '2026-08-29T12:00:00+00:00',
    status: 'attention',
    headline: '3 things need you',
    problem_count: 3,
    sentinel_halt: false,
    halts: [
      {
        scope: 'GLOBAL',
        scope_label: 'GLOBAL',
        state: 'HALT_ENTRIES',
        reason: 'reconciliation drift',
        actor: 'system',
        since: '2026-08-29T10:00:00+00:00',
        action: {
          kind: 'ack_halt',
          label: 'Review + Resume',
          requires_reason: true,
          endpoint: '/api/trading-control',
          target: { scope: 'GLOBAL' },
        },
      },
    ],
    p1_actions: [
      {
        position_id: 'e2e-pos-1',
        book_id: 'B00',
        underlying: 'SPY',
        strategy_type: 'BULL_PUT_SPREAD',
        priority: 'P1 — CLOSE NOW',
        reason: 'profit target hit',
        close_in_flight: false,
        action: {
          kind: 'close_position',
          label: 'Close now',
          requires_reason: false,
          endpoint: '/api/positions/e2e-pos-1/close',
          target: { position_id: 'e2e-pos-1' },
        },
      },
    ],
    reconciliation_drift: null,
    partial_orders: [
      {
        order_ref: 'o1',
        book_id: 'B04',
        label: 'B04 SPY iron condor',
        action: {
          kind: 'resolve_partial_order',
          label: 'Resolve partial order',
          requires_reason: true,
          endpoint: '/api/resolution/partial-order',
          target: { order_ref: 'o1' },
        },
      },
    ],
    flex_discrepancies: [],
    delivery_gaps: [
      {
        kind: 'digest',
        since: null,
        action: { kind: 'acknowledge_only', label: 'Seen', requires_reason: false, endpoint: null, target: {} },
      },
    ],
    broker_errors: [],
    unresolved_urgent_events: [],
    ...overrides,
  };
}

describe('AttentionBlock', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders one row per seeded actionable item and one collapsed informational row', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(seededAttention()));
    render(AttentionBlock);

    expect(await screen.findByTestId('attention-item-halt:GLOBAL')).toBeInTheDocument();
    expect(screen.getByTestId('attention-item-p1:e2e-pos-1')).toBeInTheDocument();
    expect(screen.getByTestId('attention-item-partial:o1')).toBeInTheDocument();

    const actionableRows = screen.getByTestId('attention-actionable-rows');
    expect(actionableRows.children).toHaveLength(3);

    // The digest gap is ACKNOWLEDGE_ONLY — folded into the collapsed
    // informational disclosure, not rendered as an alarm-weight row.
    expect(screen.queryByTestId('attention-item-gap:digest')).not.toBeInTheDocument();
    expect(screen.getByText('Informational')).toBeInTheDocument();
    await fireEvent.click(screen.getByText('Informational'));
    expect(await screen.findByTestId('attention-item-gap:digest')).toBeInTheDocument();
  });

  it('opens the reason form inline in only the tapped row', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(seededAttention()));
    render(AttentionBlock);

    await fireEvent.click(await screen.findByTestId('attention-item-halt:GLOBAL-action'));

    expect(screen.getByTestId('attention-item-halt:GLOBAL-form')).toBeInTheDocument();
    // The other actionable row's button is untouched — no shared/global form.
    expect(screen.getByTestId('attention-item-partial:o1-action')).toBeInTheDocument();
    expect(screen.queryByTestId('attention-item-partial:o1-form')).not.toBeInTheDocument();
  });

  it('disables submit on an empty reason and enables it once a reason is typed', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(seededAttention()));
    render(AttentionBlock);

    await fireEvent.click(await screen.findByTestId('attention-item-halt:GLOBAL-action'));
    const confirm = screen.getByTestId('attention-item-halt:GLOBAL-confirm');
    const reasonInput = screen.getByTestId('attention-item-halt:GLOBAL-reason');

    expect(confirm).toBeDisabled();
    await fireEvent.input(reasonInput, { target: { value: 'reviewed, resuming' } });
    expect(confirm).toBeEnabled();

    await fireEvent.input(reasonInput, { target: { value: '   ' } });
    expect(confirm).toBeDisabled();
  });

  it('headline and problem_count match the number of rendered actionable rows', async () => {
    const attention = seededAttention();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(attention));
    render(AttentionBlock);

    const headline = await screen.findByTestId('attention-headline');
    expect(within(headline).getByText(attention.headline)).toBeInTheDocument();
    expect(screen.getByTestId('attention-actionable-rows').children).toHaveLength(attention.problem_count);
  });

  it('renders the all-clear state when status is ok', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(seededAttention({
      status: 'ok',
      headline: 'All clear',
      problem_count: 0,
      halts: [],
      p1_actions: [],
      partial_orders: [],
      delivery_gaps: [],
    })));
    render(AttentionBlock);

    expect(await screen.findByTestId('attention-all-clear')).toBeInTheDocument();
    expect(screen.queryByTestId('attention-problems')).not.toBeInTheDocument();
  });

  it('CLOSE_POSITION hands off to the close callback instead of opening a reason form', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(seededAttention()));
    const onClosePosition = vi.fn();
    render(AttentionBlock, { props: { onClosePosition } });

    await fireEvent.click(await screen.findByTestId('attention-item-p1:e2e-pos-1-action'));

    expect(onClosePosition).toHaveBeenCalledWith('e2e-pos-1');
    expect(screen.queryByTestId('attention-item-p1:e2e-pos-1-form')).not.toBeInTheDocument();
  });

  it('VIEW_ONLY is routed to the informational section, not counted as a full-weight row, and still navigates on click (#915)', async () => {
    const attention = seededAttention({
      delivery_gaps: [
        {
          kind: 'digest',
          since: null,
          action: { kind: 'acknowledge_only', label: 'Seen', requires_reason: false, endpoint: null, target: {} },
        },
        {
          kind: 'urgent_push',
          since: null,
          action: {
            kind: 'view_only',
            label: 'Review books',
            requires_reason: false,
            endpoint: null,
            target: {},
            navigate_to: 'books',
          },
        },
      ],
    });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(attention));
    const onNavigate = vi.fn();
    render(AttentionBlock, { props: { onNavigate } });

    // Same three actionable rows as the base fixture — the VIEW_ONLY gap
    // does NOT join them, even though nothing in problem_count/status
    // reflects it here (this fixture keeps the server's own problem_count
    // as seeded; the point under test is purely the client-side split).
    const actionableRows = await screen.findByTestId('attention-actionable-rows');
    expect(actionableRows.children).toHaveLength(3);
    expect(screen.queryByTestId('attention-item-gap:urgent_push')).not.toBeInTheDocument();

    await fireEvent.click(screen.getByText('Informational'));
    const viewOnlyRow = await screen.findByTestId('attention-item-gap:urgent_push-action');
    expect(viewOnlyRow).toHaveTextContent('Review books');

    await fireEvent.click(viewOnlyRow);
    expect(onNavigate).toHaveBeenCalledWith('books');
  });
});
