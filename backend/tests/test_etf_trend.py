"""The monthly ETF trend book's pure rules (backend/etf_trend.py, #1054).

Fail-closed paths first: a missing month-end close, a close exactly at its
average, a non-month-end day — each must read as "do not trade / not
trending", never as a guess."""

import datetime

import pytest

from backend.etf_trend import (
    MISSING_HISTORY,
    NOT_TRENDING,
    TRENDING,
    TrendReading,
    buy_limit,
    is_signal_day,
    last_trading_day_of_month,
    month_end_dates,
    rebalance_orders,
    sell_limit,
    target_shares,
    trend_reading,
)

MENU = ("VTI", "VEA", "IEF", "GLD", "VNQ", "DBMF")
CASH = "SGOV"
SIGNAL_DAY = datetime.date(2026, 10, 30)  # Friday; Oct 31 2026 is a Saturday


def _month_end_series(closes: list[float], signal_day: datetime.date = SIGNAL_DAY) -> dict[str, float]:
    """One close per month-end, oldest first, ending on signal_day's month."""
    dates = month_end_dates(signal_day, len(closes))
    return {d.isoformat(): c for d, c in zip(dates, closes, strict=True)}


class TestMonthEndDetection:
    def test_weekend_month_end_walks_back_to_friday(self):
        assert last_trading_day_of_month(2026, 10) == datetime.date(2026, 10, 30)

    def test_holiday_month_end_walks_back_past_it(self):
        # 2027-05-31 is Memorial Day (a Monday), 29/30 are the weekend.
        assert last_trading_day_of_month(2027, 5) == datetime.date(2027, 5, 28)

    def test_ordinary_month_end(self):
        assert last_trading_day_of_month(2026, 9) == datetime.date(2026, 9, 30)

    @pytest.mark.parametrize(
        "day",
        [
            datetime.date(2026, 10, 29),  # the day before month-end
            datetime.date(2026, 10, 31),  # a Saturday month-end
            datetime.date(2027, 5, 31),  # a holiday month-end
            datetime.date(2026, 10, 1),  # first trading day of a month
        ],
    )
    def test_no_trading_on_non_signal_days(self, day):
        assert not is_signal_day(day)

    @pytest.mark.parametrize("day", [datetime.date(2026, 10, 30), datetime.date(2027, 5, 28)])
    def test_signal_day_is_the_last_trading_day(self, day):
        assert is_signal_day(day)

    def test_month_end_dates_cross_a_year_boundary_oldest_first(self):
        dates = month_end_dates(datetime.date(2027, 2, 26), 3)
        assert dates == [datetime.date(2026, 12, 31), datetime.date(2027, 1, 29), datetime.date(2027, 2, 26)]


class TestTrendSignal:
    def test_close_above_its_ten_month_average_is_trending(self):
        reading = trend_reading("VTI", _month_end_series([float(v) for v in range(100, 110)]), SIGNAL_DAY, 10)
        assert reading.status == TRENDING
        assert reading.close == 109.0
        assert reading.average == pytest.approx(104.5)

    def test_close_below_its_average_is_not_trending(self):
        reading = trend_reading("VTI", _month_end_series([float(v) for v in range(110, 100, -1)]), SIGNAL_DAY, 10)
        assert reading.status == NOT_TRENDING

    def test_close_exactly_at_the_average_is_not_trending(self):
        reading = trend_reading("VTI", _month_end_series([100.0] * 10), SIGNAL_DAY, 10)
        assert reading.status == NOT_TRENDING

    def test_one_missing_month_end_fails_closed(self):
        series = _month_end_series([float(v) for v in range(100, 110)])
        dropped = sorted(series)[3]
        del series[dropped]
        reading = trend_reading("VTI", series, SIGNAL_DAY, 10)
        assert reading.status == MISSING_HISTORY
        assert reading.missing_dates == (dropped,)
        assert reading.average is None

    def test_missing_todays_close_fails_closed(self):
        series = _month_end_series([float(v) for v in range(100, 110)])
        del series[SIGNAL_DAY.isoformat()]
        reading = trend_reading("VTI", series, SIGNAL_DAY, 10)
        assert reading.status == MISSING_HISTORY
        assert reading.close is None

    def test_too_short_history_fails_closed(self):
        reading = trend_reading("DBMF", _month_end_series([float(v) for v in range(100, 106)]), SIGNAL_DAY, 10)
        assert reading.status == MISSING_HISTORY
        assert len(reading.missing_dates) == 4

    def test_a_non_month_end_close_never_stands_in(self):
        # The month-end row is missing, but the day before it is present:
        # it must NOT be used in its place.
        series = _month_end_series([float(v) for v in range(100, 110)])
        sept_end = month_end_dates(SIGNAL_DAY, 2)[0].isoformat()
        del series[sept_end]
        series["2026-09-29"] = 108.0
        assert trend_reading("VTI", series, SIGNAL_DAY, 10).status == MISSING_HISTORY

    def test_non_positive_close_is_unusable(self):
        series = _month_end_series([float(v) for v in range(100, 110)])
        series[min(series)] = 0.0
        assert trend_reading("VTI", series, SIGNAL_DAY, 10).status == MISSING_HISTORY


def _readings(trending: set[str]) -> dict[str, TrendReading]:
    return {s: TrendReading(s, TRENDING if s in trending else NOT_TRENDING, 100.0, 90.0) for s in MENU}


CLOSES = {"VTI": 300.0, "VEA": 55.0, "IEF": 95.0, "GLD": 330.0, "VNQ": 90.0, "DBMF": 28.0, "SGOV": 100.5}


class TestTargetShares:
    def test_each_trending_asset_fills_one_equal_slot_and_the_rest_goes_to_cash(self):
        targets = target_shares(_readings({"VTI", "GLD"}), CLOSES, MENU, CASH, 6000.0)
        # slot = 1000: VTI floor(1000/300)=3, GLD floor(1000/330)=3
        assert targets["VTI"] == 3
        assert targets["GLD"] == 3
        assert all(targets[s] == 0 for s in ("VEA", "IEF", "VNQ", "DBMF"))
        # remaining 6000 - 900 - 990 = 4110 -> floor(4110 / 100.5) = 40
        assert targets[CASH] == 40

    def test_nothing_trending_is_all_cash_leg(self):
        targets = target_shares(_readings(set()), CLOSES, MENU, CASH, 10_000.0)
        assert targets[CASH] == 99
        assert sum(targets[s] for s in MENU) == 0

    def test_missing_history_slot_goes_to_cash(self):
        readings = _readings(set(MENU))
        readings["DBMF"] = TrendReading("DBMF", MISSING_HISTORY, None, None, ("2026-01-30",))
        targets = target_shares(readings, CLOSES, MENU, CASH, 6000.0)
        assert targets["DBMF"] == 0
        assert targets[CASH] > 0

    def test_missing_close_for_a_trending_asset_refuses(self):
        closes = {k: v for k, v in CLOSES.items() if k != "VTI"}
        with pytest.raises(ValueError, match="VTI"):
            target_shares(_readings({"VTI"}), closes, MENU, CASH, 6000.0)

    def test_missing_cash_leg_close_refuses(self):
        closes = {k: v for k, v in CLOSES.items() if k != CASH}
        with pytest.raises(ValueError, match=CASH):
            target_shares(_readings({"VTI"}), closes, MENU, CASH, 6000.0)

    def test_nothing_to_invest_targets_zero(self):
        targets = target_shares(_readings(set(MENU)), CLOSES, MENU, CASH, 0.0)
        assert set(targets.values()) == {0}


class TestLimits:
    def test_buy_limit_rounds_up_to_the_cent(self):
        assert buy_limit(100.0) == 102.0
        assert buy_limit(55.555) == 56.67

    def test_sell_limit_rounds_down_to_the_cent(self):
        assert sell_limit(100.0) == 98.0
        assert sell_limit(55.555) == 54.44


class TestRebalanceOrders:
    def test_only_deltas_and_sells_first(self):
        current = {"VTI": 3, "IEF": 10, "SGOV": 20}
        targets = {"VTI": 3, "IEF": 0, "GLD": 3, "SGOV": 25}
        orders = rebalance_orders(current, targets, CLOSES, cash=5000.0, cash_symbol=CASH)
        assert [(o.side, o.symbol, o.quantity) for o in orders] == [
            ("SELL", "IEF", 10),
            ("BUY", "GLD", 3),
            ("BUY", "SGOV", 5),
        ]
        assert orders[0].limit_price == sell_limit(95.0)
        assert orders[1].limit_price == buy_limit(330.0)
        assert orders[1].signed_quantity == 3 and orders[0].signed_quantity == -10

    def test_on_target_places_nothing(self):
        assert rebalance_orders({"VTI": 3}, {"VTI": 3, "SGOV": 0}, CLOSES, 1000.0, CASH) == []

    def test_a_held_symbol_missing_from_targets_is_sold_to_zero(self):
        orders = rebalance_orders({"VNQ": 4}, {"SGOV": 0}, CLOSES, 0.0, CASH)
        assert [(o.side, o.symbol, o.quantity) for o in orders] == [("SELL", "VNQ", 4)]

    def test_buys_never_exceed_cash_plus_sell_proceeds_at_limits(self):
        # 1000 cash, nothing to sell: SGOV at 102.51/share limit, $1 reserve.
        orders = rebalance_orders({}, {"SGOV": 10}, CLOSES, cash=1000.0, cash_symbol=CASH)
        (order,) = orders
        assert order.quantity * order.limit_price <= 1000.0 - 1.0
        assert order.quantity == 9

    def test_cash_leg_shrinks_before_a_risk_buy(self):
        # Enough for the GLD buy (3 x 336.60 = 1009.80) plus little else.
        orders = rebalance_orders({}, {"GLD": 3, "SGOV": 5}, CLOSES, cash=1100.0, cash_symbol=CASH)
        assert {o.symbol: o.quantity for o in orders} == {"GLD": 3}

    def test_largest_risk_buy_shrinks_when_cash_leg_is_exhausted(self):
        orders = rebalance_orders({}, {"GLD": 3, "VTI": 3}, CLOSES, cash=1500.0, cash_symbol=CASH)
        cost = sum(o.quantity * o.limit_price for o in orders) + len(orders)
        assert cost <= 1500.0
        assert sum(o.quantity for o in orders) < 6

    def test_sell_proceeds_fund_the_buys(self):
        orders = rebalance_orders({"VTI": 10}, {"VTI": 0, "SGOV": 29}, CLOSES, cash=0.0, cash_symbol=CASH)
        sells = [o for o in orders if o.side == "SELL"]
        buys = [o for o in orders if o.side == "BUY"]
        assert sells[0].quantity == 10
        proceeds = 10 * sell_limit(300.0)
        assert sum(o.quantity * o.limit_price for o in buys) <= proceeds - len(orders)
        assert buys[0].quantity == 28
