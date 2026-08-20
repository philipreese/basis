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

MARKET_TZ = ZoneInfo("America/New_York")


def market_today() -> datetime.date:
    """Today on the market's clock, regardless of host or UTC rollovers."""
    return datetime.datetime.now(MARKET_TZ).date()


def market_evening_window_start(today: datetime.date) -> str:
    """UTC ISO timestamp of noon ET on *today* — the start of the window in
    which any order belongs to 'this evening' for duplicate detection."""
    noon_et = datetime.datetime.combine(today, datetime.time(12, 0), tzinfo=MARKET_TZ)
    return noon_et.astimezone(datetime.UTC).isoformat()
