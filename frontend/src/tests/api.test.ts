import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getPortfolioConfig, getPositions, runEveningScan } from '../lib/api';

describe('API Client Tests', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('getPortfolioConfig fetches correctly', async () => {
    const mockConfig = {
      account: { total_nav: 10000, broker: 'Test' },
      risk_profile: {},
      portfolio_greek_limits: {}
    };

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => mockConfig,
    } as Response);

    const res = await getPortfolioConfig();
    expect(fetchSpy).toHaveBeenCalledWith('/api/portfolio/config');
    expect(res.account.total_nav).toBe(10000);
  });

  it('getPositions fetches correctly', async () => {
    const mockPositions = [{ id: 'test_pos', underlying: 'SPY' }];

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => mockPositions,
    } as Response);

    const res = await getPositions();
    expect(fetchSpy).toHaveBeenCalledWith('/api/positions');
    expect(res[0].underlying).toBe('SPY');
  });

  it('runEveningScan calls the endpoint with no query string by default', async () => {
    const mockResponse = { ran: true, state: { last_scan_at: '', last_scan_date: '', p1_count: 0, p2_count: 0, eligible_candidate_count: 0, market_fetch_status: 'OK', position_refresh_status: 'OK' } };
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => mockResponse,
    } as Response);

    const res = await runEveningScan();
    expect(fetchSpy).toHaveBeenCalledWith('/api/session/evening-scan', { method: 'POST' });
    expect(res.ran).toBe(true);
  });

  it('runEveningScan(true) appends ?force=true', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ ran: true, state: {} }),
    } as Response);

    await runEveningScan(true);
    expect(fetchSpy).toHaveBeenCalledWith('/api/session/evening-scan?force=true', { method: 'POST' });
  });

  it('runEveningScan throws using the response detail on failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false,
      json: async () => ({ detail: 'boom' }),
    } as Response);

    await expect(runEveningScan()).rejects.toThrow('boom');
  });
});
