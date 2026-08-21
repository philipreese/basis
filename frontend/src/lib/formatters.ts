/**
 * Formats a numeric value as a dollar currency string with exactly 2 decimal places.
 * Handles negative values correctly (e.g. -12.34 becomes -$12.34).
 */
export function formatDollar(val: number | null | undefined): string {
  if (val === null || val === undefined) return '$0.00';
  const isNegative = val < 0;
  const absVal = Math.abs(val);
  const formatted = absVal.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
  return (isNegative ? '-' : '') + '$' + formatted;
}

/**
 * Formats a numeric value as a percentage with exactly 1 decimal place.
 * Can handle both ratios (e.g. 0.123 -> 12.3%) and raw percentages (e.g. 12.3 -> 12.3%).
 */
export function formatPct(val: number | null | undefined, isRatio = false): string {
  if (val === null || val === undefined) return '0.0%';
  const pct = isRatio ? val * 100 : val;
  return pct.toFixed(1) + '%';
}

/**
 * Formats Days to Expiration (DTE) as an integer with "DTE" suffix.
 */
export function formatDte(val: number | null | undefined): string {
  if (val === null || val === undefined) return '0 DTE';
  return `${Math.round(val)} DTE`;
}

/**
 * Formats a UTC-offset-aware ISO timestamp (e.g. "2026-08-20T23:37:04+00:00",
 * the shape every backend writer emits via `datetime.now(UTC).isoformat()`)
 * as "YYYY-MM-DD HH:MM" in the OPERATOR'S LOCAL timezone (#562 #3).
 *
 * Rendering raw UTC in the console reads as future-dated to an operator west
 * of UTC during an incident — `new Date(iso)` parses the offset correctly as
 * long as the string carries one, so this is a display-only conversion, not
 * a reinterpretation of the underlying instant.
 */
export function formatLocalDateTime(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * Formats a date string or Date object to "Month DD YYYY" format (e.g. June 18 2026).
 * Handles timezone shifts by parsing YYYY-MM-DD explicitly.
 */
export function formatDate(dateVal: string | Date | null | undefined): string {
  if (!dateVal) return '';
  const monthNames = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
  ];

  if (typeof dateVal === 'string') {
    const dateStr = dateVal.trim();
    
    // Match YYYY-MM-DD pattern
    const ymdMatch = dateStr.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (ymdMatch) {
      const year = parseInt(ymdMatch[1], 10);
      const monthIndex = parseInt(ymdMatch[2], 10) - 1;
      const day = parseInt(ymdMatch[3], 10);
      if (monthIndex >= 0 && monthIndex < 12) {
        return `${monthNames[monthIndex]} ${day} ${year}`;
      }
    }
    
    // Try parsing with Date constructor
    const d = new Date(dateStr);
    if (!isNaN(d.getTime())) {
      // Default to UTC values to avoid local timezone shifts for date strings
      const year = d.getUTCFullYear();
      const monthIndex = d.getUTCMonth();
      const day = d.getUTCDate();
      return `${monthNames[monthIndex]} ${day} ${year}`;
    }
    return dateStr;
  }

  // Handle Date object
  if (isNaN(dateVal.getTime())) return '';
  const year = dateVal.getFullYear();
  const monthIndex = dateVal.getMonth();
  const day = dateVal.getDate();
  return `${monthNames[monthIndex]} ${day} ${year}`;
}
