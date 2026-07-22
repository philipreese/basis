import { describe, it, expect } from 'vitest';
import { summarizeEveningScan } from '../lib/eveningScanSummary';
import type { EveningScanResponse } from '../lib/api';

function makeResponse(overrides: Partial<EveningScanResponse['state']> = {}, ran = true): EveningScanResponse {
  return {
    ran,
    state: {
      last_scan_at: '2026-07-22T18:03:00+00:00',
      last_scan_date: '2026-07-22',
      p1_count: 0,
      p2_count: 0,
      eligible_candidate_count: 0,
      market_fetch_status: 'OK',
      position_refresh_status: 'OK',
      ...overrides,
    },
  };
}

describe('summarizeEveningScan', () => {
  it('combines nonzero counts into one message when the scan ran', () => {
    const result = summarizeEveningScan(makeResponse({ p1_count: 2, p2_count: 1, eligible_candidate_count: 3 }));
    expect(result.level).toBe('success');
    expect(result.message).toContain('2 positions need immediate attention');
    expect(result.message).toContain('1 to review');
    expect(result.message).toContain('3 opportunities eligible');
  });

  it('uses singular phrasing for a count of exactly 1', () => {
    const result = summarizeEveningScan(makeResponse({ p1_count: 1, eligible_candidate_count: 1 }));
    expect(result.message).toContain('1 position needs');
    expect(result.message).toContain('1 opportunity eligible');
  });

  it('reports nothing needs attention when all counts are zero', () => {
    const result = summarizeEveningScan(makeResponse());
    expect(result.level).toBe('success');
    expect(result.message).toContain('Nothing needs attention right now.');
  });

  it('flags degraded data when market fetch was not OK', () => {
    const result = summarizeEveningScan(makeResponse({ market_fetch_status: 'UNCONFIGURED' }));
    expect(result.level).toBe('info');
    expect(result.message).toContain('saved market data');
  });

  it('reports the already-ran-today variant when the scan did not run', () => {
    const result = summarizeEveningScan(makeResponse({}, false));
    expect(result.level).toBe('info');
    expect(result.message).toContain('already ran today at');
  });
});
