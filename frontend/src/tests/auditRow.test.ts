import { describe, it, expect } from 'vitest';
import { auditRowText } from '../lib/auditRow';
import type { AuditEvent } from '../lib/api';

function event(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    id: 1,
    run_at: '2026-09-09T22:45:16+00:00',
    book_id: 'B07',
    event_type: 'ENTRY_NOT_TAKEN',
    actor: 'executor',
    payload: { stage: 'ineligible', reasons: ['REGIME GATE: IRON_CONDOR is not in the EVENT_CATALYST playbook matrix'] },
    urgent: false,
    ...overrides,
  };
}

describe('auditRowText', () => {
  it('leads with the identity the payload does not carry', () => {
    const text = auditRowText(event());
    const [header] = text.split('\n');
    expect(header).toBe('2026-09-09T22:45:16+00:00  ENTRY_NOT_TAKEN  B07  by executor');
  });

  it('includes the full payload as indented JSON', () => {
    const text = auditRowText(event());
    expect(text).toContain('"stage": "ineligible"');
    expect(text).toContain('EVENT_CATALYST playbook matrix');
    // Round-trips: the pasted payload is still machine-readable.
    expect(JSON.parse(text.slice(text.indexOf('\n') + 1))).toEqual(event().payload);
  });

  it('prefers the book label the row actually displays', () => {
    const text = auditRowText(event({ book_label: 'B32 — XSP 684 long put (Nov 20 26)' }));
    expect(text).toContain('B32 — XSP 684 long put (Nov 20 26)');
  });

  it('omits the book segment entirely for a fleet-wide row', () => {
    const text = auditRowText(event({ book_id: null, event_type: 'BOOK_ORDER_SHUFFLED' }));
    const [header] = text.split('\n');
    expect(header).toBe('2026-09-09T22:45:16+00:00  BOOK_ORDER_SHUFFLED  by executor');
  });
});
