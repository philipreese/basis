"""etf_trend.py — the monthly ETF trend rotation's pure rules (#1054).

No database, no broker, no clock: every function here takes plain values and
returns plain values, so the signal, the targets and the order deltas are
tested on synthetic data alone. The executor plumbing that feeds these from
index_history and the books lives in backend/share_book.py.

The rules are PRE-REGISTERED (operator ruling on #1054, 2026-10-03, set
before any result was seen) and must not be tuned:

- The signal is read on the LAST trading day of each month, from that day's
  closes. An asset is trending when its close is strictly ABOVE the average
  of its last `trend_months` (10) month-end closes, that month included. A
  close exactly at the average is not trending.
- Each menu asset owns one equal slot of the investable capital (1/6 for the
  six-asset menu). A trending asset fills its slot; a slot whose asset is
  not trending goes to the cash leg. This is the issue's "hold each
  asset ... only while it is trending up, equal weight; whatever isn't
  trending sits in T-bills", and the ruling's "equal weight across trending
  assets; the remainder in SGOV" (the cash leg at the time of the ruling;
  #1087 later swapped it to a different low-priced T-bill fund, TBIL): if
  the k trending assets split 100%, there would be no remainder to put
  anywhere.
- Fail closed on data: an asset missing ANY of the month-end closes the
  average needs (including today's) is treated as not trending, and its slot
  goes to the cash leg. It is never guessed from a neighbouring day.

Sizing is whole shares (broker.place_share_order submits an integer
quantity; the order path has no fractional support): each target is floored,
and whatever the flooring leaves over is swept into the cash leg, again
floored, so a little cash stays uninvested.
"""

import calendar
import datetime
import math
from dataclasses import dataclass
from typing import Literal

from backend.calendars import is_trading_day

TRENDING = "TRENDING"
NOT_TRENDING = "NOT_TRENDING"
MISSING_HISTORY = "MISSING_HISTORY"
TrendStatus = Literal["TRENDING", "NOT_TRENDING", "MISSING_HISTORY"]

# A buy limit sits this far ABOVE the signal-day close and a sell limit this
# far BELOW it: marketable at the next session's open on an ordinary gap, but
# a cap on a bad print. An order the open gaps past simply does not fill; the
# slot stays as it was until next month (no other day trades).
LIMIT_BAND = 0.02
# Held back from the buy budget per order, for commissions — IBKR's per-order
# minimum is under this. Keeps a fully-filled rebalance from driving the
# book's cash below zero on fees alone.
COMMISSION_RESERVE_PER_ORDER = 1.0


@dataclass(frozen=True)
class TrendReading:
    """One asset's signal on one signal day. `average` is None when the
    history was incomplete (status MISSING_HISTORY)."""

    symbol: str
    status: TrendStatus
    close: float | None
    average: float | None
    missing_dates: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShareOrderIntent:
    """One whole-share order the rebalance wants placed."""

    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int
    limit_price: float
    decision_close: float

    @property
    def signed_quantity(self) -> int:
        return self.quantity if self.side == "BUY" else -self.quantity


def last_trading_day_of_month(year: int, month: int) -> datetime.date:
    """The month's last NYSE trading day, by backend/calendars.py's holiday
    table. Walks back from the calendar month end."""
    day = datetime.date(year, month, calendar.monthrange(year, month)[1])
    while not is_trading_day(day):
        day -= datetime.timedelta(days=1)
    return day


def is_signal_day(day: datetime.date) -> bool:
    """True only on the month's last trading day — the one day the book
    trades. A weekend or holiday is never a signal day."""
    return is_trading_day(day) and day == last_trading_day_of_month(day.year, day.month)


def last_signal_day_on_or_before(day: datetime.date) -> datetime.date:
    """The most recent month-end signal day at or before *day* (#1074): this
    month's last trading day once it has arrived, else last month's."""
    candidate = last_trading_day_of_month(day.year, day.month)
    if candidate <= day:
        return candidate
    year, month = (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
    return last_trading_day_of_month(year, month)


def next_signal_day_after(day: datetime.date) -> datetime.date:
    """The first month-end signal day strictly after *day* (#1074)."""
    candidate = last_trading_day_of_month(day.year, day.month)
    if candidate > day:
        return candidate
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return last_trading_day_of_month(year, month)


def month_end_dates(signal_day: datetime.date, months: int) -> list[datetime.date]:
    """The last trading day of each of the `months` months ending with
    signal_day's month, oldest first."""
    out: list[datetime.date] = []
    year, month = signal_day.year, signal_day.month
    for _ in range(months):
        out.append(last_trading_day_of_month(year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(out))


def trend_reading(
    symbol: str, closes_by_date: dict[str, float], signal_day: datetime.date, months: int
) -> TrendReading:
    """The pre-registered signal for one asset. MISSING_HISTORY (never a
    guess) when any month-end close the average needs is absent or not a
    positive number."""
    dates = month_end_dates(signal_day, months)
    missing = tuple(d.isoformat() for d in dates if not _usable(closes_by_date.get(d.isoformat())))
    today_close = closes_by_date.get(signal_day.isoformat())
    if missing:
        return TrendReading(
            symbol, MISSING_HISTORY, today_close if _usable(today_close) else None, None, missing_dates=missing
        )
    closes = [closes_by_date[d.isoformat()] for d in dates]
    average = sum(closes) / len(closes)
    close = closes[-1]
    return TrendReading(symbol, TRENDING if close > average else NOT_TRENDING, close, average)


def _usable(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0.0


def target_shares(
    readings: dict[str, TrendReading],
    closes_today: dict[str, float],
    menu: tuple[str, ...],
    cash_symbol: str,
    investable: float,
) -> dict[str, int]:
    """Whole-share targets for every menu asset and the cash leg.

    Each menu asset's slot is investable / len(menu). A TRENDING asset buys
    floor(slot / close) shares; anything else targets zero. The cash leg
    takes floor(remaining / its close), where remaining is investable less
    the risk targets' value at today's closes. Raises ValueError when a
    close needed for sizing is missing — the caller refuses the month."""
    if investable <= 0.0 or not menu:
        return dict.fromkeys((*menu, cash_symbol), 0)
    slot = investable / len(menu)
    targets: dict[str, int] = {}
    committed = 0.0
    for symbol in menu:
        reading = readings.get(symbol)
        if reading is None or reading.status != TRENDING:
            targets[symbol] = 0
            continue
        close = closes_today.get(symbol)
        if not _usable(close):
            raise ValueError(f"no close for trending asset {symbol}")
        shares = math.floor(slot / close)
        targets[symbol] = shares
        committed += shares * close
    cash_close = closes_today.get(cash_symbol)
    if not _usable(cash_close):
        raise ValueError(f"no close for the cash leg {cash_symbol}")
    targets[cash_symbol] = max(0, math.floor((investable - committed) / cash_close))
    return targets


def buy_limit(close: float, band: float = LIMIT_BAND) -> float:
    """Rounded UP to the cent, so the band is never narrower than stated."""
    return math.ceil(round(close * (1.0 + band) * 100.0, 6)) / 100.0


def sell_limit(close: float, band: float = LIMIT_BAND) -> float:
    """Rounded DOWN to the cent, so the band is never narrower than stated."""
    return math.floor(round(close * (1.0 - band) * 100.0, 6)) / 100.0


def rebalance_orders(
    current: dict[str, int],
    targets: dict[str, int],
    closes_today: dict[str, float],
    cash: float,
    cash_symbol: str,
    band: float = LIMIT_BAND,
    reserve_per_order: float = COMMISSION_RESERVE_PER_ORDER,
) -> list[ShareOrderIntent]:
    """Delta orders only: sells first, then buys, never a zero-quantity order.

    Funding: buys are capped at cash on hand plus every sell valued at its
    (worse-than-close) limit, less a commission reserve per order — so even
    if every order fills at its limit, the book never borrows. When the buys
    do not fit, the cash-leg buy shrinks first (it is the residual), then the
    largest risk buy, one share at a time. Symbols held but no longer in
    *targets* are sold to zero."""
    symbols = sorted(set(current) | set(targets))
    sells: list[ShareOrderIntent] = []
    buys: dict[str, int] = {}
    for symbol in symbols:
        delta = targets.get(symbol, 0) - current.get(symbol, 0)
        if delta == 0:
            continue
        close = closes_today[symbol]
        if delta < 0:
            sells.append(ShareOrderIntent(symbol, "SELL", -delta, sell_limit(close, band), close))
        else:
            buys[symbol] = delta

    proceeds = sum(o.quantity * o.limit_price for o in sells)

    def _shortfall() -> float:
        n_orders = len(sells) + sum(1 for q in buys.values() if q > 0)
        cost = sum(q * buy_limit(closes_today[s], band) for s, q in buys.items())
        return cost - (cash + proceeds - reserve_per_order * n_orders)

    while buys and _shortfall() > 1e-9:
        if buys.get(cash_symbol, 0) > 0:
            victim = cash_symbol
        else:
            victim = max(
                (s for s, q in buys.items() if q > 0),
                key=lambda s: (buys[s] * buy_limit(closes_today[s], band), s),
            )
        buys[victim] -= 1
        if buys[victim] == 0:
            del buys[victim]

    buy_orders = [
        ShareOrderIntent(symbol, "BUY", qty, buy_limit(closes_today[symbol], band), closes_today[symbol])
        for symbol, qty in sorted(buys.items())
        if qty > 0
    ]
    return sells + buy_orders
