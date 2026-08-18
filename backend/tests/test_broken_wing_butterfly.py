"""Tests for the broken-wing butterfly strategy type (#132).

Put-side BWB entered for a credit: +1 put at the narrow upper wing, -2 at
the body, +1 at the 2×-wide lower wing. No upside risk; defined downside
risk = (wide − narrow) − credit. Races only in book B18, which whitelists
the globally disabled seed playbook and re-enables it via overrides.
"""

from backend.book_gates import resolve_book_config
from backend.database import LAB_BOOKS, SEED_PLAYBOOKS
from backend.eligibility import REGIME_ALLOWED_STRATEGIES
from backend.executor import _book_playbooks
from backend.models import PlaybookDefinitionSchema
from backend.opportunity import generate_trade_spec, scan_opportunities
from backend.pricing import calculate_position_metrics
from backend.tests.test_assignment_defense import TODAY
from backend.tests.test_sprint4 import _make_market_state, _make_playbook, _make_portfolio_config


def _bwb_metrics(entry_premium: float = 0.75, direction: str = "CREDIT") -> dict:
    legs = [
        {"option_type": "PUT", "direction": "LONG", "strike": 753.0},
        {"option_type": "PUT", "direction": "SHORT", "strike": 750.0},
        {"option_type": "PUT", "direction": "SHORT", "strike": 750.0},
        {"option_type": "PUT", "direction": "LONG", "strike": 744.0},
    ]
    return calculate_position_metrics(
        strategy_type="BROKEN_WING_BUTTERFLY", legs=legs, entry_premium=entry_premium, premium_direction=direction
    )


class TestPricing:
    def test_credit_bwb_economics(self):
        # U=753, M=750, D=744: narrow 3, wide 6, credit 0.75
        res = _bwb_metrics()
        assert res["max_profit"] == 3.0 + 0.75  # at S = body
        assert res["max_loss"] == (6.0 - 3.0) - 0.75  # below the lower wing
        assert res["break_even_downside"] == 750.0 - 3.0 - 0.75
        assert res["break_even_upside"] is None  # no upside risk

    def test_riskless_bwb_clamps_at_zero_loss(self):
        # A credit larger than (wide − narrow) means no losing price.
        res = _bwb_metrics(entry_premium=3.5)
        assert res["max_loss"] == 0.0


def _bwb_playbook() -> PlaybookDefinitionSchema:
    return _make_playbook(
        pb_id="bwb", strategy="BROKEN_WING_BUTTERFLY", min_ivr=40.0, vix_min=10.0, short_delta=0.30, spread_width=3.0
    )


class TestSpecGeneration:
    def test_legs_are_one_two_one_with_skip_strike_lower_wing(self):
        result = generate_trade_spec(
            _bwb_playbook(), _make_market_state(ivr=55.0), [], _make_portfolio_config(), today=TODAY
        )
        assert result.spec is not None
        legs = result.spec.legs
        assert [leg.action for leg in legs] == ["BUY", "SELL", "BUY"]
        assert all(leg.option_type == "PUT" for leg in legs)
        upper, body, lower = (leg.strike for leg in legs)
        assert legs[1].quantity == 2  # the body carries the combo ratio
        assert upper - body == 3.0
        assert body - lower == 6.0
        assert result.spec.limit_price_per_share == 0.75  # ≈ narrow/4 estimate
        assert result.spec.max_loss_dollars == ((6.0 - 3.0) - 0.75) * 100

    def test_bwb_is_income_gated_and_regime_gated(self):
        assert "BROKEN_WING_BUTTERFLY" in REGIME_ALLOWED_STRATEGIES["CALM_BULL"]
        assert "BROKEN_WING_BUTTERFLY" in REGIME_ALLOWED_STRATEGIES["HIGH_VOL_NEUTRAL"]
        assert "BROKEN_WING_BUTTERFLY" not in REGIME_ALLOWED_STRATEGIES["TRENDING_BEAR"]
        # Income IVR gate applies: IVR 25 suppresses in book_mode.
        result = scan_opportunities(
            [_bwb_playbook()], _make_market_state(ivr=25.0), [], _make_portfolio_config(), today=TODAY, book_mode=True
        )
        assert "IVR GATE" in (result.candidates[0].suppressed_reason or "")


class TestB18Wiring:
    def test_b18_whitelists_and_reenables_the_disabled_seed(self):
        b18 = next(spec for spec in LAB_BOOKS if spec["id"] == "B18")
        playbooks = [PlaybookDefinitionSchema(**pb) for pb in SEED_PLAYBOOKS]
        selected = _book_playbooks(playbooks, resolve_book_config(b18["config"]))
        assert [pb.id for pb in selected] == ["spy_broken_wing_butterfly_v1"]
        assert selected[0].enabled is True
        assert selected[0].underlying_ticker == "XSP"

    def test_other_books_never_see_the_bwb(self):
        playbooks = [PlaybookDefinitionSchema(**pb) for pb in SEED_PLAYBOOKS]
        b01 = next(spec for spec in LAB_BOOKS if spec["id"] == "B01")
        selected = _book_playbooks(playbooks, resolve_book_config(b01["config"]))
        bwb = next(pb for pb in selected if pb.strategy_type == "BROKEN_WING_BUTTERFLY")
        assert bwb.enabled is False  # skipped entirely by the scan
