"""dates.py — the market clock (#259, audit finding C6).

Every run-level date in this system is a MARKET date: the US options market
lives in America/New_York, and the executor fires at 18:45 ET. Computing
"today" as datetime.now(UTC).date() left 15 minutes of margin in EST season
(18:45 ET = 23:45 UTC) — a run that started late or ran long rolled into
tomorrow's UTC date mid-pipeline, silently emptying digest sections, making
Friday runs think it was Saturday, and splitting anomaly date buckets.

Rules:
- Compute the run's date ONCE, at run start, via market_today(); thread it.
- Event rows keep UTC ISO timestamps; "tonight's events" filters use a
  run-start timestamp (>=), never a date prefix.
- The duplicate-order check uses market_evening_window_start(): noon ET of
  the market day, in UTC — wide enough to catch a same-evening double run,
  narrow enough to exclude yesterday evening's legitimate orders.
"""

import datetime
from zoneinfo import ZoneInfo

from backend.calendars import is_trading_day

MARKET_TZ = ZoneInfo("America/New_York")

# The close IBKR expires a DAY order against. Expressed as a MARKET_TZ-aware
# time, not a raw UTC offset, so EST/EDT shifts with zoneinfo instead of
# needing two hardcoded values a season apart.
MARKET_CLOSE_TIME = datetime.time(16, 0)


def market_today() -> datetime.date:
    """Today on the market's clock, regardless of host or UTC rollovers."""
    return datetime.datetime.now(MARKET_TZ).date()


def market_evening_window_start(today: datetime.date) -> str:
    """UTC ISO timestamp of noon ET on *today* — the start of the window in
    which any order belongs to 'this evening' for duplicate detection."""
    noon_et = datetime.datetime.combine(today, datetime.time(12, 0), tzinfo=MARKET_TZ)
    return noon_et.astimezone(datetime.UTC).isoformat()


def day_order_session(submitted_at: str) -> datetime.date:
    """The trading day a DAY order's own session belongs to (#959): the
    order works AT the broker through that day's close, then IBKR expires
    it — this is the day the order's absence at sync time is measured
    against, not the calendar day it happened to be submitted on.

    A submission before that day's close (a trading day, during market
    hours — e.g. midday_exits.py's 12:30 ET pass) belongs to its own
    calendar day. Anything else — after that day's close (the nightly
    18:45 ET run, #70), or submitted on a non-trading day at all — rolls
    forward to the NEXT trading day, the same way IBKR itself queues a
    DAY order placed outside a session for the next one."""
    submitted = datetime.datetime.fromisoformat(submitted_at).astimezone(MARKET_TZ)
    same_day_session = is_trading_day(submitted.date()) and submitted.time() < MARKET_CLOSE_TIME
    session = submitted.date() if same_day_session else submitted.date() + datetime.timedelta(days=1)
    while not is_trading_day(session):
        session += datetime.timedelta(days=1)
    return session


def day_order_session_closed(submitted_at: str, now: datetime.datetime) -> bool:
    """Whether a DAY order's own session (day_order_session) has actually
    closed as of an AWARE *now* — not just whether the calendar has reached
    the session's date (#965 fix-forward). day_order_session <= today alone
    is trivially true for any same-calendar-day submission regardless of the
    hour, which let a mid-session read-only drill call a genuinely-vanished
    order 'day expired' before its session's close had even happened."""
    close = datetime.datetime.combine(day_order_session(submitted_at), MARKET_CLOSE_TIME, tzinfo=MARKET_TZ)
    return close < now.astimezone(MARKET_TZ)


def market_date_of(iso: str) -> datetime.date:
    """The MARKET_TZ date an (aware UTC) ISO timestamp falls on (#419, #537).

    Grouping timestamps by their UTC date prefix silently merges/splits
    sessions in EST season, where the 18:45 ET run straddles 00:00 UTC.
    Aware timestamps convert to the market timezone first; a naive input
    (already a plain date string) is taken as a market date as-is."""
    parsed = datetime.datetime.fromisoformat(iso)
    return parsed.astimezone(MARKET_TZ).date() if parsed.tzinfo is not None else parsed.date()
