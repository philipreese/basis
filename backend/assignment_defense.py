"""assignment_defense.py — early-assignment prevention (#130, #736).

American-style options carry early-assignment risk on both sides of a
spread, from two structurally different triggers:

1. Short calls, dividend-driven (#130): a short call whose extrinsic value
   is below an upcoming dividend is worth exercising the night before the
   ex-date, which would hand the book short shares — a No-Stock Mandate P1
   incident. Calendar-predictable: `entry_ex_div_block` hard-blocks any spec
   whose short call spans a projected ex-date; `short_call_assignment_alert`
   is the Layer A backstop for a call that reaches deep-ITM within the
   ex-date's assignment window.
2. Short puts, interest-carry-driven (#736): a short put deep enough ITM
   that its extrinsic value is near zero makes early exercise economically
   rational for the holder — the interest they forgo on the strike proceeds
   by not exercising now. Unlike ex-div dates, this trigger has no calendar:
   it can open with zero forward warning on any American-style underlying,
   dividend-paying or not (unlike the call side, GLD is NOT exempt here —
   the trigger isn't about dividends). There is therefore no meaningful
   entry-side block (nothing forward-looking to check against at entry
   time) — only the Layer A alert, `short_put_assignment_alert`.

   That alert reads live moneyness — underlying_price vs. strike — the same
   inputs `short_call_assignment_alert` already takes, NOT the leg's stored
   `delta`. Delta is stamped once at entry time (executor.py, strike
   derivation) and nothing in this codebase ever refreshes it afterward —
   `operator.refresh_position_values` repriced `current_value_per_share`
   nightly but never touches `position.legs`, so a live position's delta is
   permanently frozen at its entry-day value. A short put's entry delta is
   deliberately small (short_leg_delta ~0.16-0.30) by playbook construction,
   so a delta-based deep-ITM threshold (e.g. -0.95) can never be reached by
   data that never moves — a detector that reads it would silently never
   fire (caught in review before merge). Distance-from-strike computed from
   the live underlying price is a coarser proxy for "extrinsic value near
   zero" than a true option-pricing extrinsic calculation would be — it
   ignores IV and time value directly — but it is the honest choice given
   what's actually tracked live: this module has no per-leg option-price or
   Greeks feed post-entry, only the underlying's price.

Reconciliation only *detects* an assignment after the fact
(UNEXPECTED_INSTRUMENT / ASSIGNMENT_SUSPECTED); this module *prevents* it.

XSP is immune from both triggers (European-style, cash-settled — see
`reconciliation.CASH_SETTLED_UNDERLYINGS`). GLD pays no dividend, so it is
exempt from the call-side ex-div calendar, but not from the put-side check.

The ex-div calendar is static and operator-maintained: projected ex-dates
from the funds' published schedules (SPY/IWM quarterly, TLT monthly). Dates
are estimates until confirmed — the horizon check surfaces staleness in the
nightly digest before the calendar can silently run out.
"""

import datetime

from backend.calendars import EX_DIV_CALENDAR, trading_days_between
from backend.reconciliation import CASH_SETTLED_UNDERLYINGS

# An ITM short call this many trading days before an ex-date is P1.
ASSIGNMENT_WINDOW_TRADING_DAYS = 3

# A short put is read as "near-zero extrinsic value" once the underlying
# has fallen this fraction below the strike (live distance-ITM, #736) — the
# proxy this module uses in place of delta, which is never refreshed after
# entry (see module docstring). Coarser than a true extrinsic-value
# calculation (ignores IV/time value), but computed from live data.
PUT_DEEP_ITM_PCT = 0.05


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


def short_put_assignment_alert(underlying: str, legs: list[dict], underlying_price: float | None) -> str | None:
    """P1 reason when a short put is deep ITM by LIVE distance from strike —
    interest-carry early-exercise risk (#736), the put-side mirror of
    `short_call_assignment_alert`.

    Takes underlying_price, the same live input the call-side alert takes
    (wired from the same source by every caller) — NOT the leg's stored
    delta, which is frozen at entry time and never refreshed (see module
    docstring). No calendar, unlike the call side: interest-carry economics
    have no fixed date to watch. American-style underlyings only — see
    module docstring for why GLD is NOT exempt here even though it is
    exempt from the (dividend-only) call-side calendar.
    """
    if underlying in CASH_SETTLED_UNDERLYINGS or underlying_price is None:
        return None
    short_puts = [leg for leg in legs if leg["direction"] == "SHORT" and leg["option_type"] == "PUT"]
    for leg in short_puts:
        strike = leg["strike"]
        if underlying_price >= strike:
            continue  # not ITM at all
        itm_pct = (strike - underlying_price) / strike
        if itm_pct >= PUT_DEEP_ITM_PCT:
            return (
                f"Interest-carry assignment risk: short {underlying} {leg['strike']:.0f} put is deep ITM "
                f"(price ${underlying_price:.2f}, {itm_pct:.1%} below strike). Close before the counterparty "
                "exercises for the interest carry on the strike proceeds."
            )
    return None
