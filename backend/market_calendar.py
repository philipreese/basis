"""market_calendar.py — US equity market holiday guard (#68).

On a holiday the executor must write its heartbeat and exit without
trading — silent non-operation is only acceptable when announced (design
§3.3). The gateway lifecycle also consults this before launching IB
Gateway at all.

The calendar is a static operator-maintained list of full-day NYSE/CBOE
closures (weekends are computed). Half days (day after Thanksgiving,
Christmas Eve) still trade — the evening cadence runs after any close, so
they need no special handling. Same staleness pattern as the ex-div and
catalyst calendars: the digest flags coverage before it lapses.
"""

import datetime

# Full-day US equity market closures. Verify against the NYSE published
# calendar as each year posts; observed dates included where the holiday
# falls on a weekend.
MARKET_HOLIDAYS: frozenset[str] = frozenset(
    {
        # 2026
        "2026-01-01",  # New Year's Day
        "2026-01-19",  # Martin Luther King Jr. Day
        "2026-02-16",  # Washington's Birthday
        "2026-04-03",  # Good Friday
        "2026-05-25",  # Memorial Day
        "2026-06-19",  # Juneteenth
        "2026-07-03",  # Independence Day (observed)
        "2026-09-07",  # Labor Day
        "2026-11-26",  # Thanksgiving
        "2026-12-25",  # Christmas
        # 2027
        "2027-01-01",  # New Year's Day
        "2027-01-18",  # Martin Luther King Jr. Day
        "2027-02-15",  # Washington's Birthday
        "2027-03-26",  # Good Friday
        "2027-05-31",  # Memorial Day
        "2027-06-18",  # Juneteenth (observed)
        "2027-07-05",  # Independence Day (observed)
        "2027-09-06",  # Labor Day
        "2027-11-25",  # Thanksgiving
        "2027-12-24",  # Christmas (observed)
    }
)

CALENDAR_HORIZON_DAYS = 60


def is_trading_day(day: datetime.date) -> bool:
    return day.weekday() < 5 and day.isoformat() not in MARKET_HOLIDAYS


def market_calendar_stale(today: datetime.date) -> bool:
    """True when the holiday list's last entry is inside the horizon — the
    operator must extend it before the guard silently stops guarding."""
    last = max(datetime.date.fromisoformat(d) for d in MARKET_HOLIDAYS)
    return last < today + datetime.timedelta(days=CALENDAR_HORIZON_DAYS)
