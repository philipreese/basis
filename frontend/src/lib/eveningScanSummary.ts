import type { EveningScanResponse } from './api';

export interface EveningScanSummary {
  message: string;
  level: 'success' | 'info' | 'error';
}

/**
 * Builds a toast-ready summary from an evening-scan response. Pure function
 * so the "what does this mean to the user" logic is independently testable.
 */
export function summarizeEveningScan(response: EveningScanResponse): EveningScanSummary {
  const { state } = response;

  if (!response.ran) {
    const time = new Date(state.last_scan_at).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    return { message: `Evening scan already ran today at ${time} — showing latest saved data.`, level: 'info' };
  }

  const parts: string[] = [];
  if (state.p1_count > 0) parts.push(`${state.p1_count} position${state.p1_count === 1 ? ' needs' : 's need'} immediate attention`);
  if (state.p2_count > 0) parts.push(`${state.p2_count} to review`);
  if (state.eligible_candidate_count > 0) parts.push(`${state.eligible_candidate_count} opportunit${state.eligible_candidate_count === 1 ? 'y' : 'ies'} eligible`);

  const summary = parts.length > 0 ? parts.join(' · ') : 'Nothing needs attention right now.';
  const degraded = state.market_fetch_status !== 'OK';
  const message = degraded
    ? `Evening scan ran with saved market data (Alpaca not configured or fetch failed): ${summary}`
    : `Evening scan complete: ${summary}`;

  return { message, level: degraded ? 'info' : 'success' };
}
