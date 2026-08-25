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

from backend.calendars import (
    CALENDAR_COVERAGE_END,
    CALENDAR_COVERAGE_START,
    CPI_DATES,
    EX_DIV_CALENDAR,
    FOMC_DATES,
    MARKET_HOLIDAYS,
    stale_calendars,
    trading_days_between,
)


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


class TestHistoricalBackfill:
    """Spot checks on the 2009-2023 backfill (#795) — each asserted date
    was verified against the authoritative source named in its test, so a
    regression here means someone edited verified history."""

    def test_market_holidays_known_closures(self):
        # NYSE holiday calendars (ir.theice.com press releases + two
        # independent historical closure datasets).
        assert "2014-04-18" in MARKET_HOLIDAYS  # Good Friday 2014
        assert "2012-10-29" in MARKET_HOLIDAYS  # Hurricane Sandy
        assert "2012-10-30" in MARKET_HOLIDAYS  # Hurricane Sandy
        assert "2018-12-05" in MARKET_HOLIDAYS  # G.H.W. Bush mourning
        assert "2023-06-19" in MARKET_HOLIDAYS  # Juneteenth (from 2022 only)
        assert "2022-06-20" in MARKET_HOLIDAYS  # Juneteenth (observed)

    def test_market_holidays_known_non_closures(self):
        # Juneteenth was not a market holiday before 2022, markets stayed
        # open through COVID 2020, and NYSE does not observe New Year's
        # when Jan 1 falls on a Saturday (Rule 7.2): no closure around
        # New Year's 2011 or 2022.
        assert "2021-06-18" not in MARKET_HOLIDAYS
        assert not any(d.startswith("2020-03") for d in MARKET_HOLIDAYS)
        assert "2010-12-31" not in MARKET_HOLIDAYS
        assert "2021-12-31" not in MARKET_HOLIDAYS
        assert "2022-01-03" not in MARKET_HOLIDAYS

    def test_fomc_sampled_decision_days(self):
        # federalreserve.gov/monetarypolicy/fomchistorical<year>.htm and
        # fomccalendars.htm; decision day = second day of two-day meetings.
        assert "2009-03-18" in FOMC_DATES
        assert "2015-12-16" in FOMC_DATES  # liftoff
        assert "2019-07-31" in FOMC_DATES
        assert "2023-07-26" in FOMC_DATES

    def test_fomc_2020_scheduled_march_meeting_cancelled(self):
        # The Fed's historical page lists 2020-03-17/18 as "(cancelled)"
        # and the March 2 / March 15 actions as "(unscheduled)" — none of
        # the three belongs in a scheduled-decision-day table, leaving
        # 2020 with seven entries where every other year has eight.
        by_year = {y: [d for d in FOMC_DATES if d.startswith(str(y))] for y in range(2009, 2024)}
        assert not [d for d in by_year[2020] if d.startswith("2020-03")]
        assert len(by_year[2020]) == 7
        assert all(len(dates) == 8 for y, dates in by_year.items() if y != 2020)

    def test_cpi_sampled_release_days(self):
        # BLS archived news releases (bls.gov/bls/news-release/cpi.htm).
        assert "2009-01-16" in CPI_DATES
        assert "2013-10-30" in CPI_DATES  # Sep 2013 release, shutdown-delayed
        assert "2022-06-10" in CPI_DATES
        assert all(len([d for d in CPI_DATES if d.startswith(str(y))]) == 12 for y in range(2009, 2024))

    def test_spy_ex_div_sampled_actuals(self):
        # Historical actuals double-sourced (Yahoo Finance dividend events
        # + dividendhistory.org; 2012-09-21 via digrin.com).
        spy = EX_DIV_CALENDAR["SPY"]
        assert "2009-03-20" in spy
        assert "2012-09-21" in spy
        assert "2018-12-21" in spy
        assert "2020-03-20" in spy
        assert "2023-12-15" in spy
        assert all(len([d for d in spy if d.startswith(str(y))]) == 4 for y in range(2009, 2024))

    def test_other_tickers_have_no_unverified_historical_entries(self):
        # The historical ex-div gap is deliberate (see the calendar
        # comment): IWM/TLT/AAPL actuals could not be double-sourced, so
        # replay ex-div coverage is SPY-only — no pre-2026 entries may
        # appear for them without re-verification.
        for symbol in ("IWM", "TLT", "AAPL"):
            assert min(EX_DIV_CALENDAR[symbol]) >= "2026-01-01"


class TestCoverageConstants:
    def test_coverage_range_bounds(self):
        assert CALENDAR_COVERAGE_START == datetime.date(2009, 1, 1)
        assert CALENDAR_COVERAGE_END == datetime.date(2023, 12, 31)
        all_dates = {*MARKET_HOLIDAYS, *FOMC_DATES, *CPI_DATES, *EX_DIV_CALENDAR["SPY"]}
        # START is exactly the earliest table entry (2009-01-01, New
        # Year's Day 2009): nothing precedes the declared coverage.
        assert min(datetime.date.fromisoformat(d) for d in all_dates) == CALENDAR_COVERAGE_START

    def test_every_covered_year_is_populated_in_every_table(self):
        # Fail-closed contract for #796: inside [START, END] each table
        # must actually have entries — a hollow year would silently replay
        # as "no holidays, no catalysts, no ex-div".
        for year in range(CALENDAR_COVERAGE_START.year, CALENDAR_COVERAGE_END.year + 1):
            prefix = str(year)
            assert any(d.startswith(prefix) for d in MARKET_HOLIDAYS)
            assert any(d.startswith(prefix) for d in FOMC_DATES)
            assert any(d.startswith(prefix) for d in CPI_DATES)
            assert any(d.startswith(prefix) for d in EX_DIV_CALENDAR["SPY"])

    def test_no_stray_dates_in_the_unverified_gap(self):
        # 2024-2025 were never verified; any entry there would falsely
        # widen the coverage a replay tool may trust.
        all_dates = {*MARKET_HOLIDAYS, *FOMC_DATES, *CPI_DATES}
        for dates in EX_DIV_CALENDAR.values():
            all_dates |= set(dates)
        end = CALENDAR_COVERAGE_END.isoformat()
        assert not [d for d in all_dates if end < d < "2026-01-01"]


class TestStalenessUnaffectedByBackfill:
    def test_stale_calendars_ignores_historical_entries(self):
        # stale_calendars looks only at each table's MAX date, so the
        # 2009-2023 backfill must not change its verdict: with 2027
        # coverage in every table, nothing is stale today.
        assert stale_calendars(datetime.date(2026, 8, 24)) == []

    def test_stale_verdict_driven_by_forward_coverage_only(self):
        # Push today near the end of coverage: everything goes stale —
        # proving the verdict tracks the forward edge, not table size.
        stale = stale_calendars(datetime.date(2027, 12, 1))
        assert "market holidays" in stale
        assert "FOMC/CPI catalysts" in stale
