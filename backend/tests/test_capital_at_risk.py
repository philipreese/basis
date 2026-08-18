"""Pins the unified capital-at-risk definition (#30).

Before this fix, opportunity._capital_deployed used entry_premium for DEBIT
positions while observation.run_exposure_safeguards used max_loss for all —
a manually-entered position with entry_premium != max_loss made the two
modules disagree about deployed capital. Both must now flow through
pricing.capital_at_risk (max_loss × 100 × contracts).
"""

from backend.database import SEED_PORTFOLIO_CONFIG
from backend.eligibility import capital_deployed as _capital_deployed
from backend.models import PortfolioConfigSchema, PositionSchema
from backend.observation import run_exposure_safeguards
from backend.pricing import capital_at_risk


def _position(entry_premium: float, max_loss: float, contracts: int = 1) -> PositionSchema:
    return PositionSchema(
        id="pos_test",
        underlying="SPY",
        strategy_type="BULL_CALL_SPREAD",
        execution_mode="PAPER",
        legs=[],
        entry_date="2026-08-01",
        expiration_date="2026-09-18",
        entry_premium=entry_premium,
        premium_direction="DEBIT",
        current_value_per_share=entry_premium,
        contracts=contracts,
        max_profit=5.0 - max_loss,
        max_loss=max_loss,
        notes="",
        rolls=0,
        status="OPEN",
        journal={
            "core_thesis_rationale": "test",
            "structural_invalidation": "test",
            "expected_underlying_move_pct": 1.0,
            "pre_trade_emotional_state": "Calm",
            "pre_trade_confidence_rating": 3,
        },
    )


def test_capital_at_risk_is_max_loss_based() -> None:
    assert capital_at_risk(2.5, 2) == 500.0


def test_gates_use_max_loss_not_entry_premium() -> None:
    # Deliberate data-entry mismatch: premium says 2.0, max loss says 3.0.
    pos = _position(entry_premium=2.0, max_loss=3.0)
    assert _capital_deployed([pos]) == 300.0  # not 200.0


def test_gates_and_safeguards_agree() -> None:
    """The number the opportunity gates deploy must be the number the Layer A
    safeguards measure — for any premium/max-loss combination."""
    config = PortfolioConfigSchema(**SEED_PORTFOLIO_CONFIG)
    for entry_premium, max_loss, contracts in [(2.0, 3.0, 1), (16.61, 16.61, 1), (1.5, 3.5, 4)]:
        pos = _position(entry_premium, max_loss, contracts)
        gate_value = _capital_deployed([pos])
        assert gate_value == capital_at_risk(max_loss, contracts)
        # Safeguards compute the same per-position capital: drive deployment
        # over the limit and check the reported dollar figure matches.
        big = _position(entry_premium=1.0, max_loss=100.0, contracts=1)  # $10,000 at risk
        warnings = run_exposure_safeguards([big], config)
        deployed_msgs = [w for w in warnings if w["type"] == "CAPITAL_DEPLOYED"]
        assert deployed_msgs, "expected a CAPITAL_DEPLOYED warning at 100% NAV"
        assert f"${capital_at_risk(100.0, 1):.2f}" in deployed_msgs[0]["message"]
