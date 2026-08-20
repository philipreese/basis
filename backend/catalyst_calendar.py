"""catalyst_calendar.py — seeded FOMC/CPI catalyst dates (#131).

V1's EVENT_CATALYST regime, the catalyst entry filters, and the catalyst
exit rules all key off market_state.catalyst_dates — previously a manually
typed field. If nobody typed the next FOMC date, the system confidently
sold premium into it. The safety rule existed but its input was vibes.

This module seeds the two catalysts that are published far in advance:
FOMC meeting dates (the Fed posts them years ahead; the date used is the
decision day) and CPI release dates (BLS publishes the schedule annually).
The nightly refresh merges them into catalyst_dates additively — manual
entries are preserved, never replaced — and prunes entries once they are
well past. The merge is idempotent, so re-running never duplicates.

Dates are projections until confirmed against the Fed/BLS schedules; the
staleness guard flags the calendar in the digest before coverage lapses.
"""

import datetime

from backend.calendars import CPI_DATES, FOMC_DATES

# Entries older than this many days are pruned from catalyst_dates — every
# rule that reads the calendar looks forward, never this far back.
PRUNE_AFTER_DAYS = 30


def seeded_catalysts() -> list[str]:
    """Every seeded catalyst in the parse_catalyst string format."""
    return [f"FOMC:{d}" for d in FOMC_DATES] + [f"CPI:{d}" for d in CPI_DATES]


def _entry_date(entry: str) -> datetime.date | None:
    import re

    match = re.search(r"\d{4}-\d{2}-\d{2}", entry)
    if not match:
        return None
    try:
        return datetime.date.fromisoformat(match.group(0))
    except ValueError:
        return None


def merge_catalysts(existing: list[str], today: datetime.date) -> list[str]:
    """Union of the manual/stored entries and the seeded calendar, pruned of
    long-past dates, sorted by date. Idempotent: string-level dedupe means
    nightly re-merges never grow the list. Undated manual entries are kept
    verbatim (fail-open for the human's notes, they parse as MINOR)."""
    cutoff = today - datetime.timedelta(days=PRUNE_AFTER_DAYS)
    merged = set(existing) | set(seeded_catalysts())
    kept = [e for e in merged if (d := _entry_date(e)) is None or d >= cutoff]
    return sorted(kept, key=lambda e: (_entry_date(e) or datetime.date.max, e))
