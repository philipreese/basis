/// <reference types="vitest/globals" />

import { fmtStressCheck, fmtBenchmarkCheck } from '../lib/bookMetrics';
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
    episode_while_deployed: true,
    deployment_fraction_required: 0.5,
    normal_deployment: 520,
    episode_deployment: 200,
    max_adverse_excursion: 300,
    ok: false,
    ...overrides,
  };
}

// #215: the supporting-numbers line under the gate cells must show the
// book's own exposure on the episode (#738: held ≠ exposed), not just the
// market-wide trigger — and must read as "no episode" for a calm window
// rather than inventing zeros.
describe('fmtStressCheck', () => {
  it('renders the triggers, the deployment ratio and the informational excursion', () => {
    expect(fmtStressCheck(stress())).toBe('VIX 30.0 · SPY dd 6.3% · 1 episode day · $200/$520 deployed · MAE −$300');
  });

  it('reads as no episode for a calm window, with dashes for missing series', () => {
    expect(fmtStressCheck(stress({ peak_vix_close: 18, max_spy_drawdown_pct: null, episode_dates: 0 }))).toBe(
      'VIX 18.0 · SPY dd — · no episode',
    );
  });

  it('pluralizes episode days and omits the excursion when no marks exist', () => {
    expect(fmtStressCheck(stress({ episode_dates: 3, max_adverse_excursion: null }))).toBe(
      'VIX 30.0 · SPY dd 6.3% · 3 episode days · $200/$520 deployed',
    );
  });
});

describe('fmtBenchmarkCheck', () => {
  const check = (overrides: Partial<BenchmarkCheck> = {}): BenchmarkCheck => ({
    window_start: '2026-04-01',
    window_end: '2026-08-18',
    book_return_pct: 0.5,
    spy_return_pct: -0.2,
    spy_start_date: '2026-04-01',
    spy_end_date: '2026-08-18',
    ok: true,
    ...overrides,
  });

  it('renders signed returns side by side', () => {
    expect(fmtBenchmarkCheck(check())).toBe('book +0.50% vs SPY -0.20%');
  });

  it('dashes whichever side is unavailable instead of showing a zero', () => {
    expect(fmtBenchmarkCheck(check({ book_return_pct: null }))).toBe('book — vs SPY -0.20%');
    expect(fmtBenchmarkCheck(check({ spy_return_pct: null }))).toBe('book +0.50% vs SPY —');
  });
});
