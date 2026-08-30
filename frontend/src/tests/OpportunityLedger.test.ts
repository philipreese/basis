/// <reference types="vitest/globals" />

import { render, screen, fireEvent } from '@testing-library/svelte';
import OpportunityLedger from '../lib/OpportunityLedger.svelte';
import type { OpportunityRecord } from '../lib/api';

function record(overrides: Partial<OpportunityRecord>): OpportunityRecord {
  return {
    id: Math.random().toString(36).slice(2),
    playbook_id: 'pb',
    playbook_version: '1.0',
    generated_at: '2026-08-01T00:00:00',
    accepted: true,
    outcome_if_taken: null,
    bypass_reason: null,
    ...overrides,
  };
}

const RECORDS: OpportunityRecord[] = [
  record({ id: 'a', playbook_id: 'PB-A', generated_at: '2026-08-01T00:00:00' }),
  record({ id: 'b', playbook_id: 'PB-B', generated_at: '2026-08-03T00:00:00' }),
  record({ id: 'c', playbook_id: 'PB-C', generated_at: '2026-08-02T00:00:00' }),
];

function cardOrder(): string[] {
  const container = screen.getByTestId('ledger-cards');
  const cards = Array.from(container.children).filter(el => el.getAttribute('data-testid') !== 'ledger-mobile-sort');
  return cards.map(el => el.textContent?.match(/PB-[A-Z]/)?.[0] ?? '');
}

// #915: the < 768px card view had no sort affordance — the mobile sort
// buttons must drive the SAME sortKey/sortDesc state the desktop table's
// column headers use, not a second copy of it.
describe('OpportunityLedger mobile sort control', () => {
  it('defaults to date descending and flips order when the same key is tapped again', async () => {
    render(OpportunityLedger, { props: { records: RECORDS } });

    expect(cardOrder()).toEqual(['PB-B', 'PB-C', 'PB-A']); // newest generated_at first

    await fireEvent.click(screen.getByTestId('ledger-sort-date'));
    expect(cardOrder()).toEqual(['PB-A', 'PB-C', 'PB-B']); // ascending after the second tap
  });

  it('switching the sort key resorts the card list', async () => {
    render(OpportunityLedger, {
      props: {
        records: [
          record({ id: 'a', playbook_id: 'PB-A', generated_at: '2026-08-01T00:00:00', outcome_if_taken: 10 }),
          record({ id: 'b', playbook_id: 'PB-B', generated_at: '2026-08-02T00:00:00', outcome_if_taken: 90 }),
        ],
      },
    });

    await fireEvent.click(screen.getByTestId('ledger-sort-outcome'));
    expect(cardOrder()).toEqual(['PB-B', 'PB-A']); // outcome descending: 90 before 10
  });
});
