"""Tests for the ex-dividend early-assignment defense (#130).

A short call on an American-style dividend payer spanning an ex-date is an
early-assignment candidate — a No-Stock Mandate P1. The entry-side hard
block and the Layer A P1 rule are pinned here, plus the calendar-staleness
guard that keeps the static calendar from silently running out.
"""

import datetime

from backend.assignment_defense import (
    EX_DIV_CALENDAR,
    entry_ex_div_block,
    ex_div_within,
    short_call_assignment_alert,
    stale_calendars,
)
from backend.observation import run_lifecycle_scan
from backend.opportunity import generate_trade_spec
from backend.tests.test_experiment_matrix import _position
from backend.tests.test_sprint4 import _make_market_state, _make_playbook, _make_portfolio_config

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
        assert "SPY" in stale_calendars(last_spy - datetime.timedelta(days=30))


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
