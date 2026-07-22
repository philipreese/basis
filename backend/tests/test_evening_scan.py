"""
test_evening_scan.py — Automatic Evening Scan orchestration

Tests cover:
- Full chain (market fetch -> position refresh -> Layer A/C counts) runs
  correctly when Alpaca is configured
- Graceful degradation when Alpaca is unconfigured, and when configured but
  the live fetch fails — never raises, existing rows left untouched
- Staleness gate: a second call the same calendar day is a no-op; `force=True`
  bypasses it; a new calendar day re-runs automatically
- The session_scan_state singleton row is upserted, never duplicated
- P1/P2 lifecycle counts and eligible-candidate counts (respecting the
  playbook `enabled` flag and portfolio gates) match direct computation
- POST /api/session/evening-scan response shape and staleness behavior
"""

import pytest
import pytest_asyncio
from datetime import date
from unittest.mock import patch
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select
from httpx import AsyncClient
import httpx

from backend.models import (
    Base, PortfolioConfigModel, MarketStateModel, PositionModel,
    PlaybookDefinitionModel, SessionScanStateModel, OperationalJournalEntrySchema,
)
from backend.database import SEED_PORTFOLIO_CONFIG, SEED_PLAYBOOKS, get_db
from backend.session_scan import run_evening_scan
from backend.main import app

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
TODAY = date(2026, 7, 22)

_TEST_JOURNAL = OperationalJournalEntrySchema(
    core_thesis_rationale="Test rationale",
    structural_invalidation="Test invalidation",
    expected_underlying_move_pct=2.0,
    pre_trade_emotional_state="Calm",
    pre_trade_confidence_rating=3,
).model_dump()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    session_maker = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with session_maker() as session:
        yield session


@pytest_asyncio.fixture
async def seeded_db(db_session):
    """Seed config, CALM_BULL market state, and the full seed playbook library."""
    db_session.add(PortfolioConfigModel(
        id=1,
        account=SEED_PORTFOLIO_CONFIG["account"],
        risk_profile=SEED_PORTFOLIO_CONFIG["risk_profile"],
        portfolio_greek_limits=SEED_PORTFOLIO_CONFIG["portfolio_greek_limits"],
    ))
    db_session.add(MarketStateModel(
        id=1,
        current_regime="CALM_BULL",
        spy_price=758.0,
        spy_sma20=750.0,
        vix_close=14.5,
        underlying_ivrs={"SPY": 25.0},
        spy_daily_return=0.005,
        catalyst_dates=[],
        regime_scores={},
    ))
    for pb in SEED_PLAYBOOKS:
        db_session.add(PlaybookDefinitionModel(
            id=pb["id"], version=pb["version"], name=pb["name"],
            underlying_ticker=pb["underlying_ticker"], strategy_type=pb["strategy_type"],
            execution_mode=pb["execution_mode"], enabled=pb.get("enabled", True),
            entry_filters=pb["entry_filters"], execution_specs=pb["execution_specs"],
            exit_rules=pb["exit_rules"],
        ))
    await db_session.commit()
    return db_session


@pytest_asyncio.fixture
async def api_client(db_engine):
    session_maker = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def api_client_seeded(seeded_db, api_client):
    return api_client


def _add_open_position(
    db_session,
    pos_id: str,
    strategy_type: str = "BULL_PUT_SPREAD",
    entry_premium: float = 1.0,
    current_value_per_share: float = 1.0,
    premium_direction: str = "CREDIT",
    expiration_date: str = "2026-12-18",
    max_loss: float = 4.0,
) -> PositionModel:
    pos = PositionModel(
        id=pos_id,
        underlying="SPY",
        strategy_type=strategy_type,
        execution_mode="PAPER",
        legs=[
            {"option_type": "PUT", "direction": "SHORT", "strike": 735.0, "expiration": expiration_date, "delta": -0.3, "theta": 0.05, "vega": -0.1, "gamma": 0.02},
            {"option_type": "PUT", "direction": "LONG", "strike": 730.0, "expiration": expiration_date, "delta": -0.1, "theta": 0.02, "vega": -0.05, "gamma": 0.01},
        ],
        entry_date="2026-06-20",
        expiration_date=expiration_date,
        entry_premium=entry_premium,
        premium_direction=premium_direction,
        current_value_per_share=current_value_per_share,
        contracts=1,
        max_profit=entry_premium,
        max_loss=max_loss,
        notes="",
        rolls=0,
        status="OPEN",
        journal=_TEST_JOURNAL,
    )
    db_session.add(pos)
    return pos


# ---------------------------------------------------------------------------
# Full chain / graceful degradation
# ---------------------------------------------------------------------------

class TestChainExecution:
    @pytest.mark.asyncio
    async def test_chain_runs_all_steps_when_alpaca_configured(self, seeded_db):
        _add_open_position(seeded_db, "p1", expiration_date="2026-12-18")
        await seeded_db.commit()

        fake_telemetry = {"spy_price": 770.0, "spy_sma20": 750.0, "spy_daily_return": 0.015, "vix_close": 16.0}
        fake_quotes = {
            "SPY261218P00735000": 2.0,
            "SPY261218P00730000": 0.5,
        }

        with patch("backend.session_scan.is_configured", return_value=True), \
             patch("backend.session_scan.fetch_market_telemetry", return_value=fake_telemetry), \
             patch("backend.session_scan.fetch_options_latest_quotes", return_value=fake_quotes):
            result = await run_evening_scan(seeded_db, today=TODAY)

        assert result.ran is True
        assert result.state.market_fetch_status == "OK"
        assert result.state.position_refresh_status == "OK"
        assert result.state.last_scan_date == TODAY.isoformat()

        market_row = (await seeded_db.execute(select(MarketStateModel).filter_by(id=1))).scalar_one()
        assert market_row.spy_price == 770.0
        assert market_row.vix_close == 16.0

        pos_row = (await seeded_db.execute(select(PositionModel).filter_by(id="p1"))).scalar_one()
        # CREDIT: short_val - long_val = 2.0 - 0.5 = 1.5
        assert pos_row.current_value_per_share == 1.5

    @pytest.mark.asyncio
    async def test_graceful_degradation_when_unconfigured(self, seeded_db):
        _add_open_position(seeded_db, "p1")
        await seeded_db.commit()

        with patch("backend.session_scan.is_configured", return_value=False):
            result = await run_evening_scan(seeded_db, today=TODAY)

        assert result.ran is True
        assert result.state.market_fetch_status == "UNCONFIGURED"
        assert result.state.position_refresh_status == "UNCONFIGURED"

        market_row = (await seeded_db.execute(select(MarketStateModel).filter_by(id=1))).scalar_one()
        assert market_row.spy_price == 758.0  # unchanged from seed

        pos_row = (await seeded_db.execute(select(PositionModel).filter_by(id="p1"))).scalar_one()
        assert pos_row.current_value_per_share == 1.0  # unchanged

    @pytest.mark.asyncio
    async def test_graceful_degradation_when_configured_but_fetch_fails(self, seeded_db):
        _add_open_position(seeded_db, "p1")
        await seeded_db.commit()

        with patch("backend.session_scan.is_configured", return_value=True), \
             patch("backend.session_scan.fetch_market_telemetry", return_value=None), \
             patch("backend.session_scan.fetch_options_latest_quotes", return_value={}):
            result = await run_evening_scan(seeded_db, today=TODAY)

        assert result.ran is True
        assert result.state.market_fetch_status == "FAILED"
        assert result.state.position_refresh_status == "FAILED"

        market_row = (await seeded_db.execute(select(MarketStateModel).filter_by(id=1))).scalar_one()
        assert market_row.spy_price == 758.0  # unchanged

        pos_row = (await seeded_db.execute(select(PositionModel).filter_by(id="p1"))).scalar_one()
        assert pos_row.current_value_per_share == 1.0  # unchanged


# ---------------------------------------------------------------------------
# Staleness gate
# ---------------------------------------------------------------------------

class TestStalenessGate:
    @pytest.mark.asyncio
    async def test_second_call_same_day_is_skipped(self, seeded_db):
        with patch("backend.session_scan.is_configured", return_value=False) as mock_configured:
            first = await run_evening_scan(seeded_db, today=TODAY)
            second = await run_evening_scan(seeded_db, today=TODAY)

        assert first.ran is True
        assert second.ran is False
        assert second.state.last_scan_at == first.state.last_scan_at

    @pytest.mark.asyncio
    async def test_force_reruns_even_if_already_ran_today(self, seeded_db):
        with patch("backend.session_scan.is_configured", return_value=False):
            first = await run_evening_scan(seeded_db, today=TODAY)
            second = await run_evening_scan(seeded_db, today=TODAY, force=True)

        assert first.ran is True
        assert second.ran is True

    @pytest.mark.asyncio
    async def test_new_calendar_day_triggers_automatic_rerun(self, seeded_db):
        tomorrow = date(2026, 7, 23)
        with patch("backend.session_scan.is_configured", return_value=False):
            first = await run_evening_scan(seeded_db, today=TODAY)
            second = await run_evening_scan(seeded_db, today=tomorrow)

        assert first.ran is True
        assert second.ran is True
        assert second.state.last_scan_date == tomorrow.isoformat()

    @pytest.mark.asyncio
    async def test_singleton_row_never_duplicated(self, seeded_db):
        tomorrow = date(2026, 7, 23)
        with patch("backend.session_scan.is_configured", return_value=False):
            await run_evening_scan(seeded_db, today=TODAY)
            await run_evening_scan(seeded_db, today=TODAY)  # skipped
            await run_evening_scan(seeded_db, today=tomorrow)  # new day

        rows = (await seeded_db.execute(select(SessionScanStateModel))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == 1
        assert rows[0].last_scan_date == tomorrow.isoformat()


# ---------------------------------------------------------------------------
# Layer A / Layer C counts
# ---------------------------------------------------------------------------

class TestCounts:
    @pytest.mark.asyncio
    async def test_p1_p2_counts_match_expected(self, seeded_db):
        # P1: credit trade, loss >= 2x entry premium
        _add_open_position(seeded_db, "p1_close_now", entry_premium=1.0, current_value_per_share=3.0, expiration_date="2026-12-18")
        # P2: within 21 DTE (today=2026-07-22, expiration 10 days out)
        _add_open_position(seeded_db, "p2_time_rule", entry_premium=1.0, current_value_per_share=1.05, expiration_date="2026-08-01")
        # Neither: far expiration, no P&L trigger, no regime conflict (BULL_PUT_SPREAD in CALM_BULL)
        _add_open_position(seeded_db, "p3_ok", entry_premium=1.0, current_value_per_share=1.05, expiration_date="2026-12-18")
        await seeded_db.commit()

        with patch("backend.session_scan.is_configured", return_value=False):
            result = await run_evening_scan(seeded_db, today=TODAY)

        assert result.state.p1_count == 1
        assert result.state.p2_count == 1

    @pytest.mark.asyncio
    async def test_eligible_candidate_count_respects_enabled_flag(self, db_session):
        db_session.add(PortfolioConfigModel(
            id=1,
            account=SEED_PORTFOLIO_CONFIG["account"],
            risk_profile=SEED_PORTFOLIO_CONFIG["risk_profile"],
            portfolio_greek_limits=SEED_PORTFOLIO_CONFIG["portfolio_greek_limits"],
        ))
        db_session.add(MarketStateModel(
            id=1, current_regime="CALM_BULL", spy_price=758.0, spy_sma20=750.0,
            vix_close=14.5, underlying_ivrs={"SPY": 25.0}, spy_daily_return=0.005,
            catalyst_dates=[], regime_scores={},
        ))
        entry_filters = {
            "min_ivr": 0.0, "max_ivr": 100.0, "vix_range": [0.0, 100.0],
            "required_trend": "ANY", "block_catalyst_14dte": False, "require_catalyst_14dte": False,
        }
        execution_specs = {"target_dte": 38, "short_leg_delta": 0.30, "long_leg_delta": 0.10, "spread_width_dollars": 5.0, "straddle_atm": False}
        exit_rules = {"profit_take_pct": 50.0, "stop_loss_pct": 200.0, "mandatory_exit_dte": 21, "catalyst_exit_days_after": 5}
        db_session.add(PlaybookDefinitionModel(
            id="enabled_pb", version="1.0", name="Enabled", underlying_ticker="SPY",
            strategy_type="BULL_PUT_SPREAD", execution_mode="PAPER", enabled=True,
            entry_filters=entry_filters, execution_specs=execution_specs, exit_rules=exit_rules,
        ))
        db_session.add(PlaybookDefinitionModel(
            id="disabled_pb", version="1.0", name="Disabled", underlying_ticker="SPY",
            strategy_type="BULL_PUT_SPREAD", execution_mode="PAPER", enabled=False,
            entry_filters=entry_filters, execution_specs=execution_specs, exit_rules=exit_rules,
        ))
        await db_session.commit()

        with patch("backend.session_scan.is_configured", return_value=False):
            result = await run_evening_scan(db_session, today=TODAY)

        assert result.state.eligible_candidate_count == 1

    @pytest.mark.asyncio
    async def test_portfolio_gate_block_still_runs_market_and_position_steps(self, seeded_db):
        # Force a portfolio-level block: max_simultaneous_positions = 0
        config_row = (await seeded_db.execute(select(PortfolioConfigModel).filter_by(id=1))).scalar_one()
        config_row.risk_profile = {**config_row.risk_profile, "max_simultaneous_positions": 0}
        await seeded_db.commit()

        with patch("backend.session_scan.is_configured", return_value=True), \
             patch("backend.session_scan.fetch_market_telemetry", return_value={"spy_price": 760.0, "spy_sma20": 750.0, "spy_daily_return": 0.01, "vix_close": 15.0}), \
             patch("backend.session_scan.fetch_options_latest_quotes", return_value={}):
            result = await run_evening_scan(seeded_db, today=TODAY)

        assert result.state.eligible_candidate_count == 0
        assert result.state.market_fetch_status == "OK"
        assert result.state.position_refresh_status == "OK"  # no open positions


# ---------------------------------------------------------------------------
# HTTP smoke tests
# ---------------------------------------------------------------------------

class TestEveningScanEndpoint:
    @pytest.mark.asyncio
    async def test_response_shape(self, api_client_seeded):
        with patch.dict("os.environ", {"ALPACA_API_KEY_ID": "", "ALPACA_SECRET_KEY": ""}):
            resp = await api_client_seeded.post("/api/session/evening-scan")
        assert resp.status_code == 200
        body = resp.json()
        assert "ran" in body
        assert set(body["state"].keys()) == {
            "last_scan_at", "last_scan_date", "p1_count", "p2_count",
            "eligible_candidate_count", "market_fetch_status", "position_refresh_status",
        }

    @pytest.mark.asyncio
    async def test_second_call_same_day_is_skipped(self, api_client_seeded):
        with patch.dict("os.environ", {"ALPACA_API_KEY_ID": "", "ALPACA_SECRET_KEY": ""}):
            first = await api_client_seeded.post("/api/session/evening-scan")
            second = await api_client_seeded.post("/api/session/evening-scan")
        assert first.json()["ran"] is True
        assert second.json()["ran"] is False

    @pytest.mark.asyncio
    async def test_force_query_param_reruns(self, api_client_seeded):
        with patch.dict("os.environ", {"ALPACA_API_KEY_ID": "", "ALPACA_SECRET_KEY": ""}):
            first = await api_client_seeded.post("/api/session/evening-scan")
            second = await api_client_seeded.post("/api/session/evening-scan?force=true")
        assert first.json()["ran"] is True
        assert second.json()["ran"] is True
