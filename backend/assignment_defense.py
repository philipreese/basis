"""assignment_defense.py — ex-dividend early-assignment prevention (#130).

American-style options on dividend-paying ETFs carry early-assignment risk:
a short call whose extrinsic value is below the upcoming dividend is worth
exercising the night before the ex-date, which would hand the book short
shares — a No-Stock Mandate P1 incident. Reconciliation only *detects* that
(UNEXPECTED_INSTRUMENT); this module *prevents* it, twice:

1. Entry side: any trade spec carrying a SHORT call on a dividend payer
   whose expiration spans an ex-div date is hard-blocked (EX_DIV_ASSIGNMENT).
   Conservative by design — a blocked put-side alternative always exists.
2. Layer A: an ITM short call on a payer within the assignment window of an
   ex-date becomes P1 — CLOSE NOW (the call is the exercise candidate).

XSP is immune (European-style, cash-settled). GLD pays no dividend.

The calendar is static and operator-maintained: projected ex-dates from the
funds' published schedules (SPY/IWM quarterly, TLT monthly). Dates are
estimates until confirmed — the horizon check surfaces staleness in the
nightly digest before the calendar can silently run out.
"""

import datetime

# Projected ex-dividend dates (ISO). SPY: third Friday of Mar/Jun/Sep/Dec.
# IWM: quarterly, typically the final week of the same months. TLT: monthly,
# first business day. Verify against the fund's declared dates as they post.
EX_DIV_CALENDAR: dict[str, tuple[str, ...]] = {
    "SPY": (
        "2026-03-20",
        "2026-06-19",
        "2026-09-18",
        "2026-12-18",
        "2027-03-19",
        "2027-06-18",
        "2027-09-17",
        "2027-12-17",
    ),
    "IWM": (
        "2026-03-25",
        "2026-06-26",
        "2026-09-25",
        "2026-12-24",
        "2027-03-24",
        "2027-06-25",
        "2027-09-24",
        "2027-12-23",
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

# Days of calendar coverage below which the digest flags the calendar stale.
CALENDAR_HORIZON_DAYS = 60

# An ITM short call this many trading days before an ex-date is P1.
ASSIGNMENT_WINDOW_TRADING_DAYS = 3


def _trading_days_between(start: datetime.date, end: datetime.date) -> int:
    """Weekdays in (start, end]. Same holiday-free approximation as the
    catalyst window in regime_variants."""
    days = 0
    d = start
    while d < end:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def ex_div_within(symbol: str, start: datetime.date, end: datetime.date) -> str | None:
    """First calendar ex-date d with start < d <= end, or None."""
    for iso in EX_DIV_CALENDAR.get(symbol, ()):
        d = datetime.date.fromisoformat(iso)
        if start < d <= end:
            return iso
    return None


def stale_calendars(today: datetime.date) -> list[str]:
    """Symbols whose projected calendar ends within the horizon — the
    operator must extend the dates before coverage silently lapses."""
    horizon = today + datetime.timedelta(days=CALENDAR_HORIZON_DAYS)
    return sorted(
        symbol for symbol, dates in EX_DIV_CALENDAR.items() if datetime.date.fromisoformat(dates[-1]) < horizon
    )


def entry_ex_div_block(
    underlying: str,
    has_short_call: bool,
    today: datetime.date,
    expiration: datetime.date,
) -> str | None:
    """Hard-block reason for an entry whose short call would span an ex-date."""
    if not has_short_call or underlying not in EX_DIV_CALENDAR:
        return None
    ex_date = ex_div_within(underlying, today, expiration)
    if ex_date is None:
        return None
    return (
        f"{underlying} goes ex-dividend {ex_date}, inside this spec's life (expires {expiration.isoformat()}). "
        "A short call spanning an ex-date is an early-assignment candidate — No-Stock Mandate. "
        "Put-side structures remain available."
    )


def short_call_assignment_alert(
    underlying: str,
    legs: list[dict],
    underlying_price: float | None,
    today: datetime.date,
) -> str | None:
    """P1 reason when an ITM short call sits within the assignment window of
    an ex-date. Needs the underlying's price; without telemetry there is no
    alert (the entry-side block is the primary defense)."""
    if underlying not in EX_DIV_CALENDAR or underlying_price is None:
        return None
    short_calls = [leg for leg in legs if leg["direction"] == "SHORT" and leg["option_type"] == "CALL"]
    for leg in short_calls:
        if underlying_price <= leg["strike"]:
            continue
        expiration = datetime.date.fromisoformat(leg["expiration"])
        ex_date = ex_div_within(underlying, today, expiration)
        if ex_date is None:
            continue
        if _trading_days_between(today, datetime.date.fromisoformat(ex_date)) <= ASSIGNMENT_WINDOW_TRADING_DAYS:
            return (
                f"Ex-dividend assignment risk: short {underlying} {leg['strike']:.0f} call is ITM "
                f"(price ${underlying_price:.2f}) with ex-date {ex_date} within "
                f"{ASSIGNMENT_WINDOW_TRADING_DAYS} trading days. Close before the ex-date."
            )
    return None
