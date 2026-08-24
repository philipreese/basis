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

from backend.calendars import EX_DIV_CALENDAR, trading_days_between

# An ITM short call this many trading days before an ex-date is P1.
ASSIGNMENT_WINDOW_TRADING_DAYS = 3


def ex_div_within(symbol: str, start: datetime.date, end: datetime.date) -> str | None:
    """First calendar ex-date d with start < d <= end, or None."""
    for iso in EX_DIV_CALENDAR.get(symbol, ()):
        d = datetime.date.fromisoformat(iso)
        if start < d <= end:
            return iso
    return None


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
        if trading_days_between(today, datetime.date.fromisoformat(ex_date)) <= ASSIGNMENT_WINDOW_TRADING_DAYS:
            return (
                f"Ex-dividend assignment risk: short {underlying} {leg['strike']:.0f} call is ITM "
                f"(price ${underlying_price:.2f}) with ex-date {ex_date} within "
                f"{ASSIGNMENT_WINDOW_TRADING_DAYS} trading days. Close before the ex-date."
            )
    return None
