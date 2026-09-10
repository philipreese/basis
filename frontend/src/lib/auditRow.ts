import type { AuditEvent } from './api';

/**
 * One audit row as plain text for the clipboard (#1011).
 *
 * The header line carries the identity the payload does not: when, what,
 * which book, and who wrote it. Pasting a bare payload into a conversation
 * loses all four, which is the whole reason a row gets copied out of the
 * console in the first place. `book_label` is preferred over the raw id
 * because that is what the row itself shows — a copy that disagrees with
 * the screen it came from is worse than no copy.
 */
export function auditRowText(ev: AuditEvent): string {
  const header = [ev.run_at, ev.event_type, ev.book_label ?? ev.book_id, `by ${ev.actor}`]
    .filter((part): part is string => Boolean(part))
    .join('  ');
  return `${header}\n${JSON.stringify(ev.payload, null, 2)}`;
}
