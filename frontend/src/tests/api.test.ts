import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getPortfolioConfig,
  getPositions,
  getAuditEvents,
  updateTradingControl,
  ackFlexDiscrepancies,
  getEvidenceVerdict,
} from '../lib/api';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** openapi-fetch hands `fetch` a Request object — capture it for assertions. */
function lastRequest(spy: { mock: { calls: unknown[][] } }): Request {
  const calls = spy.mock.calls;
  return calls[calls.length - 1][0] as Request;
}

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
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(mockConfig));

    const res = await getPortfolioConfig();
    const req = lastRequest(fetchSpy);
    expect(req.url).toContain('/api/portfolio/config');
    expect(req.method).toBe('GET');
    expect(res.account.total_nav).toBe(10000);
  });

  it('getPositions fetches correctly', async () => {
    const mockPositions = [{ id: 'test_pos', underlying: 'SPY' }];
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(mockPositions));

    const res = await getPositions();
    expect(lastRequest(fetchSpy).url).toContain('/api/positions');
    expect(res[0].underlying).toBe('SPY');
  });

  it('getEvidenceVerdict fetches correctly', async () => {
    const mockVerdict = { verdict: 'insufficient', closed_trades: 0, policy_version: 1 };
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(mockVerdict));

    const res = await getEvidenceVerdict();
    expect(lastRequest(fetchSpy).url).toContain('/api/analysis/evidence-verdict');
    expect(res.verdict).toBe('insufficient');
  });

  it('getAuditEvents serializes query filters', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse([]));

    await getAuditEvents({ book_id: 'B01', date: '2026-08-18', limit: 50 });
    const url = lastRequest(fetchSpy).url;
    expect(url).toContain('/api/audit-events');
    expect(url).toContain('book_id=B01');
    expect(url).toContain('date=2026-08-18');
    expect(url).toContain('limit=50');
  });

  it('ackFlexDiscrepancies posts exec_ids and reason', async () => {
    const mockResult = { acked: ['exec1'], already_acked: [] };
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(mockResult));

    const res = await ackFlexDiscrepancies(['exec1'], 'explained via cash adjust');
    const req = lastRequest(fetchSpy);
    expect(req.url).toContain('/api/resolution/flex-ack');
    expect(req.method).toBe('POST');
    const body = await req.clone().json();
    expect(body).toEqual({ exec_ids: ['exec1'], reason: 'explained via cash adjust' });
    expect(res.acked).toEqual(['exec1']);
  });

  it('surfaces the FastAPI error detail on failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ detail: 'Sentinel HALT file present' }, 409),
    );

    await expect(updateTradingControl('GLOBAL', 'ACTIVE', 'trying anyway'))
      .rejects.toThrow('Sentinel HALT file present');
  });

  it('falls back to a generic message when the error body is not detail-shaped', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ nope: true }, 500));

    await expect(getPositions()).rejects.toThrow('Failed to fetch positions');
  });

  it('flattens a FastAPI 422 validation-error array into readable text (#479)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse(
        {
          detail: [
            { loc: ['body', 'reason'], msg: 'field required', type: 'value_error.missing' },
            { loc: ['body', 'state'], msg: 'not a valid enumeration member', type: 'value_error' },
          ],
        },
        422,
      ),
    );

    await expect(updateTradingControl('GLOBAL', 'ACTIVE', 'x')).rejects.toThrow(
      'reason: field required; state: not a valid enumeration member',
    );
  });

  it('falls back to a generic message for an empty or malformed detail array', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ detail: [] }, 422));

    await expect(getPositions()).rejects.toThrow('Failed to fetch positions');
  });
});
