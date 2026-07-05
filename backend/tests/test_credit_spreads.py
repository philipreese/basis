"""
test_credit_spreads.py — Credit-spread playbooks & playbook enabled flag (issue #20)

Tests cover:
- Pricing math for BULL_PUT_SPREAD and BEAR_CALL_SPREAD (credit and debit branches)
- generate_trade_spec leg structure, credit estimate, max loss, and break-evens
- Directional-bias gates count the new spreads as bullish/bearish
- Lifecycle scan flags regime conflicts for the new spreads
- Disabled playbooks are skipped by scan_opportunities and hard-blocked from specs
- Seed library composition: every regime has an enabled premium-selling playbook,
  long straddle/strangle are disabled by default
"""

from datetime import date

from backend.models import PlaybookDefinitionSchema, PositionSchema, OptionLegSchema, OperationalJournalEntrySchema
from backend.pricing import calculate_position_metrics
from backend.observation import run_lifecycle_scan
from backend.opportunity import scan_opportunities, generate_trade_spec, _check_per_playbook_gates
from backend.database import SEED_PLAYBOOKS
from backend.tests.test_sprint4 import (
    _make_playbook,
    _make_market_state,
    _make_portfolio_config,
    _TEST_JOURNAL,
)

TODAY = date(2026, 7, 1)


# ---------------------------------------------------------------------------
# Pricing math
# ---------------------------------------------------------------------------

def _put_spread_legs(short_strike: float = 735.0, long_strike: float = 730.0) -> list[dict]:
    return [
        {"option_type": "PUT", "direction": "SHORT", "strike": short_strike},
        {"option_type": "PUT", "direction": "LONG", "strike": long_strike},
    ]


def _call_spread_legs(short_strike: float = 780.0, long_strike: float = 785.0) -> list[dict]:
    return [
        {"option_type": "CALL", "direction": "SHORT", "strike": short_strike},
        {"option_type": "CALL", "direction": "LONG", "strike": long_strike},
    ]


class TestBullPutSpreadPricing:
    def test_credit_metrics(self):
        res = calculate_position_metrics("BULL_PUT_SPREAD", _put_spread_legs(), 1.65, "CREDIT")
        assert res["max_profit"] == 1.65
        assert res["max_loss"] == 5.0 - 1.65
        assert res["break_even_downside"] == 735.0 - 1.65
        assert res["break_even_upside"] is None

    def test_debit_fallback(self):
        res = calculate_position_metrics("BULL_PUT_SPREAD", _put_spread_legs(), 1.0, "DEBIT")
        assert res["max_profit"] == 0.0
        assert res["max_loss"] == 5.0 + 1.0
        assert res["break_even_downside"] == 735.0 + 1.0


class TestBearCallSpreadPricing:
    def test_credit_metrics(self):
        res = calculate_position_metrics("BEAR_CALL_SPREAD", _call_spread_legs(), 1.65, "CREDIT")
        assert res["max_profit"] == 1.65
        assert res["max_loss"] == 5.0 - 1.65
        assert res["break_even_upside"] == 780.0 + 1.65
        assert res["break_even_downside"] is None

    def test_debit_fallback(self):
        res = calculate_position_metrics("BEAR_CALL_SPREAD", _call_spread_legs(), 1.0, "DEBIT")
        assert res["max_profit"] == 0.0
        assert res["max_loss"] == 5.0 + 1.0
        assert res["break_even_upside"] == 780.0 - 1.0


# ---------------------------------------------------------------------------
# Trade spec generation
# ---------------------------------------------------------------------------

class TestCreditSpreadSpecs:
    def _spec_for(self, strategy: str, regime: str = "CALM_BULL"):
        pb = _make_playbook(pb_id=f"test_{strategy.lower()}", strategy=strategy,
                            min_ivr=0.0, max_ivr=100.0, short_delta=0.30, spread_width=5.0)
        state = _make_market_state(regime=regime)
        config = _make_portfolio_config()
        return generate_trade_spec(pb, state, [], config, today=TODAY)

    def test_bull_put_spread_legs(self):
        result = self._spec_for("BULL_PUT_SPREAD")
        assert result.spec is not None
        legs = result.spec.legs
        assert len(legs) == 2
        sell = next(l for l in legs if l.action == "SELL")
        buy = next(l for l in legs if l.action == "BUY")
        assert sell.option_type == "PUT" and buy.option_type == "PUT"
        # Short put above the long wing; both below spot
        assert sell.strike > buy.strike
        assert sell.strike < result.spec.derivation_params.current_price

    def test_bull_put_spread_economics(self):
        result = self._spec_for("BULL_PUT_SPREAD")
        spec = result.spec
        assert spec is not None
        sell = next(l for l in spec.legs if l.action == "SELL")
        buy = next(l for l in spec.legs if l.action == "BUY")
        width = sell.strike - buy.strike
        assert spec.limit_price_per_share == round(width / 3.0, 2)
        assert spec.max_loss_dollars == (width - spec.limit_price_per_share) * 100
        assert spec.max_gain_dollars == spec.limit_price_per_share * 100
        assert spec.break_even_prices == [round(sell.strike - spec.limit_price_per_share, 2)]

    def test_bear_call_spread_legs(self):
        result = self._spec_for("BEAR_CALL_SPREAD")
        assert result.spec is not None
        legs = result.spec.legs
        sell = next(l for l in legs if l.action == "SELL")
        buy = next(l for l in legs if l.action == "BUY")
        assert sell.option_type == "CALL" and buy.option_type == "CALL"
        # Short call below the long wing; both above spot
        assert sell.strike < buy.strike
        assert sell.strike > result.spec.derivation_params.current_price

    def test_bear_call_spread_economics(self):
        result = self._spec_for("BEAR_CALL_SPREAD")
        spec = result.spec
        assert spec is not None
        sell = next(l for l in spec.legs if l.action == "SELL")
        buy = next(l for l in spec.legs if l.action == "BUY")
        width = buy.strike - sell.strike
        assert spec.limit_price_per_share == round(width / 3.0, 2)
        assert spec.max_loss_dollars == (width - spec.limit_price_per_share) * 100
        assert spec.break_even_prices == [round(sell.strike + spec.limit_price_per_share, 2)]


# ---------------------------------------------------------------------------
# Directional-bias gate
# ---------------------------------------------------------------------------

def _open_credit_spread(pos_id: str, strategy: str, underlying: str = "QQQ") -> PositionSchema:
    is_put = strategy in ("BULL_PUT_SPREAD",)
    legs = _put_spread_legs() if is_put else _call_spread_legs()
    return PositionSchema(
        id=pos_id, underlying=underlying, strategy_type=strategy,  # type: ignore
        execution_mode="PAPER",
        legs=[OptionLegSchema(option_type=l["option_type"], direction=l["direction"], strike=l["strike"],
                              expiration="2026-08-15", delta=0.3 if l["option_type"] == "CALL" else -0.3,
                              theta=0.05, vega=-0.1, gamma=0.02) for l in legs],
        entry_date="2026-06-20", expiration_date="2026-08-15",
        entry_premium=1.65, premium_direction="CREDIT",
        current_value_per_share=1.65, contracts=1,
        max_profit=1.65, max_loss=3.35,
        notes="", rolls=0, status="OPEN",
        journal=_TEST_JOURNAL,
    )


class TestDirectionalBias:
    def test_bull_put_counts_as_bullish_concentration(self):
        pb = _make_playbook(pb_id="bps", strategy="BULL_PUT_SPREAD", min_ivr=0.0)
        open_pos = [
            _open_credit_spread("p1", "BULL_PUT_SPREAD", underlying="QQQ"),
            _open_credit_spread("p2", "BULL_PUT_SPREAD", underlying="IWM"),
        ]
        reason = _check_per_playbook_gates(pb, open_pos, _make_market_state())
        assert reason is not None and "DIRECTIONAL CONCENTRATION" in reason

    def test_bear_call_counts_as_bearish_concentration(self):
        pb = _make_playbook(pb_id="bcs", strategy="BEAR_CALL_SPREAD", min_ivr=0.0)
        open_pos = [
            _open_credit_spread("p1", "BEAR_CALL_SPREAD", underlying="QQQ"),
            _open_credit_spread("p2", "BEAR_CALL_SPREAD", underlying="IWM"),
        ]
        reason = _check_per_playbook_gates(pb, open_pos, _make_market_state())
        assert reason is not None and "DIRECTIONAL CONCENTRATION" in reason


# ---------------------------------------------------------------------------
# Lifecycle scan regime conflicts
# ---------------------------------------------------------------------------

class TestLifecycleRegimeConflicts:
    def test_bull_put_spread_conflicts_in_trending_bear(self):
        pos = _open_credit_spread("p1", "BULL_PUT_SPREAD", underlying="SPY")
        scan = run_lifecycle_scan(pos, current_regime="TRENDING_BEAR", spy_price=740.0,
                                  catalyst_dates=[], today=TODAY)
        assert scan["priority"] == "P2 — REVIEW"
        assert "Regime conflict" in scan["reason"]

    def test_bear_call_spread_conflicts_in_calm_bull(self):
        pos = _open_credit_spread("p1", "BEAR_CALL_SPREAD", underlying="SPY")
        scan = run_lifecycle_scan(pos, current_regime="CALM_BULL", spy_price=760.0,
                                  catalyst_dates=[], today=TODAY)
        assert scan["priority"] == "P2 — REVIEW"
        assert "Regime conflict" in scan["reason"]


# ---------------------------------------------------------------------------
# Enabled flag
# ---------------------------------------------------------------------------

class TestEnabledFlag:
    def test_schema_defaults_to_enabled(self):
        pb = _make_playbook()
        assert pb.enabled is True

    def test_disabled_playbook_skipped_by_scan(self):
        enabled_pb = _make_playbook(pb_id="on", strategy="BULL_PUT_SPREAD", min_ivr=0.0)
        disabled_pb = _make_playbook(pb_id="off", strategy="BULL_PUT_SPREAD", min_ivr=0.0)
        disabled_pb = disabled_pb.model_copy(update={"enabled": False})
        result = scan_opportunities([enabled_pb, disabled_pb], _make_market_state(), [],
                                    _make_portfolio_config(), today=TODAY)
        ids = {c.playbook.id for c in result.candidates}
        assert "on" in ids
        assert "off" not in ids

    def test_disabled_playbook_hard_blocks_spec(self):
        pb = _make_playbook(pb_id="off", strategy="BULL_PUT_SPREAD", min_ivr=0.0)
        pb = pb.model_copy(update={"enabled": False})
        result = generate_trade_spec(pb, _make_market_state(), [], _make_portfolio_config(), today=TODAY)
        assert result.spec is None
        assert any(b.check == "PLAYBOOK_DISABLED" for b in result.hard_blocks)


# ---------------------------------------------------------------------------
# Seed library composition
# ---------------------------------------------------------------------------

class TestSeedLibrary:
    def test_seed_ids_and_count(self):
        ids = {pb["id"] for pb in SEED_PLAYBOOKS}
        assert "spy_bull_put_spread_v1" in ids
        assert "spy_bear_call_spread_v1" in ids
        assert len(SEED_PLAYBOOKS) == 7

    def test_long_vol_playbooks_disabled_by_default(self):
        by_id = {pb["id"]: pb for pb in SEED_PLAYBOOKS}
        assert by_id["spy_long_straddle_v1"]["enabled"] is False
        assert by_id["spy_long_strangle_v1"]["enabled"] is False

    def test_every_regime_has_enabled_premium_selling_playbook(self):
        enabled = [pb for pb in SEED_PLAYBOOKS if pb.get("enabled", True)]
        strategies = {pb["strategy_type"] for pb in enabled}
        # CALM_BULL income, TRENDING_BEAR income, HIGH_VOL_NEUTRAL income
        assert {"BULL_PUT_SPREAD", "BEAR_CALL_SPREAD", "IRON_CONDOR"} <= strategies

    def test_seeds_validate_against_schema(self):
        for pb in SEED_PLAYBOOKS:
            parsed = PlaybookDefinitionSchema(**pb)
            assert parsed.id == pb["id"]
