"""Tests for the Operator nightly pipeline (backend/operator.py, #23)."""

from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import operator
from backend.database import SEED_PLAYBOOKS, SEED_PORTFOLIO_CONFIG, SEED_POSITIONS
from backend.models import (
    Base,
    MarketStateModel,
    PlaybookDefinitionModel,
    PortfolioConfigModel,
    PositionModel,
)
from backend.operator import compose_digest, run_evening_operation, send_ntfy

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with maker() as session:
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
                catalyst_dates=[],
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
                    execution_mode=p_data["execution_mode"],
                    legs=p_data["legs"],
                    entry_date=p_data["entry_date"],
                    expiration_date=p_data["expiration_date"],
                    entry_premium=p_data["entry_premium"],
                    premium_direction=p_data["premium_direction"],
                    current_value_per_share=p_data["current_value_per_share"],
                    contracts=p_data["contracts"],
                    max_profit=p_data["max_profit"],
                    max_loss=p_data["max_loss"],
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
                    execution_mode=pb_data["execution_mode"],
                    enabled=pb_data.get("enabled", True),
                    entry_filters=pb_data["entry_filters"],
                    execution_specs=pb_data["execution_specs"],
                    exit_rules=pb_data["exit_rules"],
                )
            )
        await session.commit()
    yield maker
    await engine.dispose()


TELEMETRY = {"spy_price": 760.0, "spy_sma20": 750.0, "vix_close": 16.0, "spy_daily_return": 0.004}


class TestRunEveningOperation:
    @pytest.mark.asyncio
    async def test_full_run_with_live_telemetry(self, session_maker):
        with (
            patch.object(operator, "fetch_market_telemetry", return_value=TELEMETRY),
            patch.object(operator, "fetch_options_latest_quotes", return_value={}),
        ):
            title, body, _priority = await run_evening_operation(session_maker)
        assert title.startswith("basis evening:")
        assert "Regime" in body
        assert "SPY 760.00" in body
        assert "stored data" not in body  # telemetry was live

    @pytest.mark.asyncio
    async def test_degrades_to_stored_state_when_fetch_fails(self, session_maker):
        with (
            patch.object(operator, "fetch_market_telemetry", return_value=None),
            patch.object(operator, "fetch_options_latest_quotes", return_value={}),
        ):
            _title, body, _priority = await run_evening_operation(session_maker)
        assert "stored data" in body
        assert "SPY 758.00" in body  # stored value, not live

    @pytest.mark.asyncio
    async def test_persists_recomputed_regime(self, session_maker):
        from sqlalchemy import select

        with (
            patch.object(operator, "fetch_market_telemetry", return_value=TELEMETRY),
            patch.object(operator, "fetch_options_latest_quotes", return_value={}),
        ):
            await run_evening_operation(session_maker)
        async with session_maker() as session:
            state = (await session.execute(select(MarketStateModel).filter_by(id=1))).scalar_one()
            assert state.spy_price == 760.0
            assert state.vix_close == 16.0


class TestComposeDigest:
    def _lifecycle(self, priority: str) -> dict:
        return {
            "position_id": "p1",
            "underlying": "SPY",
            "strategy_type": "LONG_STRADDLE",
            "priority": priority,
            "reason": "test reason",
        }

    def test_quiet_night(self):
        title, _body, priority = compose_digest(
            regime="CALM_BULL",
            spy_price=758.0,
            vix_close=14.5,
            telemetry_live=True,
            positions_repriced=0,
            lifecycle=[],
            safeguards=[],
            scan_result=None,
        )
        assert "all quiet" in title
        assert priority == "default"

    def test_p1_escalates_priority(self):
        title, body, priority = compose_digest(
            regime="CALM_BULL",
            spy_price=758.0,
            vix_close=14.5,
            telemetry_live=True,
            positions_repriced=1,
            lifecycle=[self._lifecycle("P1 — CLOSE NOW")],
            safeguards=[],
            scan_result=None,
        )
        assert "1 CLOSE NOW" in title
        assert priority == "high"
        assert "test reason" in body

    def test_safeguard_escalates_priority(self):
        _title, body, priority = compose_digest(
            regime="CALM_BULL",
            spy_price=758.0,
            vix_close=14.5,
            telemetry_live=True,
            positions_repriced=0,
            lifecycle=[],
            safeguards=[{"type": "CAPITAL_DEPLOYED", "severity": "WARNING", "message": "over limit"}],
            scan_result=None,
        )
        assert priority == "high"
        assert "CAPITAL_DEPLOYED" in body


class TestSendNtfy:
    def test_skipped_without_topic(self, monkeypatch):
        monkeypatch.delenv("NTFY_TOPIC", raising=False)
        assert send_ntfy("t", "b") is False

    def test_posts_to_topic(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC", "basis-test-topic")
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        with patch.object(operator.httpx, "post", return_value=mock_resp) as mock_post:
            assert send_ntfy("Title", "Body", "high") is True
        url = mock_post.call_args[0][0]
        assert url.endswith("/basis-test-topic")
        assert mock_post.call_args.kwargs["headers"]["Priority"] == "high"

    def test_returns_false_on_network_error(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC", "basis-test-topic")
        with patch.object(operator.httpx, "post", side_effect=RuntimeError("connection refused")):
            assert send_ntfy("Title", "Body") is False
