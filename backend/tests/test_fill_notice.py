"""Plain-English fill headlines (#1115): worked examples per strategy, the
real B10 GLD fill pinned, and the fail-soft paths (None, never a guess)."""

import datetime

import pytest

from backend.fill_notice import (
    ExecutionRow,
    Leg,
    OrderContext,
    article,
    classify,
    describe_close,
    describe_fill,
    describe_option_order,
    describe_share,
    legs_from_order,
    money,
    parse_occ,
    parse_order_ref,
    signed_money,
)

NOV20 = "261120"


def _ex(ref: str, side: str, symbol: str, price: float, qty: float = 1.0, mult: float | None = None) -> ExecutionRow:
    row: ExecutionRow = {"order_ref": ref, "side": side, "quantity": qty, "price": price, "symbol": symbol}
    if mult is not None:
        row["multiplier"] = mult
    return row


def _occ(root: str, cp: str, strike: float, ymd: str = NOV20, pad: bool = True) -> str:
    sep = " " * (6 - len(root)) if pad else ""
    return f"{root}{sep}{ymd}{cp}{round(strike * 1000):08d}"


B10_REF = "basis:B10:o_8bd5e627:open"
B10_FILL = [
    _ex(B10_REF, "BOT", "GLD   261120P00380000", 10.70),
    _ex(B10_REF, "SLD", "GLD   261120P00375000", 8.35),
]


class TestPinnedB10GoldFill:
    """The real fill that motivated #1115: BOT GLD 380P @ 10.70, SLD 375P @
    8.35, one contract — a $2.35 debit on a $5-wide bear put spread."""

    def test_headline_reads_exactly(self):
        assert describe_fill(B10_REF, B10_FILL) == (
            "B10 opened a GLD bear put spread (bets GLD falls). Paid $235. "
            "Max gain $265 if GLD ≤ $375 on Nov 20; max loss $235 if GLD > $380. Breakeven $377.65."
        )

    def test_with_database_context_still_reads_the_same(self):
        ctx = OrderContext(
            strategy_type="BEAR_PUT_SPREAD",
            order_quantity=1,
            leg_occs=("GLD261120P00380000", "GLD261120P00375000"),
        )
        text = describe_fill(B10_REF, B10_FILL, ctx)
        assert text is not None
        for part in ("Paid $235", "Max gain $265 if GLD ≤ $375 on Nov 20", "max loss $235 if GLD > $380", "$377.65"):
            assert part in text

    def test_close_reports_realized_pnl_and_reason(self):
        ref = "basis:B10:o_8bd5e627:open:tp"
        close = [_ex(ref, "SLD", "GLD   261120P00380000", 14.00), _ex(ref, "BOT", "GLD   261120P00375000", 10.45)]
        ctx = OrderContext(exit_trigger="PROFIT_TARGET", entry_premium=2.35, premium_direction="DEBIT")
        assert describe_fill(ref, close, ctx) == (
            "B10 closed its GLD bear put spread for +$120 (profit target). Collected $355 to close; entry paid $235."
        )


class TestVerticals:
    def test_bull_put_credit_spread(self):
        ref = "basis:B07:o_1:open"
        fill = [_ex(ref, "SLD", _occ("XSP", "P", 570), 1.85), _ex(ref, "BOT", _occ("XSP", "P", 565), 1.36)]
        assert describe_fill(ref, fill) == (
            "B07 opened an XSP bull put spread (bets XSP rises). Collected $49. "
            "Max gain $49 if XSP ≥ $570 on Nov 20; max loss $451 if XSP < $565. Breakeven $569.51."
        )

    def test_bear_call_credit_spread_two_contracts(self):
        ref = "basis:B12:o_2:open"
        fill = [
            _ex(ref, "SLD", _occ("XSP", "C", 600), 2.10, qty=2),
            _ex(ref, "BOT", _occ("XSP", "C", 605), 1.26, qty=2),
        ]
        assert describe_fill(ref, fill) == (
            "B12 opened 2 XSP bear call spreads (bets XSP falls). Collected $168. "
            "Max gain $168 if XSP ≤ $600 on Nov 20; max loss $832 if XSP > $605. Breakeven $600.84."
        )

    def test_bull_call_debit_spread(self):
        ref = "basis:B03:o_3:open"
        fill = [_ex(ref, "BOT", _occ("SPY", "C", 580), 6.00), _ex(ref, "SLD", _occ("SPY", "C", 590), 2.50)]
        text = describe_fill(ref, fill)
        assert text == (
            "B03 opened a SPY bull call spread (bets SPY rises). Paid $350. "
            "Max gain $650 if SPY ≥ $590 on Nov 20; max loss $350 if SPY < $580. Breakeven $583.50."
        )

    def test_credit_close_at_a_loss_with_stop(self):
        ref = "basis:B07:o_9:close"
        close = [_ex(ref, "BOT", _occ("XSP", "P", 570), 3.10), _ex(ref, "SLD", _occ("XSP", "P", 565), 1.20)]
        ctx = OrderContext(
            strategy_type="BULL_PUT_SPREAD", exit_trigger="LOSS_LIMIT", entry_premium=0.49, premium_direction="CREDIT"
        )
        assert describe_fill(ref, close, ctx) == (
            "B07 closed its XSP bull put spread for −$141 (stop). Paid $190 to close; entry collected $49."
        )

    def test_close_without_known_entry_still_reads(self):
        ref = "basis:B07:o_9:close"
        close = [_ex(ref, "BOT", _occ("XSP", "P", 570), 3.10, 2), _ex(ref, "SLD", _occ("XSP", "P", 565), 1.20, 2)]
        assert describe_fill(ref, close, OrderContext(exit_trigger="SOMETHING_NEW")) == (
            "B07 closed its 2 XSP bull put spreads. Paid $380 to close."
        )


class TestMultiLeg:
    def test_iron_condor(self):
        ref = "basis:B07:o_2:open"
        fill = [
            _ex(ref, "BOT", _occ("XSP", "P", 540), 0.50),
            _ex(ref, "SLD", _occ("XSP", "P", 550), 1.20),
            _ex(ref, "SLD", _occ("XSP", "C", 600), 1.10),
            _ex(ref, "BOT", _occ("XSP", "C", 610), 0.40),
        ]
        assert describe_fill(ref, fill) == (
            "B07 opened an XSP iron condor (bets XSP stays between $550 and $600). Collected $140. "
            "Max gain $140 if XSP is between $550 and $600 on Nov 20; max loss $860 if XSP < $540 or XSP > $610. "
            "Breakevens $548.60 and $601.40."
        )

    def test_broken_wing_butterfly_body_fills_at_twice_the_quantity(self):
        ref = "basis:B05:o_4:open"
        fill = [
            _ex(ref, "BOT", _occ("SPY", "P", 600), 9.00),
            _ex(ref, "SLD", _occ("SPY", "P", 590), 5.00, qty=2),
            _ex(ref, "BOT", _occ("SPY", "P", 570), 1.50),
        ]
        ctx = OrderContext(
            strategy_type="BROKEN_WING_BUTTERFLY",
            order_quantity=1,
            leg_occs=(
                "SPY261120P00600000",
                "SPY261120P00590000",
                "SPY261120P00590000",
                "SPY261120P00570000",
            ),
        )
        assert describe_fill(ref, fill, ctx) == (
            "B05 opened a SPY broken-wing butterfly (bets SPY settles near $590). Paid $50. "
            "Max gain $950 if SPY is at $590 on Nov 20; max loss $1,050 if SPY < $570. "
            "Breakevens $580.50 and $599.50."
        )

    def test_calendar_spread_states_only_what_is_fixed(self):
        ref = "basis:B04:o_5:open"
        fill = [
            _ex(ref, "SLD", _occ("SPY", "C", 590, "261120"), 4.00),
            _ex(ref, "BOT", _occ("SPY", "C", 590, "261218"), 6.25),
        ]
        assert describe_fill(ref, fill) == (
            "B04 opened a SPY calendar spread (bets SPY settles near $590). Paid $225. "
            "Max loss $225 (the debit); no fixed max gain, it depends on volatility at the Nov 20 expiry."
        )

    def test_long_straddle(self):
        ref = "basis:B08:o_3:open"
        fill = [
            _ex(ref, "BOT", _occ("SPY", "C", 580), 8.10, qty=2),
            _ex(ref, "BOT", _occ("SPY", "P", 580), 7.40, qty=2),
        ]
        assert describe_fill(ref, fill) == (
            "B08 opened 2 SPY long straddles (bets SPY makes a big move either way). Paid $3,100. "
            "Max gain unlimited; max loss $3,100 if SPY is at $580 on Nov 20. Breakevens $564.50 and $595.50."
        )

    def test_long_strangle(self):
        ref = "basis:B09:o_6:open"
        fill = [_ex(ref, "BOT", _occ("SPY", "C", 600), 3.00), _ex(ref, "BOT", _occ("SPY", "P", 560), 2.50)]
        assert describe_fill(ref, fill) == (
            "B09 opened a SPY long strangle (bets SPY makes a big move either way). Paid $550. "
            "Max gain unlimited; max loss $550 if SPY is between $560 and $600 on Nov 20. "
            "Breakevens $554.50 and $605.50."
        )

    def test_long_put_tail_hedge(self):
        ref = "basis:B32:o_7:open"
        fill = [_ex(ref, "BOT", _occ("XSP", "P", 500), 3.17)]
        assert describe_fill(ref, fill) == (
            "B32 opened an XSP long put (bets XSP falls). Paid $317. "
            "Max gain $49,683 if XSP goes to $0 on Nov 20; max loss $317 if XSP > $500. Breakeven $496.83."
        )

    def test_contract_multiplier_scales_amounts(self):
        ref = "basis:B10:o_x:open"
        fill = [_ex(ref, side, sym, px, mult=10) for side, sym, px in (("BOT", B10_FILL[0]["symbol"], 10.70),)]
        fill.append(_ex(ref, "SLD", B10_FILL[1]["symbol"], 8.35, mult=10))
        text = describe_fill(ref, fill)
        assert text is not None and "Paid $23.50" in text


class TestShareOrders:
    def test_share_buy(self):
        ref = "basis:B36:s_1:share"
        assert describe_fill(ref, [_ex(ref, "BOT", "SCHB", 25.10, qty=12)]) == "B36 bought 12 SCHB @ $25.10 ($301.20)."

    def test_share_sell_split_across_executions_uses_weighted_price(self):
        ref = "basis:B36:s_2:share"
        rows = [_ex(ref, "SLD", "AGG", 100.0, qty=3), _ex(ref, "SLD", "AGG", 101.0, qty=1)]
        assert describe_fill(ref, rows) == "B36 sold 4 AGG @ $100.25 ($401)."

    def test_share_partial_says_so(self):
        ref = "basis:B36:s_3:share"
        text = describe_fill(ref, [_ex(ref, "BOT", "SCHB", 25.0, qty=5)], OrderContext(share_quantity=12))
        assert text == "B36 bought 5 SCHB @ $25 ($125). Partial: 5 of 12 shares filled so far."

    def test_mixed_share_rows_fall_back(self):
        ref = "basis:B36:s_4:share"
        rows = [_ex(ref, "BOT", "SCHB", 25.0), _ex(ref, "BOT", "AGG", 100.0)]
        assert describe_fill(ref, rows) is None

    def test_describe_share_buy_side_word(self):
        assert describe_share("B36", "SCHB", "BUY", 1, 25) == "B36 bought 1 SCHB @ $25 ($25)."


class TestFailSoft:
    """Every doubt returns None — the caller then sends the raw line."""

    def test_partial_combo_one_leg_missing(self):
        assert describe_fill(B10_REF, B10_FILL[:1], OrderContext(strategy_type="BEAR_PUT_SPREAD")) is None

    def test_partial_combo_against_ordered_legs(self):
        ctx = OrderContext(order_quantity=2, leg_occs=("GLD261120P00380000", "GLD261120P00375000"))
        assert describe_fill(B10_REF, B10_FILL, ctx) is None  # 1 of 2 filled

    def test_partial_combo_quantity_without_legs(self):
        assert describe_fill(B10_REF, B10_FILL, OrderContext(order_quantity=3)) is None

    def test_ordered_legs_do_not_match_filled_symbols(self):
        ctx = OrderContext(order_quantity=1, leg_occs=("GLD261120P00380000", "GLD261120P00370000"))
        assert describe_fill(B10_REF, B10_FILL, ctx) is None

    def test_unequal_leg_quantities_without_context(self):
        fill = [B10_FILL[0], _ex(B10_REF, "SLD", "GLD   261120P00375000", 8.35, qty=2)]
        assert describe_fill(B10_REF, fill) is None  # 1×2 ratio vertical is no known shape

    def test_strategy_mismatch(self):
        assert describe_fill(B10_REF, B10_FILL, OrderContext(strategy_type="BULL_PUT_SPREAD")) is None

    def test_unparseable_symbol(self):
        fill = [_ex(B10_REF, "BOT", "XSP P768", 1.0), _ex(B10_REF, "SLD", "XSP P765", 0.5)]
        assert describe_fill(B10_REF, fill) is None

    def test_foreign_or_unknown_refs(self):
        assert describe_fill("manual", B10_FILL) is None
        assert describe_fill("basis:B10:o_1:roll", B10_FILL) is None
        assert describe_fill("basis:B10:o_1:open:weird", B10_FILL) is None
        assert describe_fill(B10_REF, []) is None

    def test_bad_side_or_quantity(self):
        assert describe_fill(B10_REF, [_ex(B10_REF, "BUY", "GLD   261120P00380000", 1.0)]) is None
        assert describe_fill(B10_REF, [_ex(B10_REF, "BOT", "GLD   261120P00380000", 1.0, qty=0)]) is None
        assert describe_fill(B10_REF, [_ex(B10_REF, "BOT", "GLD   261120P00380000", 1.0, qty=1.5)]) is None

    def test_same_contract_bought_and_sold(self):
        fill = [B10_FILL[0], _ex(B10_REF, "SLD", "GLD   261120P00380000", 10.0)]
        assert describe_fill(B10_REF, fill) is None

    def test_two_underlyings(self):
        fill = [B10_FILL[0], _ex(B10_REF, "SLD", _occ("SLV", "P", 375), 8.35)]
        assert describe_fill(B10_REF, fill) is None

    def test_mixed_multipliers(self):
        fill = [{**B10_FILL[0], "multiplier": 100.0}, {**B10_FILL[1], "multiplier": 10.0}]
        assert describe_fill(B10_REF, fill) is None  # type: ignore[arg-type]

    def test_roll_kind_is_not_described(self):
        legs = [Leg("PUT", "LONG", 380, datetime.date(2026, 11, 20))]
        assert describe_option_order("B10", "SHARE", "GLD", legs, 1.0, 1) is None


class TestClassify:
    D1 = datetime.date(2026, 11, 20)
    D2 = datetime.date(2026, 12, 18)

    @pytest.mark.parametrize(
        "legs",
        [
            [Leg("CALL", "LONG", 580, D1)],  # long call: not traded
            [Leg("PUT", "LONG", 580, D1, ratio=2)],
            [Leg("PUT", "LONG", 580, D1), Leg("PUT", "LONG", 570, D1)],  # same direction
            [Leg("PUT", "LONG", 580, D1), Leg("PUT", "SHORT", 580, D1)],  # same strike
            [Leg("CALL", "SHORT", 580, D1), Leg("PUT", "SHORT", 580, D1)],  # short straddle
            [Leg("CALL", "LONG", 560, D1), Leg("PUT", "LONG", 600, D1)],  # inverted strangle
            [Leg("CALL", "SHORT", 580, D1), Leg("CALL", "LONG", 580, D2), Leg("CALL", "LONG", 590, D2)],
            [Leg("CALL", "LONG", 580, D1), Leg("CALL", "SHORT", 580, D2)],  # reverse calendar
            [Leg("PUT", "LONG", 570, D1), Leg("PUT", "SHORT", 590, D1), Leg("CALL", "LONG", 600, D1)],
            [
                Leg("PUT", "LONG", 540, D1),
                Leg("PUT", "SHORT", 600, D1),
                Leg("CALL", "SHORT", 550, D1),
                Leg("CALL", "LONG", 610, D1),
            ],  # crossed condor
            [
                Leg("PUT", "LONG", 540, D1),
                Leg("PUT", "SHORT", 550, D1),
                Leg("PUT", "LONG", 560, D1),
                Leg("CALL", "LONG", 610, D1),
            ],
            [Leg("PUT", "LONG", 1, D1)] * 5,
        ],
    )
    def test_unknown_shapes(self, legs):
        assert classify(legs) is None


class TestHelpers:
    def test_money_and_signed(self):
        assert money(235) == "$235"
        assert money(-377.651) == "$377.65"
        assert money(1250.5) == "$1,250.50"
        assert signed_money(120) == "+$120"
        assert signed_money(-80) == "−$80"

    def test_article(self):
        assert article("XSP") == "an"
        assert article("GLD") == "a"

    def test_parse_occ(self):
        assert parse_occ("GLD   261120P00380000") == ("GLD", datetime.date(2026, 11, 20), "PUT", 380.0)
        assert parse_occ("AAPL261120C00232500") == ("AAPL", datetime.date(2026, 11, 20), "CALL", 232.5)
        assert parse_occ("GLD   261320P00380000") is None  # month 13
        assert parse_occ("SCHB") is None

    def test_parse_order_ref(self):
        assert parse_order_ref("basis:B10:o_1:open") == ("B10", "OPEN")
        assert parse_order_ref("basis:B10:o_1:open:tp") == ("B10", "CLOSE")
        assert parse_order_ref("basis:B10:o_1:close") == ("B10", "CLOSE")
        assert parse_order_ref("basis:B36:s_1:share") == ("B36", "SHARE")
        assert parse_order_ref("basis:B10") is None

    def test_legs_from_order_collapses_ratio_expansion(self):
        raw = [
            {"option_type": "PUT", "direction": "LONG", "strike": 600.0, "expiration": "2026-11-20"},
            {"option_type": "PUT", "direction": "SHORT", "strike": 590.0, "expiration": "2026-11-20"},
            {"option_type": "PUT", "direction": "SHORT", "strike": 590.0, "expiration": "2026-11-20"},
            {"option_type": "PUT", "direction": "LONG", "strike": 570.0, "expiration": "2026-11-20"},
        ]
        legs = legs_from_order(raw)
        assert legs is not None
        assert [(leg.strike, leg.ratio) for leg in legs] == [(570.0, 1), (590.0, 2), (600.0, 1)]
        assert classify(legs) == "BROKEN_WING_BUTTERFLY"

    @pytest.mark.parametrize(
        "raw",
        [
            [],
            [{"option_type": "BOTH", "direction": "LONG", "strike": 1.0, "expiration": "2026-11-20"}],
            [{"option_type": "PUT", "direction": "LONG", "strike": "1", "expiration": "2026-11-20"}],
            [{"option_type": "PUT", "direction": "LONG", "strike": 1.0, "expiration": "not-a-date"}],
        ],
    )
    def test_legs_from_order_rejects_malformed(self, raw):
        assert legs_from_order(raw) is None

    def test_describe_close_credit_without_reason(self):
        assert describe_close("B07", "XSP", "IRON_CONDOR", 0.30, 1, 100, None, 1.40, "CREDIT") == (
            "B07 closed its XSP iron condor for +$110. Paid $30 to close; entry collected $140."
        )
