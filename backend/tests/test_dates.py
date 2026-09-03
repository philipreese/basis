"""The market clock (#259): run dates come from America/New_York, and
"tonight's events" is a run-start timestamp comparison, never a date prefix
— the EST-season 18:45 run starts 15 minutes before UTC midnight."""

import datetime

from backend.dates import MARKET_TZ, day_order_session_closed, market_date_of, market_evening_window_start, market_today


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


def test_market_date_of_merges_a_midnight_straddling_evening():
    # #537: 23:50 UTC and 00:30 UTC the next day are both 2026-01-15 on the
    # ET clock (18:50 ET and 19:30 ET) — a UTC date prefix would split them.
    assert market_date_of("2026-01-15T23:50:00+00:00") == datetime.date(2026, 1, 15)
    assert market_date_of("2026-01-16T00:30:00+00:00") == datetime.date(2026, 1, 15)


def test_market_date_of_treats_naive_input_as_already_a_market_date():
    assert market_date_of("2026-01-15") == datetime.date(2026, 1, 15)


def test_day_order_session_closed_boundary_at_exactly_1600():
    # #965: a 16:00:00 ET submission is "after close" (day_order_session's
    # own existing ruling, unchanged) — its session rolls to the NEXT
    # trading day, whose close hasn't happened yet moments later.
    submitted_at_1600 = "2026-01-15T21:00:00+00:00"  # 16:00:00 ET exactly (EST)
    just_after = datetime.datetime(2026, 1, 15, 21, 0, 1, tzinfo=datetime.UTC)  # 16:00:01 ET
    assert not day_order_session_closed(submitted_at_1600, just_after)

    # A 15:59 ET submission's session closes AT 16:00 ET THAT SAME day —
    # strictly before an aware now one minute later reads closed.
    submitted_at_1559 = "2026-01-15T20:59:00+00:00"  # 15:59 ET
    just_after_close = datetime.datetime(2026, 1, 15, 21, 0, 1, tzinfo=datetime.UTC)  # 16:00:01 ET
    assert day_order_session_closed(submitted_at_1559, just_after_close)

    # Strictly before: at exactly the close instant, not yet closed.
    exactly_close = datetime.datetime(2026, 1, 15, 21, 0, 0, tzinfo=datetime.UTC)  # 16:00:00 ET
    assert not day_order_session_closed(submitted_at_1559, exactly_close)
