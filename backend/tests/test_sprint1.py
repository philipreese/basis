import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from httpx import AsyncClient
import httpx

from backend.models import (
    Base, PlaybookDefinitionSchema, OptionLegSchema,
    PositionSchema, OperationalJournalEntrySchema,
    PortfolioConfigSchema, PortfolioConfigModel, PositionModel
)
from backend.database import SEED_PORTFOLIO_CONFIG, SEED_POSITIONS, init_db, get_db
from backend.pricing import calculate_position_metrics
from backend.main import app

# Use in-memory SQLite for tests
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"

@pytest_asyncio.fixture
async def test_db():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    async with session_maker() as session:
        # Seed test db
        new_config = PortfolioConfigModel(
            id=1,
            account=SEED_PORTFOLIO_CONFIG["account"],
            risk_profile=SEED_PORTFOLIO_CONFIG["risk_profile"],
            portfolio_greek_limits=SEED_PORTFOLIO_CONFIG["portfolio_greek_limits"]
        )
        session.add(new_config)
        
        for p_data in SEED_POSITIONS:
            new_pos = PositionModel(
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
                profit_target_per_share=p_data.get("profit_target_per_share"),
                loss_limit_per_share=p_data.get("loss_limit_per_share"),
                break_even_upside=p_data.get("break_even_upside"),
                break_even_downside=p_data.get("break_even_downside"),
                notes=p_data["notes"],
                rolls=p_data["rolls"],
                status=p_data["status"],
                journal=p_data["journal"]
            )
            session.add(new_pos)
            
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
    # Using AsyncClient with lifespan enabled
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# =====================================================================
# Unit Tests: Pydantic Validation
# =====================================================================

def test_pydantic_portfolio_config_validation():
    # Valid config
    config = PortfolioConfigSchema(**SEED_PORTFOLIO_CONFIG)
    assert config.account.total_nav == 10000.0
    
    # Invalid config (missing field)
    bad_config = SEED_PORTFOLIO_CONFIG.copy()
    del bad_config["account"]
    with pytest.raises(Exception):
        PortfolioConfigSchema(**bad_config)


# =====================================================================
# Unit Tests: Pricing Math
# =====================================================================

def test_pricing_long_straddle():
    legs = [
        {"option_type": "CALL", "direction": "LONG", "strike": 757.0, "expiration": "2026-07-18"},
        {"option_type": "PUT", "direction": "LONG", "strike": 757.0, "expiration": "2026-07-18"}
    ]
    res = calculate_position_metrics("LONG_STRADDLE", legs, 28.18, "DEBIT")
    assert res["max_profit"] == 999999.0  # Safe inf mapping
    assert res["max_loss"] == 28.18
    assert res["break_even_downside"] == 757.0 - 28.18
    assert res["break_even_upside"] == 757.0 + 28.18

def test_pricing_bull_call_spread():
    legs = [
        {"option_type": "CALL", "direction": "LONG", "strike": 100.0, "expiration": "2026-07-18"},
        {"option_type": "CALL", "direction": "SHORT", "strike": 105.0, "expiration": "2026-07-18"}
    ]
    res = calculate_position_metrics("BULL_CALL_SPREAD", legs, 2.0, "DEBIT")
    assert res["max_profit"] == 3.0  # 5.0 width - 2.0 debit
    assert res["max_loss"] == 2.0
    assert res["break_even_upside"] == 102.0

def test_pricing_bear_put_spread():
    legs = [
        {"option_type": "PUT", "direction": "LONG", "strike": 100.0, "expiration": "2026-07-18"},
        {"option_type": "PUT", "direction": "SHORT", "strike": 95.0, "expiration": "2026-07-18"}
    ]
    res = calculate_position_metrics("BEAR_PUT_SPREAD", legs, 2.0, "DEBIT")
    assert res["max_profit"] == 3.0  # 5.0 width - 2.0 debit
    assert res["max_loss"] == 2.0
    assert res["break_even_downside"] == 98.0

def test_pricing_iron_condor():
    legs = [
        {"option_type": "PUT", "direction": "LONG", "strike": 90.0, "expiration": "2026-07-18"},
        {"option_type": "PUT", "direction": "SHORT", "strike": 95.0, "expiration": "2026-07-18"},
        {"option_type": "CALL", "direction": "SHORT", "strike": 105.0, "expiration": "2026-07-18"},
        {"option_type": "CALL", "direction": "LONG", "strike": 110.0, "expiration": "2026-07-18"}
    ]
    res = calculate_position_metrics("IRON_CONDOR", legs, 1.5, "CREDIT")
    assert res["max_profit"] == 1.5
    assert res["max_loss"] == 3.5  # 5.0 width - 1.5 credit
    assert res["break_even_downside"] == 93.5
    assert res["break_even_upside"] == 106.5


# =====================================================================
# Integration Tests: DB Seeding & HTTP API
# =====================================================================

@pytest.mark.anyio
async def test_get_portfolio_config(client):
    response = await client.get("/api/portfolio/config")
    assert response.status_code == 200
    data = response.json()
    assert data["account"]["total_nav"] == 10000.0
    assert data["risk_profile"]["max_trade_risk_pct"] == 15.0

@pytest.mark.anyio
async def test_get_positions(client):
    response = await client.get("/api/positions")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    ids = [pos["id"] for pos in data]
    assert "seed_pos_spy_straddle_jun18" in ids
    assert "seed_pos_spy_straddle_jul18" in ids

@pytest.mark.anyio
async def test_create_position(client):
    new_position = {
        "id": "test_pos_custom",
        "underlying": "AAPL",
        "strategy_type": "BULL_CALL_SPREAD",
        "execution_mode": "PAPER",
        "legs": [
            { "option_type": "CALL", "direction": "LONG", "strike": 180.0, "expiration": "2026-07-18", "delta": 0.5, "theta": -0.05, "vega": 0.1 },
            { "option_type": "CALL", "direction": "SHORT", "strike": 185.0, "expiration": "2026-07-18", "delta": 0.3, "theta": -0.03, "vega": 0.08 }
        ],
        "entry_date": "2026-06-08",
        "expiration_date": "2026-07-18",
        "entry_premium": 2.0,
        "premium_direction": "DEBIT",
        "current_value_per_share": 2.0,
        "contracts": 2,
        "max_profit": 3.0,
        "max_loss": 2.0,
        "profit_target_per_share": 1.5,
        "loss_limit_per_share": 1.0,
        "notes": "Custom test position",
        "rolls": 0,
        "status": "OPEN",
        "journal": {
            "core_thesis_rationale": "Bullish breakout study",
            "structural_invalidation": "AAPL drops below 175",
            "expected_underlying_move_pct": 5.0,
            "pre_trade_emotional_state": "Calm",
            "pre_trade_confidence_rating": 4
        }
    }
    
    response = await client.post("/api/positions", json=new_position)
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "test_pos_custom"
    assert data["underlying"] == "AAPL"
    
    # Get all positions, check it was added
    all_res = await client.get("/api/positions")
    assert len(all_res.json()) == 3
