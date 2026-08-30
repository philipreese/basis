import type { LiveGateChecklist, TailHedgeMetrics } from './api';

export type GateCellStatus = 'ok' | 'fail' | 'pending';
export type GateCell = { label: string; status: GateCellStatus; title?: string };

// #655: the original ADR-0006 four render ok/fail as before; the
// ADR-0010 conditions (additional_conditions) add a THIRD, visually
// distinct 'pending' state — not_yet_evaluated must never look like a
// pass (green) or blend into an ordinary fail (the existing neutral
// fail styling), or an operator scanning the row could read a
// materially weaker standard as the real ADR-0010 bar.
export function gateCells(g: LiveGateChecklist): GateCell[] {
  const base: GateCell[] = [
    { label: g.trades_ok ? '✓ trades' : `${g.closed_trades}/${g.closed_trades_required} trades`, status: g.trades_ok ? 'ok' : 'fail' },
    { label: g.months_ok ? '✓ 3mo' : `${g.months_elapsed.toFixed(1)}/${g.months_required}mo`, status: g.months_ok ? 'ok' : 'fail' },
    { label: g.breaches_ok ? '✓ 0 breach' : `${g.breaches} breach`, status: g.breaches_ok ? 'ok' : 'fail' },
    {
      // #656: the bar is expectancy − 1·SE ≥ 0, not a point estimate —
      // the interval renders even on a pass, so the margin is always
      // visible, not just the fact of clearing it.
      label: g.expectancy_after_haircut === null
        ? 'exp —'
        : `${g.expectancy_ok ? '✓ ' : ''}exp ${fmtInterval(g.expectancy_after_haircut, g.expectancy_se)}`,
      status: g.expectancy_ok ? 'ok' : 'fail',
      title: 'expectancy ± 1 standard error, after the $5/contract haircut',
    },
  ];
  const additional: GateCell[] = g.additional_conditions.map((c) => ({
    label: c.status === 'ok' ? `✓ ${c.label}` : c.status === 'not_yet_evaluated' ? `${c.label} …` : `✗ ${c.label}`,
    status: c.status === 'not_yet_evaluated' ? 'pending' : c.status,
    title: c.detail || undefined,
  }));
  return [...base, ...additional];
}

export const gateCellClass: Record<GateCellStatus, string> = {
  ok: 'bg-ctp-green/15 text-ctp-green',
  fail: 'bg-ctp-surface0 text-ctp-overlay0',
  pending: 'bg-ctp-yellow/10 text-ctp-yellow border border-dashed border-ctp-yellow/40',
};

export const fmtPct = (v: number | null): string => (v === null ? '—' : `${(v * 100).toFixed(0)}%`);

// ADR-0012 (#772): the tail-hedge sleeve is judged on convexity, never
// expectancy — a book carrying tail_hedge_metrics renders these THREE
// numbers in place of the standard win-rate/expectancy cells, and its
// Live Gate row still shows (permanently ineligible, per the backend).
export const fmtBleed = (v: number | null): string => (v === null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%/mo`);
export const fmtStress = (m: TailHedgeMetrics): string =>
  m.stress_episode_status === 'no_episode_yet'
    ? 'no episode yet'
    : `${m.stress_episode_payoff! >= 0 ? '+' : ''}${m.stress_episode_payoff!.toFixed(0)}`;
export const fmtContribution = (v: number | null): string => (v === null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(0)}`);
// #656: expectancy renders as an interval, x ± se, everywhere it appears
// — se is None below n=2 (undefined, not zero), so the ± term is omitted
// rather than shown as "± 0.00", which would misstate a real trade as
// having no uncertainty.
export const fmtInterval = (v: number | null, se: number | null): string =>
  v === null ? '—' : se === null ? v.toFixed(2) : `${v.toFixed(2)} ± ${se.toFixed(2)}`;
