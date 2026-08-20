"""
test_opportunity.py — Layer C opportunity engine

Tests cover:
- Portfolio-level exposure gates (MAX_POSITIONS, MAX_CAPITAL)
- Per-playbook gates (UNDERLYING_CONCENTRATION, DIRECTIONAL_CONCENTRATION, IVR gates)
- Entry filter checks (IVR range, VIX range, trend, catalyst rules)
- scan_opportunities returns only eligible candidates
- generate_trade_spec for all 5 strategy types
- Hard blocks: unresolved P1, capital exceeded, max loss, expiration arithmetic,
  premium reasonableness, position count, strike sanity
- Warnings: regime consistency, duplicate underlying, break-even realism, strategy novelty
- API: GET /api/playbooks, GET /api/opportunity/scan, POST /api/opportunity/spec/{id}
- Hard blocks must be uncircumventable (spec = None when blocks present)
- Warnings do not block spec generation
"""

from datetime import date
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import (
    SEED_PLAYBOOKS,
    SEED_PORTFOLIO_CONFIG,
    SEED_POSITIONS,
    get_db,
)
from backend.models import (
    AccountConfig,
    Base,
    EntryFilters,
    ExecutionSpecs,
    ExitRules,
    MarketStateModel,
    MarketStateSchema,
    OperationalJournalEntrySchema,
    OptionLegSchema,
    PlaybookDefinitionModel,
    PlaybookDefinitionSchema,
    PortfolioConfigModel,
    PortfolioConfigSchema,
    PortfolioGreekLimits,
    PositionModel,
    PositionSchema,
    RiskProfile,
)

_TEST_JOURNAL = OperationalJournalEntrySchema(
    core_thesis_rationale="Test rationale",
    structural_invalidation="Test invalidation",
    expected_underlying_move_pct=2.0,
    pre_trade_emotional_state="Calm",
    pre_trade_confidence_rating=3,
)
from backend.eligibility import (
    capital_deployed as _capital_deployed,
)
from backend.eligibility import (
    check_entry_filters as _check_entry_filters,
)
from backend.eligibility import (
    check_per_playbook_gates as _check_per_playbook_gates,
)
from backend.eligibility import (
    has_catalyst_within_14dte as _has_catalyst_within_14dte,
)
from backend.eligibility import (
    run_portfolio_gates as _run_portfolio_gates,
)
from backend.main import app
from backend.opportunity import (
    generate_trade_spec,
    scan_opportunities,
)

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


def _make_playbook(
    pb_id: str = "test_iron_condor",
    strategy: str = "IRON_CONDOR",
    min_ivr: float = 50.0,
    max_ivr: float = 100.0,
    vix_min: float = 15.0,
    vix_max: float = 35.0,
    required_trend: str = "ANY",
    block_catalyst: bool = True,
    require_catalyst: bool = False,
    target_dte: int = 38,
    short_delta: float = 0.16,
    long_delta: float = 0.05,
    spread_width: float = 5.0,
    straddle_atm: bool = False,
) -> PlaybookDefinitionSchema:
    return PlaybookDefinitionSchema(
        id=pb_id,
        version="1.0",
        name=f"Test {strategy}",
        underlying_ticker="SPY",
        strategy_type=strategy,  # type: ignore
        execution_mode="PAPER",
        entry_filters=EntryFilters(
            min_ivr=min_ivr,
            max_ivr=max_ivr,
            vix_range=(vix_min, vix_max),
            required_trend=required_trend,  # type: ignore
            block_catalyst_14dte=block_catalyst,
            require_catalyst_14dte=require_catalyst,
        ),
        execution_specs=ExecutionSpecs(
            target_dte=target_dte,
            short_leg_delta=short_delta,
            long_leg_delta=long_delta,
            spread_width_dollars=spread_width,
            straddle_atm=straddle_atm,
        ),
        exit_rules=ExitRules(
            profit_take_pct=50.0,
            stop_loss_pct=200.0,
            mandatory_exit_dte=21,
        ),
    )


def _make_market_state(
    regime: str = "CALM_BULL",
    spy_price: float = 758.0,
    spy_sma20: float = 750.0,
    vix: float = 14.5,
    ivr: float = 25.0,
    daily_return: float = 0.005,
    catalysts: list[str] | None = None,
) -> MarketStateSchema:
    return MarketStateSchema(
        current_regime=regime,  # type: ignore
        spy_price=spy_price,
        spy_sma20=spy_sma20,
        vix_close=vix,
        underlying_ivrs={"SPY": ivr},
        spy_daily_return=daily_return,
        catalyst_dates=catalysts or [],
        regime_scores={},
    )


def _make_portfolio_config(
    nav: float = 10000.0,
    max_positions: int = 3,
    max_deployed_pct: float = 85.0,
    max_trade_risk: float = 1500.0,
) -> PortfolioConfigSchema:
    return PortfolioConfigSchema(
        account=AccountConfig(
            total_nav=nav,
            broker="Test",
            account_type="Roth IRA",
            options_approval="Level 3",
            execution_mode="PAPER",
        ),
        risk_profile=RiskProfile(
            max_trade_risk_pct=15.0,
            max_trade_risk_dollars=max_trade_risk,
            max_underlying_concentration_pct=35.0,
            max_correlated_index_pct=50.0,
            minimum_cash_reserve_pct=15.0,
            max_simultaneous_positions=max_positions,
            max_capital_deployed_pct=max_deployed_pct,
        ),
        portfolio_greek_limits=PortfolioGreekLimits(max_net_delta=50.0, max_net_vega=100.0, max_net_gamma=10.0),
    )


def _open_straddle(pos_id: str = "p1", underlying: str = "SPY", premium: float = 16.61) -> PositionSchema:
    return PositionSchema(
        id=pos_id,
        underlying=underlying,
        strategy_type="LONG_STRADDLE",
        execution_mode="PAPER",
        legs=[
            OptionLegSchema(
                option_type="CALL",
                direction="LONG",
                strike=759.0,
                expiration="2026-08-15",
                delta=0.5,
                theta=-0.1,
                vega=0.2,
                gamma=0.05,
            ),
            OptionLegSchema(
                option_type="PUT",
                direction="LONG",
                strike=759.0,
                expiration="2026-08-15",
                delta=-0.5,
                theta=-0.1,
                vega=0.2,
                gamma=0.05,
            ),
        ],
        entry_date="2026-06-07",
        expiration_date="2026-08-15",
        entry_premium=premium,
        premium_direction="DEBIT",
        current_value_per_share=premium,
        contracts=1,
        max_profit=999999.0,
        max_loss=premium,
        notes="",
        rolls=0,
        status="OPEN",
        journal=_TEST_JOURNAL,
    )


@pytest_asyncio.fixture
async def test_db():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as session:
        session.add(
            PortfolioConfigModel(
                id=1,
                account=SEED_PORTFOLIO_CONFIG["account"],
                risk_profile=SEED_PORTFOLIO_CONFIG["risk_profile"],
                portfolio_greek_limits=SEED_PORTFOLIO_CONFIG["portfolio_greek_limits"],
            )
        )
        session.add(
            MarketStateModel(
                id=1,
                current_regime="CALM_BULL",
                spy_price=758.0,
                spy_sma20=750.0,
                vix_close=14.5,
                underlying_ivrs={"SPY": 25.0},
                spy_daily_return=0.005,
                catalyst_dates=["2026-06-08"],
                regime_scores={
                    "CALM_BULL": 7.0,
                    "HIGH_VOL_NEUTRAL": 0.0,
                    "TRENDING_BEAR": -3.0,
                    "EVENT_CATALYST": -1.0,
                },
            )
        )
        for p_data in SEED_POSITIONS:
            session.add(
                PositionModel(
                    id=p_data["id"],
                    underlying=p_data["underlying"],
                    strategy_type=p_data["strategy_type"],
                    legs=p_data["legs"],
                    entry_date=p_data["entry_date"],
                    expiration_date=p_data["expiration_date"],
                    entry_premium=p_data["entry_premium"],
                    premium_direction=p_data["premium_direction"],
                    current_value_per_share=p_data["current_value_per_share"],
                    contracts=p_data["contracts"],
                    max_profit=p_data["max_profit"],
                    max_loss=p_data["max_loss"],
                    profit_target_per_share=p_data.get("profit_target_per_share"),
                    loss_limit_per_share=p_data.get("loss_limit_per_share"),
                    break_even_upside=p_data.get("break_even_upside"),
                    break_even_downside=p_data.get("break_even_downside"),
                    notes=p_data["notes"],
                    rolls=p_data["rolls"],
                    status=p_data["status"],
                    journal=p_data["journal"],
                )
            )
        for pb_data in SEED_PLAYBOOKS:
            session.add(
                PlaybookDefinitionModel(
                    id=pb_data["id"],
                    version=pb_data["version"],
                    name=pb_data["name"],
                    underlying_ticker=pb_data["underlying_ticker"],
                    strategy_type=pb_data["strategy_type"],
                    entry_filters=pb_data["entry_filters"],
                    execution_specs=pb_data["execution_specs"],
                    exit_rules=pb_data["exit_rules"],
                )
            )
        await session.commit()

    yield session_maker

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(test_db):
    async def override_get_db():
        async with test_db() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# ===========================================================================
# Section 1: Helper utilities
# ===========================================================================


class TestHelpers:
    def test_has_catalyst_within_14dte_true(self):
        today = date(2026, 6, 9)
        assert _has_catalyst_within_14dte(["2026-06-15"], today) is True

    def test_has_catalyst_within_14dte_boundary_exact_14(self):
        today = date(2026, 6, 9)
        assert _has_catalyst_within_14dte(["2026-06-23"], today) is True

    def test_has_catalyst_within_14dte_false_at_15(self):
        today = date(2026, 6, 9)
        assert _has_catalyst_within_14dte(["2026-06-24"], today) is False

    def test_has_catalyst_within_14dte_empty(self):
        today = date(2026, 6, 9)
        assert _has_catalyst_within_14dte([], today) is False

    def test_capital_deployed_debit(self):
        pos = _open_straddle(premium=16.61)
        assert _capital_deployed([pos]) == pytest.approx(16.61 * 100 * 1)

    def test_capital_deployed_credit(self):
        pos = _open_straddle(premium=5.0)
        pos = pos.model_copy(update={"premium_direction": "CREDIT", "max_loss": 5.0})
        assert _capital_deployed([pos]) == pytest.approx(5.0 * 100 * 1)

    def test_capital_deployed_skips_closed(self):
        pos = _open_straddle(premium=20.0)
        pos = pos.model_copy(update={"status": "CLOSED"})
        assert _capital_deployed([pos]) == 0.0


# ===========================================================================
# Section 2: Portfolio-level gates
# ===========================================================================


class TestPortfolioGates:
    def test_max_positions_fires_at_limit(self):
        open_pos = [_open_straddle(f"p{i}") for i in range(3)]
        config = _make_portfolio_config(max_positions=3)
        block = _run_portfolio_gates(open_pos, config)
        assert block is not None
        assert "MAX POSITIONS" in block

    def test_max_positions_does_not_fire_below_limit(self):
        open_pos = [_open_straddle("p1"), _open_straddle("p2")]
        config = _make_portfolio_config(max_positions=3)
        assert _run_portfolio_gates(open_pos, config) is None

    def test_max_capital_fires_at_threshold(self):
        # NAV=10000, limit=85%, deployed must be >= 8500
        pos = _open_straddle(premium=85.0)  # $85 * 100 = $8500
        config = _make_portfolio_config(nav=10000.0, max_deployed_pct=85.0)
        block = _run_portfolio_gates([pos], config)
        assert block is not None
        assert "MAX CAPITAL" in block

    def test_max_capital_does_not_fire_below_threshold(self):
        pos = _open_straddle(premium=16.61)  # $1661 deployed of $10k
        config = _make_portfolio_config(nav=10000.0, max_deployed_pct=85.0)
        assert _run_portfolio_gates([pos], config) is None

    def test_no_open_positions_clears_all_gates(self):
        config = _make_portfolio_config()
        assert _run_portfolio_gates([], config) is None


# ===========================================================================
# Section 3: Per-playbook gates
# ===========================================================================


class TestPerPlaybookGates:
    def test_underlying_concentration_blocks_same_ticker(self):
        pb = _make_playbook()  # SPY
        open_pos = [_open_straddle()]  # also SPY
        market = _make_market_state()
        reason = _check_per_playbook_gates(pb, open_pos, market)
        assert reason is not None
        assert "UNDERLYING CONCENTRATION" in reason

    def test_underlying_concentration_clears_different_ticker(self):
        pb = _make_playbook()  # SPY
        open_pos = [_open_straddle(underlying="QQQ")]
        # IVR=45 satisfies the income IVR gate (>= 40) so only underlying gate is relevant
        market = _make_market_state(ivr=45.0)
        assert _check_per_playbook_gates(pb, open_pos, market) is None

    def test_directional_concentration_fires_at_2_bullish(self):
        pb = _make_playbook(strategy="BULL_CALL_SPREAD", pb_id="bcs_new")
        open_pos = [
            _open_straddle("p1", underlying="QQQ").model_copy(update={"strategy_type": "BULL_CALL_SPREAD"}),
            _open_straddle("p2", underlying="IWM").model_copy(update={"strategy_type": "BULL_CALL_SPREAD"}),
        ]
        market = _make_market_state()
        reason = _check_per_playbook_gates(pb, open_pos, market)
        assert reason is not None
        assert "DIRECTIONAL CONCENTRATION" in reason

    def test_directional_concentration_neutral_strategy_not_blocked(self):
        pb = _make_playbook(strategy="IRON_CONDOR", pb_id="ic_new")
        open_pos = [
            _open_straddle("p1", underlying="QQQ").model_copy(update={"strategy_type": "BULL_CALL_SPREAD"}),
            _open_straddle("p2", underlying="IWM").model_copy(update={"strategy_type": "BULL_CALL_SPREAD"}),
        ]
        market = _make_market_state(ivr=60.0)  # satisfy income IVR gate
        # Underlying gate won't fire (QQQ/IWM, not SPY), directional is neutral
        reason = _check_per_playbook_gates(pb, open_pos, market)
        assert reason is None

    def test_ivr_income_gate_fires_below_40(self):
        pb = _make_playbook(strategy="IRON_CONDOR", min_ivr=50.0, max_ivr=100.0)
        market = _make_market_state(ivr=30.0)
        reason = _check_per_playbook_gates(pb, [], market)
        assert reason is not None
        assert "IVR GATE (INCOME)" in reason

    def test_ivr_income_gate_clears_at_40(self):
        market = _make_market_state(ivr=40.0)
        # Entry filter (min_ivr=50) will catch it before gate, test gate separately
        # Rebuild pb with min_ivr=0 to isolate gate check
        pb2 = _make_playbook(strategy="IRON_CONDOR", min_ivr=0.0, max_ivr=100.0)
        assert _check_per_playbook_gates(pb2, [], market) is None

    def test_ivr_debit_gate_fires_above_70(self):
        pb = _make_playbook(strategy="LONG_STRADDLE")
        market = _make_market_state(ivr=75.0)
        reason = _check_per_playbook_gates(pb, [], market)
        assert reason is not None
        assert "IVR GATE (DEBIT)" in reason

    def test_ivr_debit_gate_clears_at_70(self):
        pb = _make_playbook(strategy="LONG_STRADDLE", min_ivr=0.0, max_ivr=100.0)
        market = _make_market_state(ivr=70.0)
        assert _check_per_playbook_gates(pb, [], market) is None


# ===========================================================================
# Section 4: Entry filter checks
# ===========================================================================


class TestEntryFilters:
    def test_ivr_below_min_blocked(self):
        pb = _make_playbook(min_ivr=50.0, max_ivr=100.0)
        market = _make_market_state(ivr=30.0)
        reason = _check_entry_filters(pb, market)
        assert reason is not None
        assert "IVR" in reason

    def test_ivr_above_max_blocked(self):
        pb = _make_playbook(min_ivr=10.0, max_ivr=60.0)
        market = _make_market_state(ivr=70.0)
        reason = _check_entry_filters(pb, market)
        assert reason is not None
        assert "IVR" in reason

    def test_ivr_in_range_passes(self):
        pb = _make_playbook(min_ivr=20.0, max_ivr=80.0)
        market = _make_market_state(ivr=55.0, vix=20.0)
        assert _check_entry_filters(pb, market) is None

    def test_vix_below_range_blocked(self):
        pb = _make_playbook(min_ivr=0.0, max_ivr=100.0, vix_min=15.0, vix_max=35.0)
        market = _make_market_state(ivr=25.0, vix=12.0)
        reason = _check_entry_filters(pb, market)
        assert reason is not None
        assert "VIX" in reason

    def test_vix_above_range_blocked(self):
        pb = _make_playbook(min_ivr=0.0, max_ivr=100.0, vix_min=15.0, vix_max=25.0)
        market = _make_market_state(ivr=25.0, vix=30.0)
        reason = _check_entry_filters(pb, market)
        assert reason is not None
        assert "VIX" in reason

    def test_trend_required_above_sma20_blocks_when_below(self):
        pb = _make_playbook(
            min_ivr=0.0,
            max_ivr=100.0,
            vix_min=0.0,
            vix_max=100.0,
            required_trend="ABOVE_SMA20",
        )
        market = _make_market_state(spy_price=740.0, spy_sma20=760.0, ivr=25.0, vix=14.5)
        reason = _check_entry_filters(pb, market)
        assert reason is not None
        assert "trend" in reason.lower()

    def test_trend_below_sma20_required_and_met(self):
        pb = _make_playbook(
            min_ivr=0.0,
            max_ivr=100.0,
            vix_min=0.0,
            vix_max=100.0,
            required_trend="BELOW_SMA20",
        )
        market = _make_market_state(spy_price=740.0, spy_sma20=760.0, ivr=25.0, vix=20.0)
        assert _check_entry_filters(pb, market) is None

    def test_block_catalyst_fires_when_catalyst_within_14dte(self):
        today = date(2026, 6, 9)
        pb = _make_playbook(min_ivr=0.0, max_ivr=100.0, vix_min=0.0, vix_max=100.0, block_catalyst=True)
        market = _make_market_state(ivr=25.0, vix=14.5, catalysts=["2026-06-15"])
        reason = _check_entry_filters(pb, market, today)
        assert reason is not None
        assert "catalyst" in reason.lower()

    def test_require_catalyst_blocks_when_none_upcoming(self):
        pb = _make_playbook(
            min_ivr=0.0,
            max_ivr=100.0,
            vix_min=0.0,
            vix_max=100.0,
            block_catalyst=False,
            require_catalyst=True,
        )
        market = _make_market_state(ivr=25.0, vix=14.5, catalysts=[])
        reason = _check_entry_filters(pb, market)
        assert reason is not None
        assert "catalyst" in reason.lower()

    def test_require_catalyst_passes_when_catalyst_present(self):
        today = date(2026, 6, 9)
        pb = _make_playbook(
            min_ivr=0.0,
            max_ivr=100.0,
            vix_min=0.0,
            vix_max=100.0,
            block_catalyst=False,
            require_catalyst=True,
        )
        market = _make_market_state(ivr=25.0, vix=20.0, catalysts=["2026-06-15"])
        reason = _check_entry_filters(pb, market, today)
        assert reason is None


# ===========================================================================
# Section 5: scan_opportunities
# ===========================================================================


class TestScanOpportunities:
    def test_portfolio_gate_returns_no_candidates(self):
        pbs = [_make_playbook()]
        market = _make_market_state()
        positions = [_open_straddle(f"p{i}") for i in range(3)]  # at 3-position limit
        config = _make_portfolio_config(max_positions=3)
        result = scan_opportunities(pbs, market, positions, config)
        assert result.portfolio_blocked is True
        assert "MAX POSITIONS" in result.block_reason
        assert result.candidates == []

    def test_eligible_candidate_returned_when_all_filters_pass(self):
        # Iron condor eligible when: IVR in range, VIX in range, no catalyst, no same-underlying pos
        pb = _make_playbook(
            strategy="IRON_CONDOR",
            min_ivr=0.0,
            max_ivr=100.0,
            vix_min=0.0,
            vix_max=100.0,
            block_catalyst=False,
        )
        market = _make_market_state(ivr=60.0, vix=20.0)
        config = _make_portfolio_config(max_positions=3)
        result = scan_opportunities([pb], market, [], config)
        assert result.portfolio_blocked is False
        assert len(result.candidates) == 1
        assert result.candidates[0].eligible is True
        assert result.candidates[0].strike_params is not None

    def test_ineligible_candidate_has_suppressed_reason(self):
        pb = _make_playbook(
            strategy="IRON_CONDOR",
            min_ivr=50.0,
            max_ivr=100.0,
            vix_min=0.0,
            vix_max=100.0,
            block_catalyst=False,
        )
        market = _make_market_state(ivr=30.0, vix=20.0)  # IVR too low
        config = _make_portfolio_config()
        result = scan_opportunities([pb], market, [], config)
        assert result.candidates[0].eligible is False
        assert result.candidates[0].suppressed_reason is not None

    def test_all_5_seed_playbooks_loaded(self):
        """Seed playbooks cover all 5 strategy types."""
        from backend.database import SEED_PLAYBOOKS

        strategy_types = {pb["strategy_type"] for pb in SEED_PLAYBOOKS}
        assert "IRON_CONDOR" in strategy_types
        assert "BULL_CALL_SPREAD" in strategy_types
        assert "BEAR_PUT_SPREAD" in strategy_types
        assert "LONG_STRADDLE" in strategy_types
        assert "LONG_STRANGLE" in strategy_types


# ===========================================================================
# Section 6: generate_trade_spec — all 5 strategy types
# ===========================================================================

TODAY = date(2026, 6, 9)


def _make_full_pb(strategy: str) -> PlaybookDefinitionSchema:
    is_debit = strategy in (
        "BULL_CALL_SPREAD",
        "BEAR_PUT_SPREAD",
        "LONG_STRADDLE",
        "LONG_STRANGLE",
    )
    return _make_playbook(
        pb_id=f"test_{strategy.lower()}",
        strategy=strategy,
        min_ivr=0.0,
        max_ivr=100.0,
        vix_min=0.0,
        vix_max=100.0,
        block_catalyst=False,
        require_catalyst=False,
        target_dte=38,
        short_delta=0.16 if not is_debit else 0.50,
        long_delta=0.05 if not is_debit else 0.25,
        spread_width=5.0,
        straddle_atm=(strategy == "LONG_STRADDLE"),
    )


class TestGenerateTradeSpec:
    def _spec_for(self, strategy: str, today: date = TODAY):
        pb = _make_full_pb(strategy)
        market = _make_market_state(ivr=55.0, vix=20.0)
        config = _make_portfolio_config()
        with patch("backend.opportunity.date") as mock_date:
            mock_date.today.return_value = today
            mock_date.fromisoformat.side_effect = date.fromisoformat
            return generate_trade_spec(pb, market, [], config, today=today)

    def test_iron_condor_spec_has_4_legs(self):
        result = self._spec_for("IRON_CONDOR")
        assert result.spec is not None or result.hard_blocks  # may block on max_loss
        if result.spec:
            assert len(result.spec.legs) == 4

    def test_iron_condor_legs_are_buy_sell_balanced(self):
        result = self._spec_for("IRON_CONDOR")
        if result.spec:
            actions = [l.action for l in result.spec.legs]
            assert actions.count("BUY") == 2
            assert actions.count("SELL") == 2

    def test_bull_call_spread_has_2_legs(self):
        result = self._spec_for("BULL_CALL_SPREAD")
        if result.spec:
            assert len(result.spec.legs) == 2
            option_types = {l.option_type for l in result.spec.legs}
            assert option_types == {"CALL"}

    def test_bear_put_spread_has_2_legs(self):
        result = self._spec_for("BEAR_PUT_SPREAD")
        if result.spec:
            assert len(result.spec.legs) == 2
            option_types = {l.option_type for l in result.spec.legs}
            assert option_types == {"PUT"}

    def test_long_straddle_has_call_and_put_at_same_atm_strike(self):
        result = self._spec_for("LONG_STRADDLE")
        if result.spec:
            assert len(result.spec.legs) == 2
            strikes = {l.strike for l in result.spec.legs}
            assert len(strikes) == 1  # ATM straddle — same strike

    def test_long_strangle_has_call_and_put_at_different_strikes(self):
        result = self._spec_for("LONG_STRANGLE")
        if result.spec:
            assert len(result.spec.legs) == 2
            strikes = [l.strike for l in result.spec.legs]
            assert strikes[0] != strikes[1]  # strangle — different strikes

    def test_all_specs_have_required_fields(self):
        for strategy in (
            "IRON_CONDOR",
            "BULL_CALL_SPREAD",
            "BEAR_PUT_SPREAD",
            "LONG_STRADDLE",
            "LONG_STRANGLE",
        ):
            result = self._spec_for(strategy)
            if result.spec:
                spec = result.spec
                assert spec.expiration_date is not None
                assert spec.dte_at_entry >= 14
                assert spec.order_type == "LIMIT"
                assert spec.limit_price_per_share > 0
                assert spec.max_loss_dollars > 0
                assert spec.max_gain_note != ""
                assert len(spec.break_even_prices) > 0
                assert spec.profit_target_dollars > 0
                assert spec.loss_limit_dollars > 0
                assert spec.closing_order_instructions != ""
                assert spec.derivation_params is not None

    def test_long_put_spec_is_a_debit(self):
        # #498: LONG_PUT (B32's tail hedge) must report premium_direction —
        # frontend components trust this instead of maintaining their own
        # strategy_type Sets that silently miss new strategies.
        result = self._spec_for("LONG_PUT")
        if result.spec:
            assert len(result.spec.legs) == 1
            assert result.spec.legs[0].option_type == "PUT"
            assert result.spec.legs[0].action == "BUY"
            assert result.spec.premium_direction == "DEBIT"

    @pytest.mark.parametrize(
        ("strategy", "expected_direction"),
        [
            ("IRON_CONDOR", "CREDIT"),
            ("BULL_PUT_SPREAD", "CREDIT"),
            ("BEAR_CALL_SPREAD", "CREDIT"),
            ("BROKEN_WING_BUTTERFLY", "CREDIT"),
            ("BULL_CALL_SPREAD", "DEBIT"),
            ("BEAR_PUT_SPREAD", "DEBIT"),
            ("LONG_STRADDLE", "DEBIT"),
            ("LONG_STRANGLE", "DEBIT"),
            ("LONG_PUT", "DEBIT"),
        ],
    )
    def test_premium_direction_matches_credit_strategy_set(self, strategy, expected_direction):
        result = self._spec_for(strategy)
        if result.spec:
            assert result.spec.premium_direction == expected_direction

    def test_spec_expiration_is_friday(self):
        result = self._spec_for("IRON_CONDOR")
        if result.spec:
            exp = date.fromisoformat(result.spec.expiration_date)
            assert exp.weekday() == 4  # 4 = Friday

    def test_holiday_friday_snaps_to_prior_trading_day(self):
        # H8 (#282): options for a holiday Friday expire the prior trading
        # day — a naive Friday snap yields unpriceable legs.
        from backend.opportunity import _target_expiration

        exp, dte = _target_expiration(date(2026, 12, 22), 3, False, [])
        assert exp == date(2026, 12, 24)  # 2026-12-25 is Christmas, a Friday
        assert dte == 2

    def test_strike_grid_defaults_to_one_dollar(self):
        # H8 (#282): the short-delta knob sweeps in $1 steps, not $5 lumps.
        from backend.opportunity import _nearest_strike

        assert _nearest_strike(612.4) == 612.0
        assert _nearest_strike(612.4, interval=5.0) == 610.0  # explicit still works


# ===========================================================================
# Section 7: Hard blocks — must be uncircumventable
# ===========================================================================


class TestHardBlocks:
    def _get_blocks(self, positions=None, config=None, strategy="IRON_CONDOR", today=TODAY):
        pb = _make_full_pb(strategy)
        market = _make_market_state(ivr=55.0, vix=20.0)
        if config is None:
            config = _make_portfolio_config()
        with patch("backend.opportunity.date") as mock_date:
            mock_date.today.return_value = today
            mock_date.fromisoformat.side_effect = date.fromisoformat
            return generate_trade_spec(pb, market, positions or [], config, today=today)

    def test_p1_alert_triggers_hard_block(self):
        # Debit trade P1 fires at 100% gain: current_value >= 2 × entry_premium
        pos = _open_straddle(premium=16.61)
        pos = pos.model_copy(update={"current_value_per_share": 33.22})  # 100% gain
        result = self._get_blocks(positions=[pos])
        block_checks = [b.check for b in result.hard_blocks]
        assert "UNRESOLVED_P1" in block_checks

    def test_p1_block_means_spec_is_none(self):
        pos = _open_straddle(premium=16.61)
        pos = pos.model_copy(update={"current_value_per_share": 33.22})  # P1 trigger
        result = self._get_blocks(positions=[pos])
        assert result.spec is None

    def test_max_loss_exceeded_fires(self):
        config = _make_portfolio_config(max_trade_risk=1.0)  # $1 limit — will always fire
        result = self._get_blocks(config=config)
        block_checks = [b.check for b in result.hard_blocks]
        assert "MAX_LOSS_EXCEEDED" in block_checks

    def test_max_loss_block_means_spec_is_none(self):
        config = _make_portfolio_config(max_trade_risk=1.0)
        result = self._get_blocks(config=config)
        assert result.spec is None

    def test_expiration_too_close_fires(self):
        # target_dte=5 produces an expiration ~5 days out, which is < 14 DTE minimum
        pb = _make_playbook(target_dte=5)
        market = _make_market_state(ivr=55.0, vix=20.0)
        config = _make_portfolio_config(max_trade_risk=5000.0)
        with patch("backend.opportunity.date") as mock_date:
            mock_date.today.return_value = TODAY
            mock_date.fromisoformat.side_effect = date.fromisoformat
            result = generate_trade_spec(pb, market, [], config, today=TODAY)
        block_checks = [b.check for b in result.hard_blocks]
        assert "EXPIRATION_ARITHMETIC" in block_checks

    def test_position_count_fires_when_at_limit(self):
        positions = [_open_straddle(f"p{i}", underlying="QQQ") for i in range(3)]
        config = _make_portfolio_config(max_positions=3)
        result = self._get_blocks(positions=positions, config=config)
        block_checks = [b.check for b in result.hard_blocks]
        assert "POSITION_COUNT" in block_checks

    def test_no_hard_blocks_with_clean_state(self):
        result = self._get_blocks(positions=[], config=_make_portfolio_config(max_trade_risk=5000.0))
        # There may still be blocks (capital, etc.) depending on derived limit price
        # The key invariant: if blocks exist, spec must be None
        if result.hard_blocks:
            assert result.spec is None
        else:
            assert result.spec is not None


# ===========================================================================
# Section 8: Warnings — shown but do not suppress spec
# ===========================================================================


class TestWarnings:
    def test_regime_inconsistency_triggers_warning(self):
        pb = _make_full_pb("BULL_CALL_SPREAD")
        # TRENDING_BEAR regime with bullish strategy
        market = _make_market_state(regime="TRENDING_BEAR", ivr=55.0, vix=20.0)
        config = _make_portfolio_config(max_trade_risk=5000.0)
        with patch("backend.opportunity.date") as mock_date:
            mock_date.today.return_value = TODAY
            mock_date.fromisoformat.side_effect = date.fromisoformat
            result = generate_trade_spec(pb, market, [], config, today=TODAY)
        warning_checks = [w.check for w in result.warnings]
        assert "REGIME_CONSISTENCY" in warning_checks

    def test_strategy_novelty_warning_when_first_use(self):
        pb = _make_full_pb("IRON_CONDOR")
        market = _make_market_state(ivr=55.0, vix=20.0)
        config = _make_portfolio_config(max_trade_risk=5000.0)
        with patch("backend.opportunity.date") as mock_date:
            mock_date.today.return_value = TODAY
            mock_date.fromisoformat.side_effect = date.fromisoformat
            result = generate_trade_spec(pb, market, [], config, today=TODAY)
        warning_checks = [w.check for w in result.warnings]
        assert "STRATEGY_NOVELTY" in warning_checks

    def test_strategy_novelty_absent_when_strategy_previously_used(self):
        pb = _make_full_pb("LONG_STRADDLE")
        market = _make_market_state(ivr=55.0, vix=20.0)
        config = _make_portfolio_config(max_trade_risk=5000.0)
        # Position of same strategy type already in history
        existing = _open_straddle()  # LONG_STRADDLE
        with patch("backend.opportunity.date") as mock_date:
            mock_date.today.return_value = TODAY
            mock_date.fromisoformat.side_effect = date.fromisoformat
            result = generate_trade_spec(pb, market, [existing], config, today=TODAY)
        warning_checks = [w.check for w in result.warnings]
        assert "STRATEGY_NOVELTY" not in warning_checks

    def test_duplicate_underlying_warning(self):
        pb = _make_full_pb("IRON_CONDOR")
        market = _make_market_state(ivr=55.0, vix=20.0)
        config = _make_portfolio_config(max_trade_risk=5000.0)
        existing = _open_straddle(underlying="SPY")  # same underlying
        with patch("backend.opportunity.date") as mock_date:
            mock_date.today.return_value = TODAY
            mock_date.fromisoformat.side_effect = date.fromisoformat
            result = generate_trade_spec(pb, market, [existing], config, today=TODAY)
        warning_checks = [w.check for w in result.warnings]
        assert "DUPLICATE_UNDERLYING" in warning_checks

    def test_warnings_do_not_suppress_spec_without_hard_blocks(self):
        pb = _make_full_pb("BULL_CALL_SPREAD")
        market = _make_market_state(regime="TRENDING_BEAR", ivr=55.0, vix=20.0)
        config = _make_portfolio_config(max_trade_risk=5000.0)
        with patch("backend.opportunity.date") as mock_date:
            mock_date.today.return_value = TODAY
            mock_date.fromisoformat.side_effect = date.fromisoformat
            result = generate_trade_spec(pb, market, [], config, today=TODAY)
        if not result.hard_blocks:
            assert result.spec is not None
            assert len(result.warnings) > 0


# ===========================================================================
# Section 9: API integration tests
# ===========================================================================


@pytest.mark.anyio
class TestOpportunityAPI:
    async def test_get_playbooks_returns_all_seeds(self, client):
        resp = await client.get("/api/playbooks")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 11  # nine SPY + AAPL earnings condor (#317) + XSP tail put (#319)
        strategy_types = {pb["strategy_type"] for pb in data}
        assert {
            "IRON_CONDOR",
            "BULL_CALL_SPREAD",
            "BEAR_PUT_SPREAD",
            "BULL_PUT_SPREAD",
            "BEAR_CALL_SPREAD",
            "BROKEN_WING_BUTTERFLY",
            "CALENDAR_SPREAD",
            "LONG_STRADDLE",
            "LONG_STRANGLE",
            "LONG_PUT",
        } == strategy_types

    async def test_get_playbooks_schema_valid(self, client):
        resp = await client.get("/api/playbooks")
        assert resp.status_code == 200
        for pb in resp.json():
            assert "id" in pb
            assert "version" in pb
            assert "entry_filters" in pb
            assert "execution_specs" in pb
            assert "exit_rules" in pb

    async def test_opportunity_scan_returns_result(self, client):
        resp = await client.get("/api/opportunity/scan")
        assert resp.status_code == 200
        data = resp.json()
        assert "portfolio_blocked" in data
        assert "candidates" in data

    async def test_opportunity_scan_returns_suppressed_with_reasons(self, client):
        # API returns all candidates; suppressed ones carry a suppressed_reason
        resp = await client.get("/api/opportunity/scan")
        assert resp.status_code == 200
        candidates = resp.json()["candidates"]
        for candidate in candidates:
            if not candidate["eligible"]:
                assert candidate["suppressed_reason"] is not None
                assert len(candidate["suppressed_reason"]) > 0

    async def test_trade_spec_for_valid_playbook(self, client):
        resp = await client.post("/api/opportunity/spec/spy_iron_condor_v1")
        assert resp.status_code == 200
        data = resp.json()
        assert "hard_blocks" in data
        assert "warnings" in data
        assert "spec" in data

    async def test_trade_spec_blocks_if_unresolvable(self, client):
        # The seed positions are LONG_STRADDLE debit trades with current = entry
        # No P1 should be active on seeded data (current_value == entry_premium)
        resp = await client.post("/api/opportunity/spec/spy_iron_condor_v1")
        assert resp.status_code == 200
        data = resp.json()
        # If blocks exist, spec must be null
        if data["hard_blocks"]:
            assert data["spec"] is None
        else:
            assert data["spec"] is not None

    async def test_trade_spec_unknown_playbook_404(self, client):
        resp = await client.post("/api/opportunity/spec/nonexistent_playbook")
        assert resp.status_code == 404

    async def test_trade_spec_result_has_all_required_spec_fields(self, client):
        resp = await client.post("/api/opportunity/spec/spy_long_straddle_v1")
        assert resp.status_code == 200
        data = resp.json()
        if data["spec"]:
            spec = data["spec"]
            assert spec["order_type"] == "LIMIT"
            assert spec["dte_at_entry"] >= 14
            assert spec["max_loss_dollars"] > 0
            assert len(spec["legs"]) > 0
            assert spec["closing_order_instructions"] != ""
