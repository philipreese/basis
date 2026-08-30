/// <reference types="vitest/globals" />

import { render, screen, fireEvent } from '@testing-library/svelte';
import AttentionItem from '../lib/AttentionItem.svelte';
import type { AttentionRowItem } from '../lib/api';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function item(overrides: Partial<AttentionRowItem['action']> = {}): AttentionRowItem {
  return {
    id: 'partial:o1',
    title: 'Partial order — B04 SPY iron condor',
    action: {
      kind: 'resolve_partial_order',
      label: 'Resolve partial order',
      requires_reason: true,
      endpoint: '/api/resolution/partial-order',
      target: { order_ref: 'o1' },
      ...overrides,
    },
  };
}

// #915: the reason form is gated on action.requires_reason, not on which
// action.kind it is — resolve_partial_order is used for both cases below to
// prove the gate reads the boolean, not a hardcoded per-kind mapping.
describe('AttentionItem requires_reason gating', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('opens the reason form when requires_reason is true', async () => {
    render(AttentionItem, { props: { item: item({ requires_reason: true }) } });

    await fireEvent.click(screen.getByTestId('attention-item-partial:o1-action'));

    expect(screen.getByTestId('attention-item-partial:o1-form')).toBeInTheDocument();
  });

  it('submits immediately with no reason form when requires_reason is false', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ order_ref: 'o1', resolution: 'ok' }),
    );
    const onResolved = vi.fn();
    render(AttentionItem, { props: { item: item({ requires_reason: false }), onResolved } });

    await fireEvent.click(screen.getByTestId('attention-item-partial:o1-action'));

    expect(screen.queryByTestId('attention-item-partial:o1-form')).not.toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalled();
    await vi.waitFor(() => expect(onResolved).toHaveBeenCalled());
  });
});
