"""Tests for backend/calendars.py's shared trading-day helpers (#742).

trading_days_between is the holiday-aware replacement for two independent
weekday-only approximations that used to live in assignment_defense.py and
regime_variants.py — both explicitly flagged as approximations in their own
comments. Weekday-only over-counts trading days across a market holiday,
which is the dangerous direction for a `<= N trading days` window check: a
real "within N trading days" condition can evaluate false and a P1/catalyst
alert fires late.
"""

import datetime

from backend.calendars import trading_days_between


class TestTradingDaysBetween:
    def test_ordinary_weekdays_with_no_holiday(self):
        # Tue -> Fri, no holiday in between: 3 trading days.
        assert trading_days_between(datetime.date(2026, 8, 18), datetime.date(2026, 8, 21)) == 3

    def test_weekend_is_excluded(self):
        # Fri -> Mon: 1 trading day (the weekend contributes nothing).
        assert trading_days_between(datetime.date(2026, 8, 21), datetime.date(2026, 8, 24)) == 1

    def test_thanksgiving_holiday_is_excluded(self):
        # #742's exact regression: Mon 2026-11-23 -> Fri 2026-11-27, with
        # Thanksgiving (Thu 2026-11-26) a market holiday in between. A naive
        # weekday count says 4 (Tue/Wed/Thu/Fri) — over the <=3 P1 window, so
        # the alert would evaluate false and fire late. The true trading-day
        # count, skipping the holiday, is 3 (Tue/Wed/Fri) — inside the window.
        start = datetime.date(2026, 11, 23)
        end = datetime.date(2026, 11, 27)
        assert trading_days_between(start, end) == 3

    def test_window_is_exclusive_start_inclusive_end(self):
        d = datetime.date(2026, 8, 18)
        assert trading_days_between(d, d) == 0
        assert trading_days_between(d, d + datetime.timedelta(days=1)) == 1
