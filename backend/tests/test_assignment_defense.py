"""Tests for the ex-dividend early-assignment defense (#130).

A short call on an American-style dividend payer spanning an ex-date is an
early-assignment candidate — a No-Stock Mandate P1. The entry-side hard
block and the Layer A P1 rule are pinned here, plus the calendar-staleness
guard that keeps the static calendar from silently running out.
"""

import datetime

from backend.assignment_defense import (
    entry_ex_div_block,
    ex_div_within,
    short_call_assignment_alert,
    short_put_assignment_alert,
)
from backend.calendars import EX_DIV_CALENDAR, stale_calendars
from backend.observation import run_lifecycle_scan
from backend.opportunity import generate_trade_spec
from backend.tests.test_experiment_matrix import _position
from backend.tests.test_opportunity import _make_market_state, _make_playbook, _make_portfolio_config

TODAY = datetime.date(2026, 8, 18)
SPY_EX = datetime.date(2026, 9, 18)


class TestCalendar:
    def test_window_is_exclusive_start_inclusive_end(self):
        assert ex_div_within("SPY", SPY_EX, SPY_EX + datetime.timedelta(days=30)) != "2026-09-18"
        assert ex_div_within("SPY", SPY_EX - datetime.timedelta(days=1), SPY_EX) == "2026-09-18"

    def test_unknown_symbol_has_no_dates(self):
        assert ex_div_within("XSP", TODAY, TODAY + datetime.timedelta(days=365)) is None
        assert ex_div_within("GLD", TODAY, TODAY + datetime.timedelta(days=365)) is None

    def test_stale_calendars_fire_near_the_horizon(self):
        assert stale_calendars(TODAY) == []
        last_spy = datetime.date.fromisoformat(EX_DIV_CALENDAR["SPY"][-1])
        assert "ex-div SPY" in stale_calendars(last_spy - datetime.timedelta(days=30))


class TestEntryBlock:
    def test_short_call_spanning_ex_div_is_blocked(self):
        reason = entry_ex_div_block(
            "SPY", has_short_call=True, today=TODAY, expiration=SPY_EX + datetime.timedelta(days=7)
        )
        assert reason is not None and "ex-dividend" in reason

    def test_put_side_and_non_spanning_and_immune_underlyings_pass(self):
        exp = SPY_EX + datetime.timedelta(days=7)
        assert entry_ex_div_block("SPY", has_short_call=False, today=TODAY, expiration=exp) is None
        assert (
            entry_ex_div_block("SPY", has_short_call=True, today=TODAY, expiration=datetime.date(2026, 9, 11)) is None
        )
        assert entry_ex_div_block("XSP", has_short_call=True, today=TODAY, expiration=exp) is None

    def test_spec_generation_hard_blocks_spanning_bear_call(self):
        # target_dte 38 from 2026-08-18 expires late September — across 09-18.
        pb = _make_playbook(pb_id="bcs", strategy="BEAR_CALL_SPREAD", target_dte=38)
        result = generate_trade_spec(pb, _make_market_state(), [], _make_portfolio_config(), today=TODAY)
        assert result.spec is None
        assert "EX_DIV_ASSIGNMENT" in [b.check for b in result.hard_blocks]

    def test_spec_generation_allows_the_put_side_alternative(self):
        pb = _make_playbook(pb_id="bps", strategy="BULL_PUT_SPREAD", target_dte=38)
        result = generate_trade_spec(pb, _make_market_state(), [], _make_portfolio_config(), today=TODAY)
        assert result.spec is not None


def _short_call_legs(strike: float, expiration: str) -> list[dict]:
    return [
        {"direction": "SHORT", "option_type": "CALL", "strike": strike, "expiration": expiration},
        {"direction": "LONG", "option_type": "CALL", "strike": strike + 5.0, "expiration": expiration},
    ]


class TestLayerAAlert:
    _NEAR = datetime.date(2026, 9, 16)  # 2 trading days before the 09-18 ex-date

    def test_itm_short_call_near_ex_date_is_flagged(self):
        reason = short_call_assignment_alert("SPY", _short_call_legs(750.0, "2026-09-25"), 760.0, self._NEAR)
        assert reason is not None and "assignment risk" in reason

    def test_otm_or_distant_or_priceless_is_quiet(self):
        legs = _short_call_legs(750.0, "2026-09-25")
        assert short_call_assignment_alert("SPY", _short_call_legs(765.0, "2026-09-25"), 760.0, self._NEAR) is None
        assert short_call_assignment_alert("SPY", legs, 760.0, TODAY) is None  # ex-date ~1 month out
        assert short_call_assignment_alert("SPY", legs, None, self._NEAR) is None

    def test_lifecycle_scan_promotes_assignment_risk_to_p1(self):
        pos = _position(strategy="BEAR_CALL_SPREAD", expiration="2026-09-25")
        pos = pos.model_copy(
            update={
                "legs": [
                    leg.model_copy(update={"option_type": "CALL", "direction": d, "strike": s})
                    for leg, d, s in zip(pos.legs, ("SHORT", "LONG"), (750.0, 755.0), strict=True)
                ]
            }
        )
        result = run_lifecycle_scan(pos, "CALM_BULL", 760.0, [], today=self._NEAR)
        assert result["priority"] == "P1 — CLOSE NOW"
        assert "assignment risk" in result["reason"]


def _short_put_legs(strike: float = 735.0, delta: float = -0.16) -> list[dict]:
    # delta is included deliberately (and set to a realistic, shallow
    # ENTRY-time value, ~short_leg_delta) precisely so a test that keys off
    # live price but accidentally also reads delta would be caught: at
    # -0.16 the old (dead) delta-threshold check would never have fired.
    return [
        {"direction": "SHORT", "option_type": "PUT", "strike": strike, "expiration": "2026-10-30", "delta": delta},
        {
            "direction": "LONG",
            "option_type": "PUT",
            "strike": strike - 5.0,
            "expiration": "2026-10-30",
            "delta": delta + 0.05,
        },
    ]


class TestPutSideLayerAAlert:
    """#736: interest-carry early-exercise risk for short puts. No calendar,
    no entry-side block — Layer A only.

    Reads LIVE underlying_price (the same input the call-side alert takes),
    never the leg's stored delta — delta is stamped once at entry and never
    refreshed post-entry (assignment_defense.py's module docstring). Every
    fixture below stamps a realistic, shallow ENTRY-time delta (~-0.16) on
    its short leg specifically so a regression that resurrects a delta read
    would be caught: at -0.16, a delta-keyed check could never fire."""

    def test_deep_itm_short_put_is_flagged(self):
        # Strike 735, live price 690: (735-690)/735 = 6.1% below strike, over PUT_DEEP_ITM_PCT.
        reason = short_put_assignment_alert("SPY", _short_put_legs(strike=735.0), underlying_price=690.0)
        assert reason is not None and "assignment risk" in reason and "carry" in reason

    def test_shallow_itm_or_otm_or_priceless_is_quiet(self):
        legs = _short_put_legs(strike=735.0)
        assert short_put_assignment_alert("SPY", legs, underlying_price=730.0) is None  # 0.7% ITM, shallow
        assert short_put_assignment_alert("SPY", legs, underlying_price=740.0) is None  # OTM
        assert short_put_assignment_alert("SPY", legs, underlying_price=None) is None  # no telemetry

    def test_predicate_never_reads_leg_delta(self):
        # #736 rework: the coordinator's finding was that a delta-keyed
        # version of this alert is dead code, because delta is frozen at
        # entry and every short put's entry delta (~0.16-0.30 by playbook
        # construction) can never cross a "deep ITM" delta threshold. Pin
        # that the predicate is driven ENTIRELY by underlying_price: an
        # extreme (physically impossible) delta on the stored leg must
        # neither fire an otherwise-OTM call nor suppress an otherwise-ITM
        # one — because it's never read at all.
        legs = _short_put_legs(strike=735.0, delta=-0.999)  # deep-ITM-shaped delta
        assert short_put_assignment_alert("SPY", legs, underlying_price=740.0) is None  # still OTM by price
        legs_shallow_delta = _short_put_legs(strike=735.0, delta=-0.01)  # far-OTM-shaped delta
        reason = short_put_assignment_alert("SPY", legs_shallow_delta, underlying_price=690.0)
        assert reason is not None  # still fires: price says deep ITM regardless of delta

    def test_xsp_is_immune_but_gld_is_not(self):
        # Unlike the call side (dividend-driven, GLD pays none so it's
        # exempt), interest-carry applies to GLD too — only the
        # European/cash-settled XSP is immune here.
        legs = _short_put_legs(strike=735.0)
        assert short_put_assignment_alert("XSP", legs, underlying_price=690.0) is None
        assert short_put_assignment_alert("GLD", legs, underlying_price=690.0) is not None

    def test_lifecycle_scan_promotes_put_assignment_risk_to_p1(self):
        pos = _position(strategy="BULL_PUT_SPREAD", underlying="SPY")
        short_leg = pos.legs[0]
        assert short_leg.direction == "SHORT" and short_leg.option_type == "PUT"
        deep_itm_price = short_leg.strike * (1 - 0.10)  # 10% below strike, well past the 5% threshold
        result = run_lifecycle_scan(pos, "CALM_BULL", deep_itm_price, [], today=TODAY)
        assert result["priority"] == "P1 — CLOSE NOW"
        assert "carry" in result["reason"]
