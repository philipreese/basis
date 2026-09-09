import type { LiveGateChecklist, TailHedgeMetrics } from './api';

export type GateCellStatus = 'ok' | 'fail' | 'pending' | 'nodata';
export type GateCell = { label: string; status: GateCellStatus; title?: string };

// #215: a computed row whose INPUTS are missing (no index_history in the
// window; no closed trades or fewer than two SPY closes for the
// benchmark) is fail-closed in the backend — eligible stays false — but
// must not render as "✗ tested and lost". The converse of the #655
// principle below: the label says the row could not be evaluated.
function stressHasNoData(g: LiveGateChecklist): boolean {
  const c = g.stress_episode_check;
  return c.peak_vix_close === null && c.max_spy_drawdown_pct === null;
}
function benchmarkHasNoData(g: LiveGateChecklist): boolean {
  const c = g.benchmark_check;
  return c.book_return_pct === null || c.spy_return_pct === null;
}

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
  const additional: GateCell[] = g.additional_conditions.map((c) => {
    const noData =
      c.status === 'fail' &&
      ((c.key === 'stress_episode_observed' && stressHasNoData(g)) || (c.key === 'beats_spy_benchmark' && benchmarkHasNoData(g)));
    return {
      label:
        c.status === 'ok' ? `✓ ${c.label}` : c.status === 'not_yet_evaluated' ? `${c.label} …` : noData ? `${c.label} (no data)` : `✗ ${c.label}`,
      status: c.status === 'not_yet_evaluated' ? 'pending' : noData ? 'nodata' : c.status,
      title: c.detail || undefined,
    };
  });
  return [...base, ...additional];
}

export const gateCellClass: Record<GateCellStatus, string> = {
  ok: 'bg-ctp-green/15 text-ctp-green',
  fail: 'bg-ctp-surface0 text-ctp-overlay0',
  pending: 'bg-ctp-yellow/10 text-ctp-yellow border border-dashed border-ctp-yellow/40',
  nodata: 'bg-ctp-surface0 text-ctp-overlay0 border border-dashed border-ctp-overlay0/40',
};

export const fmtPct = (v: number | null): string => (v === null ? '—' : `${(v * 100).toFixed(0)}%`);

// #215: the ADR-0010 computed rows' supporting numbers, one compact line
// under the gate cells. Stress: peak VIX and deepest SPY drawdown in the
// book's gate window (2 dp — the precision the verdict was taken at, so a
// 24.96 never renders as "25.0" beside "no episode"), then the book's own
// exposure through the episode session against the $ bar the verdict used
// (#738: held ≠ exposed — the bare overlap renders as "held, under-
// deployed" so the two readings can be seen to disagree; the max adverse
// excursion is informational). Benchmark: haircut-net realized return on
// basis vs SPY.
export function fmtStressCheck(c: LiveGateChecklist['stress_episode_check']): string {
  const vix = c.peak_vix_close === null ? 'VIX —' : `VIX ${c.peak_vix_close.toFixed(2)}`;
  const spy = c.max_spy_drawdown_pct === null ? 'SPY dd —' : `SPY dd ${c.max_spy_drawdown_pct.toFixed(2)}%`;
  if (c.peak_vix_close === null && c.max_spy_drawdown_pct === null) return `${vix} · ${spy} · no index data in window`;
  if (c.episode_dates === 0) return `${vix} · ${spy} · no episode`;
  const exposure =
    c.episode_deployment === null
      ? ''
      : ` · $${c.episode_deployment.toFixed(2)} deployed, needs ≥$${c.required_deployment.toFixed(2)} (½ of $${c.normal_deployment.toFixed(2)})`;
  const held = c.episode_while_deployed ? '' : c.episode_while_position_open ? ' · held, under-deployed' : ' · not held';
  const mae = c.max_adverse_excursion === null ? '' : ` · MAE −$${c.max_adverse_excursion.toFixed(2)}`;
  return `${vix} · ${spy} · ${c.episode_dates} episode day${c.episode_dates === 1 ? '' : 's'}${exposure}${held}${mae}`;
}

export function fmtBenchmarkCheck(c: LiveGateChecklist['benchmark_check']): string {
  const signed = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
  const book = c.book_return_pct === null ? 'book —' : `book ${signed(c.book_return_pct)}`;
  const spy = c.spy_return_pct === null ? 'SPY —' : `SPY ${signed(c.spy_return_pct)}`;
  return `${book} vs ${spy}`;
}

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
