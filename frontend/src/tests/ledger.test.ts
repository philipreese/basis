/// <reference types="vitest/globals" />

import { computeOverrideStats, filterAndSort } from '../lib/ledger';
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
  record({ id: 'a', accepted: true, generated_at: '2026-08-01T00:00:00', outcome_if_taken: 120 }),
  record({ id: 'b', accepted: false, generated_at: '2026-08-03T00:00:00', outcome_if_taken: -80, bypass_reason: 'IVR gate' }),
  record({ id: 'c', accepted: false, generated_at: '2026-08-02T00:00:00', outcome_if_taken: 50, bypass_reason: 'regime' }),
  record({ id: 'd', accepted: false, generated_at: '2026-08-04T00:00:00', outcome_if_taken: null, bypass_reason: 'manual' }),
];

describe('filterAndSort', () => {
  it('filters by status', () => {
    expect(filterAndSort(RECORDS, 'accepted', 'date', true).map(r => r.id)).toEqual(['a']);
    expect(filterAndSort(RECORDS, 'bypassed', 'date', true).map(r => r.id)).toEqual(['d', 'b', 'c']);
    expect(filterAndSort(RECORDS, 'all', 'date', true)).toHaveLength(4);
  });

  it('sorts by date in both directions', () => {
    expect(filterAndSort(RECORDS, 'all', 'date', true).map(r => r.id)).toEqual(['d', 'b', 'c', 'a']);
    expect(filterAndSort(RECORDS, 'all', 'date', false).map(r => r.id)).toEqual(['a', 'c', 'b', 'd']);
  });

  it('sorts by outcome with unknown outcomes always last', () => {
    const desc = filterAndSort(RECORDS, 'all', 'outcome', true).map(r => r.id);
    expect(desc).toEqual(['a', 'c', 'b', 'd']); // 120, 50, -80, unknown
    const asc = filterAndSort(RECORDS, 'all', 'outcome', false).map(r => r.id);
    expect(asc).toEqual(['b', 'c', 'a', 'd']); // -80, 50, 120, unknown
  });

  it('does not mutate the input', () => {
    const ids = RECORDS.map(r => r.id);
    filterAndSort(RECORDS, 'all', 'outcome', true);
    expect(RECORDS.map(r => r.id)).toEqual(ids);
  });
});

describe('computeOverrideStats', () => {
  it('summarizes bypassed records with known outcomes', () => {
    const stats = computeOverrideStats(RECORDS);
    expect(stats.bypassed).toBe(3);
    expect(stats.known).toBe(2); // d has no counterfactual
    expect(stats.missedWins).toBe(1); // c would have won
    expect(stats.total).toBe(-30); // -80 + 50 → bypassing avoided $30 net
  });

  it('handles a ledger with no bypasses', () => {
    const stats = computeOverrideStats([record({ accepted: true })]);
    expect(stats).toEqual({ bypassed: 0, known: 0, missedWins: 0, total: 0 });
  });
});
