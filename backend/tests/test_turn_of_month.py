"""The turn-of-month calendar-effect book's pure rules (backend/turn_of_month.py, #1092).

Fail-closed paths first: a day in a year with no verified holiday table must
read UNKNOWN, never as a guess in either direction."""

import datetime

import pytest

from backend.turn_of_month import (
    IN_WINDOW,
    OUT_OF_WINDOW,
    UNKNOWN,
    desired_status,
    evening_is_transition,
    last_transition_evening_on_or_before,
    next_transition_evening_after,
    target_shares,
    trading_days_of_month,
    window_status,
)

RISK = "SCHB"
CASH = "TBIL"


class TestWindowStatus:
    def test_last_trading_day_of_month_is_in_window(self):
        # October 2026's last trading day is Friday the 30th (31st is Saturday).
        assert window_status(datetime.date(2026, 10, 30)) is IN_WINDOW

    def test_first_three_trading_days_of_next_month_are_in_window(self):
        # Nov 2026: 1st is a Sunday, so trading days are 2, 3, 4, 5, ...
        assert window_status(datetime.date(2026, 11, 2)) is IN_WINDOW
        assert window_status(datetime.date(2026, 11, 3)) is IN_WINDOW
        assert window_status(datetime.date(2026, 11, 4)) is IN_WINDOW
        assert window_status(datetime.date(2026, 11, 5)) is OUT_OF_WINDOW

    def test_mid_month_is_out_of_window(self):
        assert window_status(datetime.date(2026, 10, 15)) is OUT_OF_WINDOW

    def test_weekend_is_out_of_window(self):
        assert window_status(datetime.date(2026, 10, 31)) is OUT_OF_WINDOW  # Saturday

    def test_window_spans_a_new_years_holiday(self):
        # Dec 2026 -> Jan 2027: Jan 1 2027 is a holiday (New Year's, a Friday).
        # Dec's last trading day is Dec 31 (Thursday; Dec 25 was Christmas).
        assert window_status(datetime.date(2026, 12, 31)) is IN_WINDOW
        # Jan 1 is a holiday, so the first 3 trading days of Jan 2027 are
        # Jan 4, 5, 6 (weekend absorbs Jan 2-3).
        assert window_status(datetime.date(2027, 1, 4)) is IN_WINDOW
        assert window_status(datetime.date(2027, 1, 5)) is IN_WINDOW
        assert window_status(datetime.date(2027, 1, 6)) is IN_WINDOW
        assert window_status(datetime.date(2027, 1, 7)) is OUT_OF_WINDOW

    def test_window_spans_independence_day_observed(self):
        # Jun -> Jul 2026: Jul 3 2026 is observed Independence Day (a Friday).
        # Jun's last trading day is Jun 30 (Tuesday).
        assert window_status(datetime.date(2026, 6, 30)) is IN_WINDOW
        # First 3 trading days of Jul 2026: 1, 2, 6 (3rd is the holiday, 4/5 weekend).
        assert window_status(datetime.date(2026, 7, 1)) is IN_WINDOW
        assert window_status(datetime.date(2026, 7, 2)) is IN_WINDOW
        assert window_status(datetime.date(2026, 7, 6)) is IN_WINDOW
        assert window_status(datetime.date(2026, 7, 7)) is OUT_OF_WINDOW

    def test_unverified_year_is_unknown(self):
        # 2028 has no MARKET_HOLIDAYS table (MARKET_HOLIDAY_YEARS stops at
        # 2027) — never guessed as in or out.
        assert window_status(datetime.date(2028, 1, 31)) is UNKNOWN
        assert window_status(datetime.date(2028, 6, 15)) is UNKNOWN

    def test_known_gap_years_are_unknown(self):
        # 2024/2025 are a deliberate documented gap (calendars.py).
        assert window_status(datetime.date(2024, 5, 30)) is UNKNOWN
        assert window_status(datetime.date(2025, 3, 31)) is UNKNOWN


class TestTradingDaysOfMonth:
    def test_matches_last_and_first_three(self):
        days = trading_days_of_month(2026, 10)
        assert days[-1] == datetime.date(2026, 10, 30)
        assert days[0] == datetime.date(2026, 10, 1)


class TestDesiredStatusAndTransitions:
    def test_desired_status_is_tomorrows_session(self):
        # Evening of Oct 29 2026 (Thursday): tomorrow (Oct 30) is the
        # window's first session.
        assert desired_status(datetime.date(2026, 10, 29)) is IN_WINDOW
        # Evening of Oct 30 (the window's own last day): tomorrow is a
        # Saturday -> next trading day is Nov 2, still IN_WINDOW.
        assert desired_status(datetime.date(2026, 10, 30)) is IN_WINDOW

    def test_entry_evening_is_a_transition(self):
        # Oct 29 evening: today (Oct 29) is OUT_OF_WINDOW, tomorrow (Oct 30)
        # is IN_WINDOW -> a real transition.
        assert evening_is_transition(datetime.date(2026, 10, 29))

    def test_exit_evening_is_a_transition(self):
        # Nov 4 2026 (Wednesday) is the window's last day (3rd trading day of
        # Nov); Nov 5 is OUT_OF_WINDOW.
        assert evening_is_transition(datetime.date(2026, 11, 4))

    def test_mid_window_evening_is_not_a_transition(self):
        assert not evening_is_transition(datetime.date(2026, 10, 30))

    def test_mid_month_evening_is_not_a_transition(self):
        assert not evening_is_transition(datetime.date(2026, 10, 15))

    def test_weekend_evening_is_never_a_transition(self):
        assert not evening_is_transition(datetime.date(2026, 10, 31))

    def test_transition_in_an_unknown_calendar_is_never_claimed(self):
        assert not evening_is_transition(datetime.date(2028, 1, 15))

    def test_last_and_next_transition_evenings(self):
        # Every month boundary is its own window (Sep's last day + Oct's
        # first 3; Oct's last day + Nov's first 3; ...), so a transition
        # lands roughly every two weeks, not once a month: looking back from
        # Oct 15 finds Oct 5 (the Sep->Oct window's exit evening, Oct's 3rd
        # trading day), and looking forward finds Oct 29 (the Oct->Nov
        # window's entry evening, the day before Oct's last trading day).
        today = datetime.date(2026, 10, 15)
        last = last_transition_evening_on_or_before(today)
        nxt = next_transition_evening_after(today)
        assert last == datetime.date(2026, 10, 5)
        assert nxt == datetime.date(2026, 10, 29)


class TestTargetShares:
    def test_in_window_buys_the_risk_symbol(self):
        targets = target_shares(IN_WINDOW, {RISK: 50.0, CASH: 100.0}, RISK, CASH, 1000.0)
        assert targets[RISK] == 20
        assert targets[CASH] == 0  # 1000 - 20*50 = 0 remaining

    def test_out_of_window_buys_only_cash(self):
        targets = target_shares(OUT_OF_WINDOW, {RISK: 50.0, CASH: 100.0}, RISK, CASH, 1000.0)
        assert targets == {RISK: 0, CASH: 10}

    def test_unknown_status_defaults_to_cash(self):
        targets = target_shares(UNKNOWN, {RISK: 50.0, CASH: 100.0}, RISK, CASH, 1000.0)
        assert targets == {RISK: 0, CASH: 10}

    def test_zero_investable_is_all_zero(self):
        assert target_shares(IN_WINDOW, {RISK: 50.0, CASH: 100.0}, RISK, CASH, 0.0) == {RISK: 0, CASH: 0}

    def test_missing_risk_close_raises(self):
        with pytest.raises(ValueError):
            target_shares(IN_WINDOW, {CASH: 100.0}, RISK, CASH, 1000.0)

    def test_missing_cash_close_raises(self):
        with pytest.raises(ValueError):
            target_shares(OUT_OF_WINDOW, {RISK: 50.0}, RISK, CASH, 1000.0)

    def test_leftover_is_swept_into_cash(self):
        # 1000 / 51 -> 19 shares (969), remaining 31 -> floor(31/100) = 0.
        targets = target_shares(IN_WINDOW, {RISK: 51.0, CASH: 100.0}, RISK, CASH, 1000.0)
        assert targets[RISK] == 19
        assert targets[CASH] == 0
