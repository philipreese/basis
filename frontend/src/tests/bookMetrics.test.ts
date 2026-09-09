/// <reference types="vitest/globals" />

import { fmtStressCheck, fmtBenchmarkCheck, gateCells } from '../lib/bookMetrics';
import type { LiveGateChecklist } from '../lib/api';

type StressCheck = LiveGateChecklist['stress_episode_check'];
type BenchmarkCheck = LiveGateChecklist['benchmark_check'];

function stress(overrides: Partial<StressCheck> = {}): StressCheck {
  return {
    window_start: '2026-04-01',
    window_end: '2026-08-18',
    peak_vix_close: 30,
    max_spy_drawdown_pct: 6.25,
    episode_dates: 1,
    episode_while_position_open: true,
    episode_while_deployed: false,
    deployment_fraction_required: 0.5,
    normal_deployment: 520,
    required_deployment: 260,
    episode_deployment: 200,
    max_adverse_excursion: 300,
    ok: false,
    ...overrides,
  };
}

function benchmark(overrides: Partial<BenchmarkCheck> = {}): BenchmarkCheck {
  return {
    window_start: '2026-04-01',
    window_end: '2026-08-18',
    book_return_pct: 0.5,
    spy_return_pct: -0.2,
    spy_start_date: '2026-04-01',
    spy_end_date: '2026-08-18',
    ok: true,
    ...overrides,
  };
}

function checklist(overrides: Partial<LiveGateChecklist> = {}): LiveGateChecklist {
  return {
    closed_trades: 10,
    closed_trades_required: 30,
    trades_ok: false,
    months_elapsed: 1.5,
    months_required: 3,
    months_ok: false,
    breaches: 0,
    breaches_ok: true,
    era_start: '2026-04-01',
    expectancy_after_haircut: 12.5,
    expectancy_se: 3.1,
    expectancy_ok: true,
    stress_episode_ok: false,
    stress_episode_check: stress(),
    benchmark_ok: true,
    benchmark_check: benchmark(),
    additional_conditions: [
      { key: 'stress_episode_observed', label: 'stress episode', status: 'fail', detail: 'd1' },
      { key: 'beats_spy_benchmark', label: 'beats SPY', status: 'ok', detail: 'd2' },
      { key: 'beats_same_engine_baseline', label: 'beats baseline', status: 'not_yet_evaluated', detail: 'd3' },
    ],
    tail_magnitude_check: { largest_adverse_move: 100, multiplier: 3, hypothetical_tail_loss: 300, informational: true },
    eligible: false,
    as_raced_config_hash: 'abc12345',
    ...overrides,
  };
}

// #215: the supporting-numbers line under the gate cells must show the
// book's own exposure through the episode session against the $ bar the
// verdict used (#738: held ≠ exposed), render the bare overlap so the two
// readings can be seen to disagree, and read as "no episode" for a calm
// window rather than inventing zeros.
describe('fmtStressCheck', () => {
  it('renders the triggers, the deployment vs the bar, the held-but-under-deployed tag and the excursion', () => {
    expect(fmtStressCheck(stress())).toBe(
      'VIX 30.00 · SPY dd 6.25% · 1 episode day · $200.00 deployed, needs ≥$260.00 (½ of $520.00) · held, under-deployed · MAE −$300.00',
    );
  });

  it('omits the tag on a pass and says not held when no position covered the session', () => {
    expect(fmtStressCheck(stress({ episode_while_deployed: true, ok: true, episode_deployment: 520 }))).toBe(
      'VIX 30.00 · SPY dd 6.25% · 1 episode day · $520.00 deployed, needs ≥$260.00 (½ of $520.00) · MAE −$300.00',
    );
    expect(fmtStressCheck(stress({ episode_while_position_open: false, episode_deployment: 0, max_adverse_excursion: null }))).toBe(
      'VIX 30.00 · SPY dd 6.25% · 1 episode day · $0.00 deployed, needs ≥$260.00 (½ of $520.00) · not held',
    );
  });

  it('reads as no episode for a calm window, with dashes for missing series', () => {
    expect(fmtStressCheck(stress({ peak_vix_close: 18, max_spy_drawdown_pct: null, episode_dates: 0 }))).toBe(
      'VIX 18.00 · SPY dd — · no episode',
    );
  });

  it('keeps the precision the verdict was taken at, so 24.96 never reads as 25.0', () => {
    expect(fmtStressCheck(stress({ peak_vix_close: 24.96, max_spy_drawdown_pct: 4.99, episode_dates: 0 }))).toBe(
      'VIX 24.96 · SPY dd 4.99% · no episode',
    );
  });

  it('says no index data when neither series exists in the window', () => {
    expect(fmtStressCheck(stress({ peak_vix_close: null, max_spy_drawdown_pct: null, episode_dates: 0 }))).toBe(
      'VIX — · SPY dd — · no index data in window',
    );
  });

  it('pluralizes episode days and omits the excursion when no marks exist', () => {
    expect(fmtStressCheck(stress({ episode_dates: 3, max_adverse_excursion: null }))).toBe(
      'VIX 30.00 · SPY dd 6.25% · 3 episode days · $200.00 deployed, needs ≥$260.00 (½ of $520.00) · held, under-deployed',
    );
  });
});

describe('fmtBenchmarkCheck', () => {
  it('renders signed returns side by side', () => {
    expect(fmtBenchmarkCheck(benchmark())).toBe('book +0.50% vs SPY -0.20%');
  });

  it('dashes whichever side is unavailable instead of showing a zero', () => {
    expect(fmtBenchmarkCheck(benchmark({ book_return_pct: null }))).toBe('book — vs SPY -0.20%');
    expect(fmtBenchmarkCheck(benchmark({ spy_return_pct: null }))).toBe('book +0.50% vs SPY —');
  });
});

// #215: a computed row whose inputs are missing is fail-closed for
// eligibility but must not render as an ordinary "✗ tested and lost".
describe('gateCells for the computed ADR-0010 rows', () => {
  it('renders an evaluated fail as an ordinary fail', () => {
    const cells = gateCells(checklist());
    expect(cells.find((c) => c.label.includes('stress episode'))).toMatchObject({ label: '✗ stress episode', status: 'fail' });
    expect(cells.find((c) => c.label.includes('beats SPY'))).toMatchObject({ label: '✓ beats SPY', status: 'ok' });
    expect(cells.find((c) => c.label.includes('beats baseline'))).toMatchObject({ label: 'beats baseline …', status: 'pending' });
  });

  it('renders a stress row with no index_history in the window as no data, not a fail', () => {
    const g = checklist({ stress_episode_check: stress({ peak_vix_close: null, max_spy_drawdown_pct: null, episode_dates: 0 }) });
    expect(gateCells(g).find((c) => c.label.includes('stress episode'))).toMatchObject({
      label: 'stress episode (no data)',
      status: 'nodata',
    });
  });

  it('renders a benchmark row missing either return as no data', () => {
    const g = checklist({
      benchmark_check: benchmark({ spy_return_pct: null, ok: false }),
      additional_conditions: [{ key: 'beats_spy_benchmark', label: 'beats SPY', status: 'fail', detail: 'd' }],
    });
    expect(gateCells(g)[4]).toMatchObject({ label: 'beats SPY (no data)', status: 'nodata' });
  });
});
