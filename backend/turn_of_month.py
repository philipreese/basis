"""turn_of_month.py — the turn-of-month calendar effect's pure rules (#1092).

Same discipline as etf_trend.py: no database, no broker, no clock. Every
function takes plain values and returns plain values, so the window, the
targets and the order deltas are tested on synthetic dates alone. The
executor plumbing that feeds these from the calendar and the books lives in
backend/share_book.py, which also places the actual orders — this module
reuses etf_trend.py's order primitives (buy_limit, sell_limit,
rebalance_orders) rather than forking them; none of the three reads anything
trend-specific, they are plain whole-share order math.

The rule is PRE-REGISTERED (operator approval on #1092, 2026-10-04, from
#1082 sanity check 6, set before any live result exists) and must not be
tuned:

- The window is the LAST trading day of a month through the FIRST 3 trading
  days of the next month — 4 sessions, entirely local to each day's own
  trading-day position within its month (no month-end-average lookback the
  way etf_trend.py needs, so no MISSING_HISTORY case exists here).
- In the window: 100% SCHB. Outside it: 100% TBIL.
- Fail closed on the calendar. A day whose month's holiday table is not
  VERIFIED (calendars.MARKET_HOLIDAY_YEARS) is UNKNOWN, never guessed as
  either in or out.

Timing (#1092, matching how closely the nightly executor allows — the
backtest this rule came from assumed trading AT a close, which the executor
cannot do; full comparison in the PR):

  The backtest's held-out result assumes buying at the close of the
  second-to-last trading day of the month and selling at the close of the
  3rd trading day of the next month — the 4 holding sessions run close(T-1)
  to close(D3). The executor only ever places evening DAY limit orders that
  fill the NEXT session, so the closest equivalent is: stage the BUY the
  evening of T-1 (fills near the open of T, the window's first session) and
  stage the SELL the evening of D3 (fills near the open of D4, the window's
  first day back out). This keeps all 4 of the window's intraday sessions,
  trading the T-1->T overnight gap for the D3->D4 one (plus any weekend or
  holiday before D4) — #1082's exploratory SPY overnight/intraday split
  found the edge lives almost entirely intraday, so this swap is expected to
  cost little, though that split was exploratory, not part of the kill line.

  Unlike B36's month-end rebalance (which never catches up a missed order
  until next month), a missed or unfilled EXIT would leave the book holding
  SCHB through a whole extra month — the opposite of fail closed. So exits
  retry every evening the book still holds SCHB while the window says it
  should not (share_book.py's catch-up), while ENTRIES never catch up late
  (buying several sessions into a 4-session window is a worse trade than
  skipping it, and the position was never at risk either way) — an
  intentional asymmetry, not an oversight.
"""

import datetime
import math
from dataclasses import dataclass
from typing import Literal

from backend.calendars import calendar_known_for, is_trading_day, next_trading_day, previous_trading_day

IN_WINDOW = "IN_WINDOW"
OUT_OF_WINDOW = "OUT_OF_WINDOW"
UNKNOWN = "UNKNOWN"
WindowStatus = Literal["IN_WINDOW", "OUT_OF_WINDOW", "UNKNOWN"]

# The pre-registered window: the month's last trading day, plus this many
# trading days at the START of the next month.
WINDOW_DAYS_INTO_MONTH = 3

_QTY_TOLERANCE = 1e-6


def _usable(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0.0


def trading_days_of_month(year: int, month: int) -> list[datetime.date]:
    """Every NYSE trading day in *year*-*month*, ascending, by
    calendars.is_trading_day. Callers that need a fail-closed read check
    _month_calendar_known first — this function itself trusts whatever
    is_trading_day says, silent gaps included."""
    import calendar as _calendar

    last_day = _calendar.monthrange(year, month)[1]
    days = []
    for day_num in range(1, last_day + 1):
        day = datetime.date(year, month, day_num)
        if is_trading_day(day):
            days.append(day)
    return days


def _month_calendar_known(year: int, month: int) -> bool:
    """False when *year* has no verified MARKET_HOLIDAYS table. A month never
    spans two calendar years, so one sampled day settles it."""
    return calendar_known_for(datetime.date(year, month, 1))


def window_status(day: datetime.date) -> WindowStatus:
    """IN_WINDOW: *day* is the last trading day of its month, or one of the
    first WINDOW_DAYS_INTO_MONTH trading days of its month. OUT_OF_WINDOW on
    any other trading day (and on a non-trading day, for which no order is
    ever placed anyway). UNKNOWN when the month's holiday table is not
    verified — never guessed."""
    if not is_trading_day(day):
        return OUT_OF_WINDOW
    if not _month_calendar_known(day.year, day.month):
        return UNKNOWN
    days = trading_days_of_month(day.year, day.month)
    if not days:
        return UNKNOWN
    if day == days[-1] or day in days[:WINDOW_DAYS_INTO_MONTH]:
        return IN_WINDOW
    return OUT_OF_WINDOW


def desired_status(evening: datetime.date) -> WindowStatus:
    """What the session *evening*'s order would fill into — the next trading
    day — should hold, by the pre-registered rule. This is the one question
    share_book.py's nightly check asks: it is never "what is today", always
    "what does tonight's order expose the book to tomorrow". UNKNOWN when
    that next session's calendar can't be read."""
    return window_status(next_trading_day(evening))


def evening_is_transition(evening: datetime.date) -> bool:
    """True when *evening* is a trading day whose desired_status differs
    from the trading day before it — an entry or exit decision evening.
    False (never a guess) when either side is UNKNOWN or *evening* itself is
    not a trading day (the executor's rebalance only runs on one)."""
    if not is_trading_day(evening):
        return False
    today = desired_status(evening)
    if today is UNKNOWN:
        return False
    yesterday = desired_status(previous_trading_day(evening))
    if yesterday is UNKNOWN:
        return False
    return today != yesterday


def last_transition_evening_on_or_before(day: datetime.date, horizon_days: int = 45) -> datetime.date | None:
    """The most recent evening on or before *day* that was a scheduled entry
    or exit evening (#1092's missed-transition watch note). None when no
    transition is found within *horizon_days* — the window's own 4-session
    cadence means a real calendar always has one well inside the default
    45-day horizon; None in practice means the calendar can't be read."""
    d = day
    for _ in range(horizon_days):
        if evening_is_transition(d):
            return d
        d -= datetime.timedelta(days=1)
    return None


def next_transition_evening_after(day: datetime.date, horizon_days: int = 45) -> datetime.date | None:
    """The next_signal_day_after mirror (#1092): the first scheduled entry or
    exit evening strictly after *day*."""
    d = day
    for _ in range(horizon_days):
        d += datetime.timedelta(days=1)
        if evening_is_transition(d):
            return d
    return None


@dataclass(frozen=True)
class TurnOfMonthTargets:
    risk_symbol_shares: int
    cash_symbol_shares: int


def target_shares(
    status: WindowStatus,
    closes_today: dict[str, float],
    risk_symbol: str,
    cash_symbol: str,
    investable: float,
) -> dict[str, int]:
    """Whole-share targets for the two-symbol book: 100% *risk_symbol* when
    *status* is IN_WINDOW, 100% *cash_symbol* otherwise (OUT_OF_WINDOW or
    UNKNOWN — the fail-closed default is always the cash leg, never the risk
    leg). Mirrors etf_trend.target_shares' floor-then-sweep-the-remainder
    shape for a menu of exactly one asset. Raises ValueError when a close
    needed for sizing is missing — the caller refuses the night, same as
    etf_trend."""
    if investable <= 0.0:
        return {risk_symbol: 0, cash_symbol: 0}
    committed = 0.0
    risk_shares = 0
    if status is IN_WINDOW:
        risk_close = closes_today.get(risk_symbol)
        if not _usable(risk_close):
            raise ValueError(f"no close for {risk_symbol}")
        risk_shares = math.floor(investable / risk_close)
        committed = risk_shares * risk_close
    cash_close = closes_today.get(cash_symbol)
    if not _usable(cash_close):
        raise ValueError(f"no close for the cash leg {cash_symbol}")
    cash_shares = max(0, math.floor((investable - committed) / cash_close))
    return {risk_symbol: risk_shares, cash_symbol: cash_shares}
