"""Tests for the #136 experiment-matrix mechanics (ADR-0009).

Covers the behaviors the matrix depends on: the regime gate (which existed
only as prose before #136), the book_mode/enforce_* scan flags that the
control books B12/B16 hinge on, per-book playbook whitelists and dot-keyed
overrides, and exit thresholds read from the position's frozen playbook
snapshot instead of hardcoded constants.
"""

import datetime

import pytest
from pydantic import ValidationError

from backend.database import SEED_PLAYBOOKS
from backend.executor import _book_playbooks
from backend.models import ExitRules, OptionLegSchema, PlaybookDefinitionSchema, PositionSchema
from backend.observation import run_lifecycle_scan
from backend.opportunity import REGIME_ALLOWED_STRATEGIES, _check_regime_gate, scan_opportunities
from backend.tests.test_sprint4 import (
    _TEST_JOURNAL,
    _make_market_state,
    _make_playbook,
    _make_portfolio_config,
)

TODAY = datetime.date(2026, 8, 18)


def _seed_schemas() -> list[PlaybookDefinitionSchema]:
    return [PlaybookDefinitionSchema(**pb) for pb in SEED_PLAYBOOKS]


def _position(
    pos_id: str = "p1",
    strategy: str = "BULL_PUT_SPREAD",
    underlying: str = "SPY",
    premium: float = 1.65,
    current: float = 1.65,
    direction: str = "CREDIT",
    expiration: str = "2026-10-30",
    snapshot: PlaybookDefinitionSchema | None = None,
) -> PositionSchema:
    return PositionSchema(
        id=pos_id,
        underlying=underlying,
        strategy_type=strategy,  # type: ignore
        execution_mode="PAPER",
        legs=[
            OptionLegSchema(
                option_type="PUT",
                direction="SHORT",
                strike=735.0,
                expiration=expiration,
                delta=-0.16,
                theta=0.05,
                vega=-0.1,
                gamma=0.02,
            ),
            OptionLegSchema(
                option_type="PUT",
                direction="LONG",
                strike=730.0,
                expiration=expiration,
                delta=-0.05,
                theta=0.02,
                vega=-0.05,
                gamma=0.01,
            ),
        ],
        entry_date="2026-07-20",
        expiration_date=expiration,
        entry_premium=premium,
        premium_direction=direction,  # type: ignore
        current_value_per_share=current,
        contracts=1,
        max_profit=premium,
        max_loss=5.0 - premium,
        notes="",
        rolls=0,
        status="OPEN",
        journal=_TEST_JOURNAL,
        playbook_snapshot=snapshot,
    )


# ---------------------------------------------------------------------------
# Regime gate (domain-rules playbook matrix, enforced since #136)
# ---------------------------------------------------------------------------


class TestRegimeGate:
    def test_calm_bull_blocks_bearish_strategies(self):
        pb = _make_playbook(strategy="BEAR_CALL_SPREAD")
        reason = _check_regime_gate(pb, _make_market_state(regime="CALM_BULL"))
        assert reason is not None and "REGIME GATE" in reason

    def test_calm_bull_allows_its_matrix_row(self):
        for strategy in ("BULL_PUT_SPREAD", "BULL_CALL_SPREAD", "IRON_CONDOR"):
            pb = _make_playbook(strategy=strategy)
            assert _check_regime_gate(pb, _make_market_state(regime="CALM_BULL")) is None

    def test_trending_bear_blocks_income(self):
        pb = _make_playbook(strategy="IRON_CONDOR")
        reason = _check_regime_gate(pb, _make_market_state(regime="TRENDING_BEAR"))
        assert reason is not None

    def test_scan_surfaces_regime_suppression(self):
        pb = _make_playbook(pb_id="bcs", strategy="BEAR_CALL_SPREAD", min_ivr=0.0, vix_min=10.0)
        result = scan_opportunities(
            [pb], _make_market_state(regime="CALM_BULL"), [], _make_portfolio_config(), today=TODAY
        )
        (card,) = result.candidates
        assert not card.eligible
        assert "REGIME GATE" in (card.suppressed_reason or "")

    def test_event_catalyst_means_do_nothing_on_the_seed_mix(self):
        # EVENT_CATALYST allows only the long-vol strategies, which ship
        # disabled — so the seeded playbook set must yield zero eligible.
        assert REGIME_ALLOWED_STRATEGIES["EVENT_CATALYST"] == {"LONG_STRADDLE", "LONG_STRANGLE"}
        result = scan_opportunities(
            _seed_schemas(),
            _make_market_state(regime="EVENT_CATALYST", ivr=55.0),
            [],
            _make_portfolio_config(),
            today=TODAY,
        )
        assert not result.portfolio_blocked
        assert all(not c.eligible for c in result.candidates)

    def test_b12_control_bypasses_the_regime_gate_only(self):
        pb = _make_playbook(pb_id="bcs", strategy="BEAR_CALL_SPREAD", min_ivr=0.0, vix_min=10.0)
        result = scan_opportunities(
            [pb],
            _make_market_state(regime="CALM_BULL"),
            [],
            _make_portfolio_config(),
            today=TODAY,
            enforce_regime=False,
        )
        (card,) = result.candidates
        assert card.eligible


# ---------------------------------------------------------------------------
# book_mode / enforce_ivr scan flags (executor lab books)
# ---------------------------------------------------------------------------


class TestBookModeFlags:
    def _bull_call(self):
        # Debit vertical: allowed in CALM_BULL, not IVR-gated at IVR 25.
        return _make_playbook(pb_id="bcs", strategy="BULL_CALL_SPREAD", min_ivr=0.0, vix_min=10.0)

    def test_manual_scan_still_blocks_underlying_concentration(self):
        result = scan_opportunities(
            [self._bull_call()], _make_market_state(), [_position()], _make_portfolio_config(), today=TODAY
        )
        (card,) = result.candidates
        assert "UNDERLYING CONCENTRATION" in (card.suppressed_reason or "")

    def test_book_mode_skips_concentration_gates(self):
        # A lab book ladders multiple positions on ONE underlying by design —
        # its concentration policy is the risk envelope, not these gates.
        result = scan_opportunities(
            [self._bull_call()],
            _make_market_state(),
            [_position("p1"), _position("p2")],
            _make_portfolio_config(),
            today=TODAY,
            book_mode=True,
        )
        (card,) = result.candidates
        assert card.eligible

    def test_b16_control_bypasses_the_income_ivr_gate(self):
        condor = _make_playbook(pb_id="ic", strategy="IRON_CONDOR", min_ivr=0.0, vix_min=10.0)
        gated = scan_opportunities(
            [condor], _make_market_state(ivr=25.0), [], _make_portfolio_config(), today=TODAY, book_mode=True
        )
        ungated = scan_opportunities(
            [condor],
            _make_market_state(ivr=25.0),
            [],
            _make_portfolio_config(),
            today=TODAY,
            book_mode=True,
            enforce_ivr=False,
        )
        assert "IVR GATE" in (gated.candidates[0].suppressed_reason or "")
        assert ungated.candidates[0].eligible


# ---------------------------------------------------------------------------
# Per-book playbook whitelist and overrides (executor._book_playbooks)
# ---------------------------------------------------------------------------


class TestBookPlaybooks:
    def test_no_config_returns_playbooks_unchanged(self):
        playbooks = _seed_schemas()
        assert _book_playbooks(playbooks, {}) == playbooks

    def test_whitelist_selects_only_listed_ids(self):
        selected = _book_playbooks(_seed_schemas(), {"playbook_ids": ["spy_iron_condor_v1"]})
        assert [pb.id for pb in selected] == ["spy_iron_condor_v1"]

    def test_dot_keyed_override_rewrites_nested_field(self):
        original = _seed_schemas()
        adjusted = _book_playbooks(original, {"playbook_overrides": {"execution_specs.target_dte": 24}})
        assert all(pb.execution_specs.target_dte == 24 for pb in adjusted)
        # The source playbooks are untouched — overrides are per-book views.
        assert all(pb.execution_specs.target_dte != 24 for pb in original)

    def test_bad_override_fails_loudly_at_scan_time(self):
        with pytest.raises(ValidationError):
            _book_playbooks(_seed_schemas(), {"playbook_overrides": {"execution_specs.target_dte": "not-a-dte"}})


# ---------------------------------------------------------------------------
# Exit thresholds from the frozen playbook snapshot (run_lifecycle_scan)
# ---------------------------------------------------------------------------


class TestSnapshotExitRules:
    def _snapshot(self, **exit_kwargs) -> PlaybookDefinitionSchema:
        rules = {
            "profit_take_pct": 50.0,
            "stop_loss_pct": 200.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        }
        rules.update(exit_kwargs)
        return _make_playbook().model_copy(update={"exit_rules": ExitRules(**rules)})

    def _scan(self, position: PositionSchema) -> dict:
        return run_lifecycle_scan(position, "CALM_BULL", 758.0, [], today=TODAY)

    def test_b15_arm_takes_profit_at_25_pct(self):
        # 30% of premium captured: below the 50% default, above B15's 25%.
        pos_default = _position(premium=1.65, current=1.15)
        pos_b15 = _position(premium=1.65, current=1.15, snapshot=self._snapshot(profit_take_pct=25.0))
        assert self._scan(pos_default)["priority"] != "P1 — CLOSE NOW"
        assert self._scan(pos_b15)["priority"] == "P1 — CLOSE NOW"

    def test_b17_arm_holds_past_21_dte(self):
        # ~10 DTE: the 21-DTE default says close soon; B17's 7-DTE rule holds.
        pos_default = _position(expiration="2026-08-28")
        pos_b17 = _position(expiration="2026-08-28", snapshot=self._snapshot(mandatory_exit_dte=7))
        assert self._scan(pos_default)["priority"] == "P2 — CLOSE SOON"
        assert "Time limit" not in self._scan(pos_b17)["reason"]

    def test_debit_loss_limit_closes_at_half_premium_lost(self):
        # The spec's debit stop (50% of premium paid) was missing entirely
        # before #136 — this pins the new P1 branch.
        pos = _position(premium=2.0, current=0.9, direction="DEBIT")
        result = self._scan(pos)
        assert result["priority"] == "P1 — CLOSE NOW"
        assert "Loss limit" in result["reason"]

    def test_snapshot_stop_loss_governs_credit_exit(self):
        # Down 1.2× premium: inside the 2× default, beyond a 100% snapshot stop.
        pos = _position(premium=1.0, current=2.2, snapshot=self._snapshot(stop_loss_pct=100.0))
        result = self._scan(pos)
        assert result["priority"] == "P1 — CLOSE NOW"
        assert "Loss limit" in result["reason"]
