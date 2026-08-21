import { describe, it, expect } from 'vitest';
import { formatDollar, formatPct, formatDte, formatDate, formatLocalDateTime } from '../lib/formatters';

describe('Formatter Utility Tests', () => {
  describe('formatDollar', () => {
    it('formats positive values correctly with 2 decimal places and $ sign', () => {
      expect(formatDollar(1234.56)).toBe('$1,234.56');
      expect(formatDollar(0)).toBe('$0.00');
      expect(formatDollar(0.5)).toBe('$0.50');
      expect(formatDollar(10000)).toBe('$10,000.00');
    });

    it('formats negative values correctly as -$X.XX', () => {
      expect(formatDollar(-12.34)).toBe('-$12.34');
      expect(formatDollar(-1234.5)).toBe('-$1,234.50');
    });

    it('handles null and undefined', () => {
      expect(formatDollar(null)).toBe('$0.00');
      expect(formatDollar(undefined)).toBe('$0.00');
    });
  });

  describe('formatPct', () => {
    it('formats percentage values with 1 decimal place', () => {
      expect(formatPct(12.34)).toBe('12.3%');
      expect(formatPct(0.5)).toBe('0.5%');
      expect(formatPct(0)).toBe('0.0%');
    });

    it('formats ratios when flag is true', () => {
      expect(formatPct(0.1234, true)).toBe('12.3%');
      expect(formatPct(0.005, true)).toBe('0.5%');
      expect(formatPct(-0.015, true)).toBe('-1.5%');
    });

    it('handles null and undefined', () => {
      expect(formatPct(null)).toBe('0.0%');
      expect(formatPct(undefined)).toBe('0.0%');
    });
  });

  describe('formatDte', () => {
    it('formats DTE to rounded integer with suffix', () => {
      expect(formatDte(21.4)).toBe('21 DTE');
      expect(formatDte(21.6)).toBe('22 DTE');
      expect(formatDte(0)).toBe('0 DTE');
    });

    it('handles null and undefined', () => {
      expect(formatDte(null)).toBe('0 DTE');
      expect(formatDte(undefined)).toBe('0 DTE');
    });
  });

  describe('formatDate', () => {
    it('formats date string YYYY-MM-DD to Month DD YYYY', () => {
      expect(formatDate('2026-06-18')).toBe('June 18 2026');
      expect(formatDate('2026-12-05')).toBe('December 5 2026');
    });

    it('formats ISO date strings using UTC parts to avoid local offset shifting', () => {
      expect(formatDate('2026-06-18T00:00:00Z')).toBe('June 18 2026');
      expect(formatDate('2026-06-18T23:59:59.999Z')).toBe('June 18 2026');
    });

    it('handles Date objects', () => {
      const d = new Date(2026, 5, 18); // June 18 2026 (local index 5)
      expect(formatDate(d)).toBe('June 18 2026');
    });

    it('handles null/undefined and empty values', () => {
      expect(formatDate(null)).toBe('');
      expect(formatDate(undefined)).toBe('');
      expect(formatDate('')).toBe('');
    });
  });

  describe('formatLocalDateTime', () => {
    it('renders a UTC-offset ISO timestamp in the local timezone', () => {
      const iso = '2026-08-20T23:37:04+00:00';
      const d = new Date(iso);
      const pad = (n: number) => String(n).padStart(2, '0');
      const expected = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
      expect(formatLocalDateTime(iso)).toBe(expected);
    });

    it('handles null/undefined/empty', () => {
      expect(formatLocalDateTime(null)).toBe('');
      expect(formatLocalDateTime(undefined)).toBe('');
      expect(formatLocalDateTime('')).toBe('');
    });

    it('falls back to the raw string for an unparseable timestamp', () => {
      expect(formatLocalDateTime('not-a-date')).toBe('not-a-date');
    });
  });
});
