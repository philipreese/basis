import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getPortfolioConfig, getPositions } from '../lib/api';

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
});
