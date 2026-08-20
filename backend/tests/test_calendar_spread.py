"""Tests for the calendar-spread strategy type (#133).

Long ATM call calendar: SELL the front expiry, BUY the same strike one
monthly cycle back, net debit. Conventions under test: the position's
expiration is the FRONT leg, max_loss = max_profit = debit paid, no
analytic break-evens. Races only in book B21 (XSP — cash-settled front
short), which whitelists and re-enables the globally disabled seed.
"""

import datetime

from backend.book_gates import resolve_book_config
from backend.eligibility import REGIME_ALLOWED_STRATEGIES
from backend.executor import _book_playbooks
from backend.models import PlaybookDefinitionSchema
from backend.opportunity import generate_trade_spec
from backend.pricing import calculate_position_metrics
from backend.seeds import LAB_BOOKS, SEED_PLAYBOOKS
from backend.tests.test_opportunity import _make_market_state, _make_playbook, _make_portfolio_config

TODAY = datetime.date(2026, 8, 18)


def _calendar_playbook() -> PlaybookDefinitionSchema:
    # XSP, like B21: a SPY calendar's front short call spanning an ex-div
    # date is (correctly) hard-blocked by the #130 assignment defense.
    pb = _make_playbook(pb_id="cal", strategy="CALENDAR_SPREAD", min_ivr=0.0, vix_min=10.0, target_dte=30)
    return pb.model_copy(update={"underlying_ticker": "XSP"})


class TestPricingConventions:
    def test_debit_is_both_max_loss_and_the_stated_max_profit(self):
        legs = [
            {"option_type": "CALL", "direction": "SHORT", "strike": 760.0},
            {"option_type": "CALL", "direction": "LONG", "strike": 760.0},
        ]
        res = calculate_position_metrics(
            strategy_type="CALENDAR_SPREAD", legs=legs, entry_premium=3.0, premium_direction="DEBIT"
        )
        assert res["max_loss"] == 3.0
        assert res["max_profit"] == 3.0  # stated 1:1 convention — not analytic
        assert res["break_even_upside"] is None and res["break_even_downside"] is None


class TestSpecGeneration:
    def test_two_legs_same_strike_different_expiries_front_is_the_position_expiry(self):
        result = generate_trade_spec(
            _calendar_playbook(), _make_market_state(), [], _make_portfolio_config(), today=TODAY
        )
        assert result.spec is not None
        front, back = result.spec.legs
        assert (front.action, back.action) == ("SELL", "BUY")
        assert front.option_type == back.option_type == "CALL"
        assert front.strike == back.strike
        assert front.expiration_date < back.expiration_date
        # Back leg sits ~one monthly cycle behind the front, Friday-snapped.
        gap = datetime.date.fromisoformat(back.expiration_date) - datetime.date.fromisoformat(front.expiration_date)
        assert 28 <= gap.days <= 34
        assert datetime.date.fromisoformat(back.expiration_date).weekday() == 4
        # The position's expiration is the FRONT leg — DTE rules key off it.
        assert result.spec.expiration_date == front.expiration_date
        # Debit economics: the whole debit is the defined risk.
        assert result.spec.max_loss_dollars == result.spec.limit_price_per_share * 100

    def test_spy_calendar_spanning_ex_div_is_blocked(self):
        # The front SHORT call on SPY across 2026-09-18 trips the #130
        # defense — the reason the calendar arm lives on cash-settled XSP.
        spy_pb = _make_playbook(pb_id="cal_spy", strategy="CALENDAR_SPREAD", min_ivr=0.0, vix_min=10.0, target_dte=30)
        result = generate_trade_spec(spy_pb, _make_market_state(), [], _make_portfolio_config(), today=TODAY)
        assert result.spec is None
        assert "EX_DIV_ASSIGNMENT" in [b.check for b in result.hard_blocks]

    def test_calendar_is_neutral_and_calm_regime_gated(self):
        assert "CALENDAR_SPREAD" in REGIME_ALLOWED_STRATEGIES["CALM_BULL"]
        assert "CALENDAR_SPREAD" in REGIME_ALLOWED_STRATEGIES["HIGH_VOL_NEUTRAL"]
        assert "CALENDAR_SPREAD" not in REGIME_ALLOWED_STRATEGIES["TRENDING_BEAR"]


class TestB21Wiring:
    def test_b21_whitelists_and_reenables_the_disabled_seed(self):
        b21 = next(spec for spec in LAB_BOOKS if spec["id"] == "B21")
        playbooks = [PlaybookDefinitionSchema(**pb) for pb in SEED_PLAYBOOKS]
        selected = _book_playbooks(playbooks, resolve_book_config(b21["config"]))
        assert [pb.id for pb in selected] == ["spy_calendar_spread_v1"]
        assert selected[0].enabled is True
        assert selected[0].underlying_ticker == "XSP"
        # The front short leg must be cash-settled — XSP only.
        assert b21["config"]["underlying"] == "XSP"
        # An ATM calendar debit runs ~$300: this arm's envelope raises the
        # per-trade cap to 4%, and that choice is part of its fingerprint.
        assert b21["config"]["envelope"]["max_loss_pct_per_trade"] == 4.0

    def test_seed_exit_rules_exit_before_the_front_final_week(self):
        seed = next(pb for pb in SEED_PLAYBOOKS if pb["id"] == "spy_calendar_spread_v1")
        assert seed["enabled"] is False
        assert seed["exit_rules"]["mandatory_exit_dte"] == 7
        assert seed["exit_rules"]["stop_loss_pct"] == 50.0
