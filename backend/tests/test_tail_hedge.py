"""Tests for the tail-hedge sleeve B32 and the LONG_PUT strategy (#319).

ADR-0012: the sleeve is judged on bleed vs crisis payoff, never Live Gate
expectancy — so the machinery pinned here is the structure (one far-OTM
put, whole debit at risk), the always-on entry (no vol/trend gates), and
the roll cadence (time exit at 30 DTE, re-entry next night).
"""

import datetime

from backend.models import MarketStateSchema, PlaybookDefinitionSchema, PortfolioConfigSchema
from backend.opportunity import generate_trade_spec
from backend.pricing import calculate_position_metrics
from backend.seeds import LAB_BOOKS, SEED_PLAYBOOKS, SEED_PORTFOLIO_CONFIG

TODAY = datetime.date(2026, 8, 20)


def _playbook() -> PlaybookDefinitionSchema:
    raw = next(pb for pb in SEED_PLAYBOOKS if pb["id"] == "xsp_tail_put_v1")
    return PlaybookDefinitionSchema(**raw).model_copy(update={"enabled": True})


class TestLongPutStrategy:
    def test_pricing_debit_is_the_whole_risk(self):
        metrics = calculate_position_metrics(
            "LONG_PUT",
            legs=[{"option_type": "PUT", "direction": "LONG", "strike": 700.0}],
            entry_premium=3.0,
            premium_direction="DEBIT",
        )
        assert metrics["max_loss"] == 3.0
        assert metrics["max_profit"] == 697.0  # underlying at zero
        assert metrics["break_even_downside"] == 697.0
        assert metrics["break_even_upside"] is None

    def test_spec_is_one_otm_put_below_spot(self):
        state = MarketStateSchema(
            current_regime="CALM_BULL",
            spy_price=760.0,
            spy_sma20=750.0,
            vix_close=14.5,
            underlying_ivrs={"SPY": 25.0},
            spy_daily_return=0.004,
            catalyst_dates=[],
            regime_scores={},
        )
        config = PortfolioConfigSchema(**SEED_PORTFOLIO_CONFIG)
        result = generate_trade_spec(
            _playbook(), state, positions=[], portfolio_config=config, contracts=1, today=TODAY
        )
        assert result.spec is not None, [b.reason for b in result.hard_blocks]
        (leg,) = result.spec.legs
        assert leg.action == "BUY" and leg.option_type == "PUT"
        assert leg.strike < 760.0  # far OTM, below spot
        # ~75 DTE, snapped to a Friday
        exp = datetime.date.fromisoformat(leg.expiration_date)
        assert 70 <= (exp - TODAY).days <= 85


class TestB32Seed:
    def test_b32_is_the_promotion_excluded_sleeve(self):
        b32 = next(spec for spec in LAB_BOOKS if spec["id"] == "B32")
        cfg = b32["config"]
        assert cfg["playbook_ids"] == ["xsp_tail_put_v1"]
        assert cfg["ignore_regime"] is True
        assert cfg["envelope"]["max_loss_pct_per_trade"] == 4.0
        # Two slots (#351): one slot made every monthly roll an uninsured
        # session — the resting close held the slot against the replacement.
        assert cfg["envelope"]["max_positions"] == 2

    def test_playbook_is_always_on_insurance(self):
        raw = next(pb for pb in SEED_PLAYBOOKS if pb["id"] == "xsp_tail_put_v1")
        assert raw["enabled"] is False  # globally off; whitelisted by B32 only
        f = raw["entry_filters"]
        # No vol/trend gating — a hedge that only buys cheap vol lapses
        # exactly when cover matters most (ADR-0012).
        assert f["min_ivr"] == 0.0 and f["max_ivr"] == 100.0
        assert f["vix_range"] == [0.0, 100.0]
        assert f["required_trend"] == "ANY"
        x = raw["exit_rules"]
        # Only the time exit (the roll) or a 4x crisis take ever close it.
        assert x["mandatory_exit_dte"] == 30
        assert x["profit_take_pct"] == 400.0
        assert x["stop_loss_pct"] == 100.0
