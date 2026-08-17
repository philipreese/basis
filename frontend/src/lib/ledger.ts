/** Pure ledger view logic (#8) — extracted so vitest can pin the rules. */
import type { OpportunityRecord } from './api';

export type StatusFilter = 'all' | 'accepted' | 'bypassed';
export type SortKey = 'date' | 'outcome';

export function filterAndSort(
  records: OpportunityRecord[],
  statusFilter: StatusFilter,
  sortKey: SortKey,
  sortDesc: boolean,
): OpportunityRecord[] {
  const filtered = records.filter(r =>
    statusFilter === 'all' ? true : statusFilter === 'accepted' ? r.accepted : !r.accepted,
  );
  const sorted = [...filtered].sort((a, b) => {
    if (sortKey === 'date') return a.generated_at.localeCompare(b.generated_at);
    // outcome sort: unknown outcomes sink to the bottom regardless of direction
    const ao = a.outcome_if_taken;
    const bo = b.outcome_if_taken;
    if (ao == null && bo == null) return 0;
    if (ao == null) return sortDesc ? -1 : 1;
    if (bo == null) return sortDesc ? 1 : -1;
    return ao - bo;
  });
  return sortDesc ? sorted.reverse() : sorted;
}

export interface OverrideStats {
  bypassed: number;
  known: number; // bypassed with a recorded counterfactual outcome
  missedWins: number;
  total: number; // sum of counterfactual outcomes; negative = bypassing avoided losses
}

/** The value of human override: what the bypassed opportunities would have done. */
export function computeOverrideStats(records: OpportunityRecord[]): OverrideStats {
  const bypassed = records.filter(r => !r.accepted);
  const known = bypassed.filter(r => r.outcome_if_taken != null);
  const missedWins = known.filter(r => (r.outcome_if_taken ?? 0) > 0);
  const total = known.reduce((sum, r) => sum + (r.outcome_if_taken ?? 0), 0);
  return { bypassed: bypassed.length, known: known.length, missedWins: missedWins.length, total };
}
