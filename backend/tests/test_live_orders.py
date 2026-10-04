"""The live share-order safety rules (#1065, backend/live_orders.py)."""

from backend.broker import SharePreview
from backend.etf_trend import ShareOrderIntent, buy_limit
from backend.live_orders import (
    check_buy_preview,
    check_no_debit,
    check_order_caps,
    rebalance_sells,
    size_live_buys,
)

CLOSES = {"AAA": 30.0, "BBB": 40.0, "CASH": 50.0}


def _preview(ewl: float | None = 1000.0, im: float | None = 100.0, commission: float | None = 1.0) -> SharePreview:
    return SharePreview(10.0, 10.0, ewl, im, commission, commission)


def test_rebalance_sells_returns_only_sells():
    sells = rebalance_sells({"AAA": 10, "BBB": 1}, {"AAA": 0, "BBB": 5, "CASH": 3}, CLOSES, "CASH")
    assert [(s.symbol, s.side, s.quantity) for s in sells] == [("AAA", "SELL", 10)]


def test_buys_never_count_unfilled_sale_proceeds():
    # Holding AAA above target adds nothing to the budget: only cash funds buys.
    buys = size_live_buys({"AAA": 100}, {"AAA": 0, "BBB": 10}, CLOSES, budget=0.0, cash_symbol="CASH")
    assert buys == []


def test_buys_fit_the_budget_cash_leg_shrinks_first():
    budget = 10 * buy_limit(40.0) + 2.0 + 2 * buy_limit(50.0)  # BBB fully, CASH only 2 of 5
    buys = size_live_buys({}, {"BBB": 10, "CASH": 5}, CLOSES, budget=budget, cash_symbol="CASH")
    assert {b.symbol: b.quantity for b in buys} == {"BBB": 10, "CASH": 2}
    assert all(b.side == "BUY" for b in buys)


def test_buys_shrink_largest_risk_buy_after_cash_leg():
    buys = size_live_buys({}, {"AAA": 2, "BBB": 2}, CLOSES, budget=buy_limit(30.0) * 2 + 2.0, cash_symbol="CASH")
    # One share at a time off whichever buy is largest at that moment (the
    # paper shrink order): BBB, then AAA, then BBB again.
    assert {b.symbol: b.quantity for b in buys} == {"AAA": 1}


def test_non_finite_budget_buys_nothing():
    assert size_live_buys({}, {"AAA": 1}, CLOSES, budget=float("nan"), cash_symbol="CASH") == []


def _intent(symbol: str, side: str, qty: int, limit: float) -> ShareOrderIntent:
    return ShareOrderIntent(symbol, side, qty, limit, limit)


def test_caps_refuse_an_order_bigger_than_the_stake():
    reason = check_order_caps([_intent("AAA", "BUY", 100, 30.0)], stake=1000.0, investable=5000.0, holdings={})
    assert reason and "exceeds the stake" in reason


def test_caps_refuse_buys_beyond_stake_plus_pnl():
    intents = [_intent("AAA", "BUY", 30, 30.0), _intent("BBB", "BUY", 20, 40.0)]
    reason = check_order_caps(intents, stake=1000.0, investable=1500.0, holdings={})
    assert reason == "the run's buys exceed stake + accrued P&L"


def test_caps_refuse_selling_more_than_held():
    reason = check_order_caps([_intent("AAA", "SELL", 5, 30.0)], stake=1000.0, investable=1000.0, holdings={"AAA": 4})
    assert reason and "exceeds the 4 held" in reason


def test_caps_refuse_non_positive_stake_or_investable():
    assert check_order_caps([], stake=0.0, investable=1.0, holdings={})
    assert check_order_caps([], stake=1.0, investable=float("nan"), holdings={})


def test_caps_pass():
    assert check_order_caps([_intent("AAA", "BUY", 10, 30.0)], stake=1000.0, investable=1000.0, holdings={}) is None


def test_buy_preview_refuses_missing_figures_and_margin_debit():
    assert check_buy_preview(_preview(ewl=None))
    assert check_buy_preview(_preview(im=None))
    assert "margin debit" in check_buy_preview(_preview(ewl=50.0, im=100.0))
    assert check_buy_preview(_preview()) is None


def test_no_debit_uses_the_smaller_of_book_and_broker_cash_and_previewed_commission():
    buys = [(_intent("AAA", "BUY", 10, 30.0), _preview(commission=5.0))]  # 300 + 5
    assert check_no_debit(buys, book_cash=1000.0, broker_cash=304.0)
    assert check_no_debit(buys, book_cash=304.0, broker_cash=1000.0)
    assert check_no_debit(buys, book_cash=305.0, broker_cash=305.0) is None


def test_no_debit_reserve_when_preview_has_no_commission_and_unknown_cash_refuses():
    buys = [(_intent("AAA", "BUY", 10, 30.0), _preview(commission=None))]
    assert check_no_debit(buys, book_cash=300.5, broker_cash=1000.0)  # 300 + 1 reserve
    assert check_no_debit(buys, book_cash=1000.0, broker_cash=None) == "broker cash is unknown"
    assert check_no_debit(buys, book_cash=float("nan"), broker_cash=1000.0)
    assert check_no_debit([], book_cash=0.0, broker_cash=None) is None
