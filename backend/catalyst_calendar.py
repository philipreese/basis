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

# FOMC decision days (second day of each two-day meeting).
FOMC_DATES: tuple[str, ...] = (
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",
    "2026-06-17",
    "2026-07-29",
    "2026-09-16",
    "2026-10-28",
    "2026-12-09",
    "2027-01-27",
    "2027-03-17",
    "2027-04-28",
    "2027-06-16",
    "2027-07-28",
    "2027-09-15",
    "2027-10-27",
    "2027-12-08",
)

# CPI release days (BLS, ~8:30 ET mid-month). 2026 verified against the
# posted BLS schedule 2026-08-18; 2027 projects the second-week pattern.
CPI_DATES: tuple[str, ...] = (
    "2026-01-13",
    "2026-02-13",
    "2026-03-11",
    "2026-04-10",
    "2026-05-12",
    "2026-06-10",
    "2026-07-14",
    "2026-08-12",
    "2026-09-11",
    "2026-10-14",
    "2026-11-10",
    "2026-12-10",
    "2027-01-13",
    "2027-02-10",
    "2027-03-10",
    "2027-04-13",
    "2027-05-12",
    "2027-06-10",
    "2027-07-13",
    "2027-08-11",
    "2027-09-14",
    "2027-10-13",
    "2027-11-10",
    "2027-12-10",
)

# Entries older than this many days are pruned from catalyst_dates — the
# catalyst exit rule (catalyst_exit_days_after, max 5) is long expired.
PRUNE_AFTER_DAYS = 30

# Days of seeded coverage below which the digest flags the calendar stale.
CALENDAR_HORIZON_DAYS = 60


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


def catalyst_calendar_stale(today: datetime.date) -> bool:
    """True when seeded coverage ends within the horizon — extend the
    FOMC/CPI lists before the EVENT_CATALYST input silently dries up."""
    last = max(datetime.date.fromisoformat(d) for d in (*FOMC_DATES, *CPI_DATES))
    return last < today + datetime.timedelta(days=CALENDAR_HORIZON_DAYS)
