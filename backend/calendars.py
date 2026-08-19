"""calendars.py — the operator-maintained static calendars (#187).

Three domain modules consume dates that only a human maintains: projected
ex-dividend dates (assignment defense, #130), FOMC/CPI schedules (catalyst
merging, #131), and NYSE full closures (the holiday guard, #68). The tables
and the one staleness rule live here; the domain logic stays with its
concern. One digest question — "is any calendar about to run out?" — has
one interface: stale_calendars().

All dates are projections until confirmed against the published schedules
(SSGA/iShares distributions, the Fed and BLS calendars, the NYSE holiday
page). The staleness horizon flags coverage in the digest before it lapses.
"""

import datetime

# Projected ex-dividend dates (ISO). SPY: third Friday of Mar/Jun/Sep/Dec.
# IWM: quarterly, mid-month (verified 2026-08-18: September is 09-15 per
# iShares — NOT month-end). TLT: monthly, first business day.
EX_DIV_CALENDAR: dict[str, tuple[str, ...]] = {
    # SPY 2026-09-18 confirmed against SSGA's distribution schedule
    # (2026-08-18); June was actually 06-18.
    "SPY": (
        "2026-03-20",
        "2026-06-18",
        "2026-09-18",
        "2026-12-18",
        "2027-03-19",
        "2027-06-18",
        "2027-09-17",
        "2027-12-17",
    ),
    "IWM": (
        "2026-03-17",
        "2026-06-16",
        "2026-09-15",
        "2026-12-15",
        "2027-03-16",
        "2027-06-15",
        "2027-09-14",
        "2027-12-14",
    ),
    "TLT": (
        "2026-09-01",
        "2026-10-01",
        "2026-11-02",
        "2026-12-01",
        "2027-01-04",
        "2027-02-01",
        "2027-03-01",
        "2027-04-01",
        "2027-05-03",
        "2027-06-01",
        "2027-07-01",
        "2027-08-02",
        "2027-09-01",
        "2027-10-01",
        "2027-11-01",
        "2027-12-01",
    ),
}

# DELIBERATELY ABSENT: election days (e.g. the 2026-11-03 midterms). Decided
# 2026-08-19 (#233): an election inside a Live Gate window is the stress
# episode ADR-0010 requires — adding it as a MAJOR catalyst would block
# entries across it and make the books hold their fewest positions exactly
# when the experiment most needs exposure. The regime engine and IVR gates
# are the general-purpose defense; do not "helpfully" add election dates.

# FOMC decision days (second day of each two-day meeting; the Fed posts
# them years ahead). 2027 dates are projections until the Fed publishes.
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

# Full-day US equity market closures. Verify against the NYSE published
# calendar as each year posts; observed dates included where the holiday
# falls on a weekend. Half days (day after Thanksgiving, Christmas Eve)
# still trade — the evening cadence runs after any close.
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

# Days of remaining coverage below which a calendar is flagged stale.
CALENDAR_HORIZON_DAYS = 60


def is_trading_day(day: datetime.date) -> bool:
    """Weekday and not a full-day closure. On holidays the executor writes
    its heartbeat and exits, and the gateway lifecycle never launches (#68)."""
    return day.weekday() < 5 and day.isoformat() not in MARKET_HOLIDAYS


def _ends_within_horizon(dates: tuple[str, ...] | frozenset[str], horizon: datetime.date) -> bool:
    return max(datetime.date.fromisoformat(d) for d in dates) < horizon


def stale_calendars(today: datetime.date) -> list[str]:
    """Labels of every calendar whose coverage ends within the horizon —
    the operator must extend its table before it silently runs out."""
    horizon = today + datetime.timedelta(days=CALENDAR_HORIZON_DAYS)
    stale = [
        f"ex-div {symbol}" for symbol, dates in sorted(EX_DIV_CALENDAR.items()) if _ends_within_horizon(dates, horizon)
    ]
    if _ends_within_horizon((*FOMC_DATES, *CPI_DATES), horizon):
        stale.append("FOMC/CPI catalysts")
    if _ends_within_horizon(MARKET_HOLIDAYS, horizon):
        stale.append("market holidays")
    return stale
