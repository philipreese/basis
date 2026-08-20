"""The market clock (#259): run dates come from America/New_York, and
"tonight's events" is a run-start timestamp comparison, never a date prefix
— the EST-season 18:45 run starts 15 minutes before UTC midnight."""

import datetime

from backend.dates import MARKET_TZ, market_evening_window_start, market_today


def test_market_today_is_the_new_york_date(monkeypatch):
    # 2026-01-15 23:45 ET == 2026-01-16 04:45 UTC: UTC has rolled, ET has not.
    class FakeDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime.datetime(2026, 1, 16, 4, 45, tzinfo=datetime.UTC)
            return base.astimezone(tz) if tz else base.replace(tzinfo=None)

    monkeypatch.setattr("backend.dates.datetime.datetime", FakeDatetime)
    assert market_today() == datetime.date(2026, 1, 15)


def test_evening_window_start_is_noon_eastern_in_utc():
    # EST (January): noon ET == 17:00 UTC. EDT (August): noon ET == 16:00 UTC.
    assert market_evening_window_start(datetime.date(2026, 1, 15)).startswith("2026-01-15T17:00:00")
    assert market_evening_window_start(datetime.date(2026, 8, 19)).startswith("2026-08-19T16:00:00")


def test_window_start_contains_the_evening_run_but_not_yesterdays():
    window = market_evening_window_start(datetime.date(2026, 1, 15))
    tonight_pre_midnight = "2026-01-15T23:50:00+00:00"  # 18:50 ET
    tonight_post_midnight = "2026-01-16T00:30:00+00:00"  # 19:30 ET, UTC rolled
    yesterday_evening = "2026-01-15T00:30:00+00:00"  # Jan 14 19:30 ET
    assert tonight_pre_midnight >= window
    assert tonight_post_midnight >= window
    assert not (yesterday_evening >= window)


def test_market_tz_is_new_york():
    assert str(MARKET_TZ) == "America/New_York"
