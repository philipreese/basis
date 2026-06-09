import pytest
import pytest_asyncio
import datetime
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from httpx import AsyncClient
import httpx

from backend.models import (
    Base, PlaybookDefinitionSchema, OptionLegSchema,
    PositionSchema, OperationalJournalEntrySchema,
    PortfolioConfigSchema, PortfolioConfigModel, PositionModel,
    MarketStateSchema, MarketStateModel
)
from backend.database import SEED_PORTFOLIO_CONFIG, SEED_POSITIONS, get_db

_TEST_JOURNAL = OperationalJournalEntrySchema(
    core_thesis_rationale="Test rationale",
    structural_invalidation="Test invalidation",
    expected_underlying_move_pct=2.0,
    pre_trade_emotional_state="Calm",
    pre_trade_confidence_rating=3,
)
from backend.observation import (
    calculate_dte, run_lifecycle_scan,
    aggregate_portfolio_greeks, run_exposure_safeguards
)
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
        # Seed portfolio config
        new_config = PortfolioConfigModel(
            id=1,
            account=SEED_PORTFOLIO_CONFIG["account"],
            risk_profile=SEED_PORTFOLIO_CONFIG["risk_profile"],
            portfolio_greek_limits=SEED_PORTFOLIO_CONFIG["portfolio_greek_limits"]
        )
        session.add(new_config)
        
        # Seed default market state
        new_mstate = MarketStateModel(
            id=1,
            current_regime="CALM_BULL",
            spy_price=758.0,
            catalyst_dates=["2026-06-08"]
        )
        session.add(new_mstate)

        # Seed positions
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
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# =====================================================================
# Unit Tests: Lifecycle Scan (P1, P2, P3, OK)
# =====================================================================

def test_lifecycle_scan_p1_credit_loss():
    # Credit position: entry premium = 2.0, max_loss = 3.0. contracts = 1
    # 2x loss threshold = 4.0. Loss per share = current_value - entry_premium
    # Current value = 6.0 => Loss = 4.0 (Exactly 2x premium collected)
    pos = PositionSchema(
        id="test_pos_p1_credit_loss",
        underlying="AAPL",
        strategy_type="IRON_CONDOR",
        execution_mode="PAPER",
        legs=[],
        entry_date="2026-06-01",
        expiration_date="2026-07-01",
        entry_premium=2.0,
        premium_direction="CREDIT",
        current_value_per_share=6.0,
        contracts=1,
        max_profit=2.0,
        max_loss=3.0,
        status="OPEN",
        notes="Test P1 Credit Loss",
        journal=_TEST_JOURNAL,
    )
    res = run_lifecycle_scan(pos, "CALM_BULL", 180.0, [], today=datetime.date(2026, 6, 1))
    assert res["priority"] == "P1 — CLOSE NOW"
    assert "Loss limit reached" in res["reason"]

def test_lifecycle_scan_p1_credit_profit():
    # Credit position: entry premium = 2.0. Profit threshold = 50% = 1.0
    # Profit = entry_premium - current_value
    # Current value = 1.0 => Profit = 1.0 (Exactly 50%)
    pos = PositionSchema(
        id="test_pos_p1_credit_profit",
        underlying="AAPL",
        strategy_type="IRON_CONDOR",
        execution_mode="PAPER",
        legs=[],
        entry_date="2026-06-01",
        expiration_date="2026-07-01",
        entry_premium=2.0,
        premium_direction="CREDIT",
        current_value_per_share=1.0,
        contracts=1,
        max_profit=2.0,
        max_loss=3.0,
        status="OPEN",
        notes="Test P1 Credit Profit",
        journal=_TEST_JOURNAL,
    )
    res = run_lifecycle_scan(pos, "CALM_BULL", 180.0, [], today=datetime.date(2026, 6, 1))
    assert res["priority"] == "P1 — CLOSE NOW"
    assert "Profit target reached" in res["reason"]

def test_lifecycle_scan_p1_debit_profit():
    # Debit position: entry premium = 2.0. 100% gain threshold = 2.0
    # Gain = current_value - entry_premium
    # Current value = 4.0 => Gain = 2.0 (Exactly 100%)
    pos = PositionSchema(
        id="test_pos_p1_debit_profit",
        underlying="AAPL",
        strategy_type="BULL_CALL_SPREAD",
        execution_mode="PAPER",
        legs=[],
        entry_date="2026-06-01",
        expiration_date="2026-07-01",
        entry_premium=2.0,
        premium_direction="DEBIT",
        current_value_per_share=4.0,
        contracts=1,
        max_profit=3.0,
        max_loss=2.0,
        status="OPEN",
        notes="Test P1 Debit Profit",
        journal=_TEST_JOURNAL,
    )
    res = run_lifecycle_scan(pos, "CALM_BULL", 180.0, [], today=datetime.date(2026, 6, 1))
    assert res["priority"] == "P1 — CLOSE NOW"
    assert "Profit target reached" in res["reason"]

def test_lifecycle_scan_p2_dte():
    # DTE threshold: DTE <= 21
    # Expiration July 22, today July 1 => DTE = 21 (Exactly 21)
    pos = PositionSchema(
        id="test_pos_p2_dte",
        underlying="AAPL",
        strategy_type="IRON_CONDOR",
        execution_mode="PAPER",
        legs=[],
        entry_date="2026-06-01",
        expiration_date="2026-07-22",
        entry_premium=2.0,
        premium_direction="CREDIT",
        current_value_per_share=2.0,
        contracts=1,
        max_profit=2.0,
        max_loss=3.0,
        status="OPEN",
        notes="Test P2 DTE",
        journal=_TEST_JOURNAL,
    )
    # Today: 2026-07-01. Exp: 2026-07-22. DTE = 21. Should trigger P2
    res = run_lifecycle_scan(pos, "CALM_BULL", 180.0, [], today=datetime.date(2026, 7, 1))
    assert res["priority"] == "P2 — CLOSE SOON"
    assert "Time limit warning" in res["reason"]

    # DTE = 22. Should NOT trigger P2 DTE (will be OK)
    res2 = run_lifecycle_scan(pos, "CALM_BULL", 180.0, [], today=datetime.date(2026, 6, 30))
    assert res2["priority"] == "OK"

def test_lifecycle_scan_p2_regime_bear_bullish():
    # Bear market + Bull Call Spread
    pos = PositionSchema(
        id="test_pos_p2_bear",
        underlying="AAPL",
        strategy_type="BULL_CALL_SPREAD",
        execution_mode="PAPER",
        legs=[],
        entry_date="2026-06-01",
        expiration_date="2026-07-30",
        entry_premium=2.0,
        premium_direction="DEBIT",
        current_value_per_share=2.0,
        contracts=1,
        max_profit=3.0,
        max_loss=2.0,
        status="OPEN",
        notes="Test Bear Conflict",
        journal=_TEST_JOURNAL,
    )
    res = run_lifecycle_scan(pos, "TRENDING_BEAR", 180.0, [], today=datetime.date(2026, 6, 1))
    assert res["priority"] == "P2 — REVIEW"
    assert "Regime conflict detected" in res["reason"]

def test_lifecycle_scan_p2_iron_condor_breach():
    # Iron condor short strikes: short put 95, short call 105.
    # Breached short put: SPY price <= 95 * 1.02 = 96.9.
    # Breached short call: SPY price >= 105 * 0.98 = 102.9.
    pos = PositionSchema(
        id="test_pos_ic",
        underlying="SPY",
        strategy_type="IRON_CONDOR",
        execution_mode="PAPER",
        legs=[
            OptionLegSchema(option_type="PUT", direction="LONG", strike=90.0, expiration="2026-07-30", delta=-0.15, theta=-0.01, vega=0.05, gamma=0.01),
            OptionLegSchema(option_type="PUT", direction="SHORT", strike=95.0, expiration="2026-07-30", delta=-0.30, theta=0.04, vega=-0.10, gamma=-0.02),
            OptionLegSchema(option_type="CALL", direction="SHORT", strike=105.0, expiration="2026-07-30", delta=0.30, theta=0.04, vega=-0.10, gamma=-0.02),
            OptionLegSchema(option_type="CALL", direction="LONG", strike=110.0, expiration="2026-07-30", delta=0.15, theta=-0.01, vega=0.05, gamma=0.01)
        ],
        entry_date="2026-06-01",
        expiration_date="2026-07-30",
        entry_premium=1.5,
        premium_direction="CREDIT",
        current_value_per_share=1.5,
        contracts=1,
        max_profit=1.5,
        max_loss=3.5,
        status="OPEN",
        notes="Iron Condor",
        journal=_TEST_JOURNAL,
    )
    
    # 1. SPY price = 97.0 (Not breached, since 97.0 > 96.9 and 97.0 < 102.9)
    res_ok = run_lifecycle_scan(pos, "HIGH_VOL_NEUTRAL", 97.0, [], today=datetime.date(2026, 6, 1))
    assert res_ok["priority"] == "OK"

    # 2. SPY price = 96.9 (Exactly breached on put side, since 96.9 <= 96.9)
    res_put = run_lifecycle_scan(pos, "HIGH_VOL_NEUTRAL", 96.9, [], today=datetime.date(2026, 6, 1))
    assert res_put["priority"] == "P2 — REVIEW"
    assert "short put strike 95.0 breached within 2%" in res_put["reason"]

    # 3. SPY price = 102.9 (Exactly breached on call side, since 102.9 >= 102.9)
    res_call = run_lifecycle_scan(pos, "HIGH_VOL_NEUTRAL", 102.9, [], today=datetime.date(2026, 6, 1))
    assert res_call["priority"] == "P2 — REVIEW"
    assert "short call strike 105.0 breached within 2%" in res_call["reason"]

def test_lifecycle_scan_p2_catalyst_conflict():
    # EVENT_CATALYST + short premium position expiring within 14 days of catalyst
    pos = PositionSchema(
        id="test_pos_catalyst",
        underlying="AAPL",
        strategy_type="IRON_CONDOR",
        execution_mode="PAPER",
        legs=[],
        entry_date="2026-06-01",
        expiration_date="2026-07-15",
        entry_premium=2.0,
        premium_direction="CREDIT",
        current_value_per_share=2.0,
        contracts=1,
        max_profit=2.0,
        max_loss=3.0,
        status="OPEN",
        notes="Catalyst test",
        journal=_TEST_JOURNAL,
    )
    # Catalyst date July 1 => Exp July 15 => Diff = 14 days. Should trigger conflict
    res = run_lifecycle_scan(pos, "EVENT_CATALYST", 180.0, ["2026-07-01"], today=datetime.date(2026, 6, 1))
    assert res["priority"] == "P2 — REVIEW"
    assert "within 14 days of catalyst" in res["reason"]

    # Catalyst July 30 => Exp July 15 => Diff = 15 days. Should NOT trigger conflict
    res_ok = run_lifecycle_scan(pos, "EVENT_CATALYST", 180.0, ["2026-07-30"], today=datetime.date(2026, 6, 1))
    assert res_ok["priority"] == "OK"

def test_lifecycle_scan_p3_credit_profit_approaching():
    # Credit position: entry = 2.0. 35% target = 0.70.
    # Current value = 1.30 => Profit = 0.70 (Exactly 35%)
    pos = PositionSchema(
        id="test_pos_p3_credit",
        underlying="AAPL",
        strategy_type="IRON_CONDOR",
        execution_mode="PAPER",
        legs=[],
        entry_date="2026-06-01",
        expiration_date="2026-07-30",
        entry_premium=2.0,
        premium_direction="CREDIT",
        current_value_per_share=1.30,
        contracts=1,
        max_profit=2.0,
        max_loss=3.0,
        status="OPEN",
        notes="Monitor profit",
        journal=_TEST_JOURNAL,
    )
    res = run_lifecycle_scan(pos, "CALM_BULL", 180.0, [], today=datetime.date(2026, 6, 1))
    assert res["priority"] == "P3 — MONITOR"
    assert "Profit threshold approaching" in res["reason"]

def test_lifecycle_scan_p3_debit_loss_approaching():
    # Debit position: entry = 2.0. 35% loss target = 0.70.
    # Loss = entry_premium - current_value
    # Current value = 1.30 => Loss = 0.70 (Exactly 35%)
    pos = PositionSchema(
        id="test_pos_p3_debit",
        underlying="AAPL",
        strategy_type="BULL_CALL_SPREAD",
        execution_mode="PAPER",
        legs=[],
        entry_date="2026-06-01",
        expiration_date="2026-07-30",
        entry_premium=2.0,
        premium_direction="DEBIT",
        current_value_per_share=1.30,
        contracts=1,
        max_profit=3.0,
        max_loss=2.0,
        status="OPEN",
        notes="Monitor loss",
        journal=_TEST_JOURNAL,
    )
    res = run_lifecycle_scan(pos, "CALM_BULL", 180.0, [], today=datetime.date(2026, 6, 1))
    assert res["priority"] == "P3 — MONITOR"
    assert "Loss threshold approaching" in res["reason"]


# =====================================================================
# Unit Tests: Greeks Aggregation
# =====================================================================

def test_aggregate_portfolio_greeks():
    # Pos 1: LONG straddle, 2 contracts
    # Leg 1: Long Call, delta = 0.5, theta = -0.1, vega = 0.2, gamma = 0.05
    # Leg 2: Long Put, delta = -0.5, theta = -0.1, vega = 0.2, gamma = 0.05
    # Contribution Pos 1:
    # Delta: (0.5 * 2) + (-0.5 * 2) = 0
    # Theta: (-0.1 * 2) + (-0.1 * 2) = -0.4
    # Vega: (0.2 * 2) + (0.2 * 2) = 0.8
    # Gamma: (0.05 * 2) + (0.05 * 2) = 0.2
    pos1 = PositionSchema(
        id="p1", underlying="SPY", strategy_type="LONG_STRADDLE", execution_mode="PAPER",
        legs=[
            OptionLegSchema(option_type="CALL", direction="LONG", strike=750.0, expiration="2026-07-30", delta=0.5, theta=-0.1, vega=0.2, gamma=0.05),
            OptionLegSchema(option_type="PUT", direction="LONG", strike=750.0, expiration="2026-07-30", delta=-0.5, theta=-0.1, vega=0.2, gamma=0.05)
        ],
        entry_date="2026-06-01", expiration_date="2026-07-30", contracts=2, entry_premium=10.0,
        premium_direction="DEBIT", current_value_per_share=10.0, max_profit=999999.0, max_loss=10.0, status="OPEN", notes="", journal=_TEST_JOURNAL,
    )

    # Pos 2: Short premium (Credit), 1 contract
    # Leg 1: Short Call, delta = 0.4, theta = 0.05, vega = -0.15, gamma = -0.02
    # Contribution Pos 2 (SHORT):
    # Delta: -0.4 * 1 = -0.4
    # Theta: -0.05 * 1 = -0.05
    # Vega: -(-0.15) * 1 = 0.15
    # Gamma: -(-0.02) * 1 = 0.02
    pos2 = PositionSchema(
        id="p2", underlying="AAPL", strategy_type="IRON_CONDOR", execution_mode="PAPER",
        legs=[
            OptionLegSchema(option_type="CALL", direction="SHORT", strike=190.0, expiration="2026-07-30", delta=0.4, theta=0.05, vega=-0.15, gamma=-0.02)
        ],
        entry_date="2026-06-01", expiration_date="2026-07-30", contracts=1, entry_premium=2.0,
        premium_direction="CREDIT", current_value_per_share=2.0, max_profit=2.0, max_loss=3.0, status="OPEN", notes="", journal=_TEST_JOURNAL,
    )

    # Total expected:
    # Delta: 0 + (-0.4) = -0.4
    # Theta: -0.4 + (-0.05) = -0.45
    # Vega: 0.8 + 0.15 = 0.95
    # Gamma: 0.2 + 0.02 = 0.22
    res = aggregate_portfolio_greeks([pos1, pos2])
    assert res["net_delta"] == -0.4
    assert res["net_theta"] == -0.45
    assert res["net_vega"] == 0.95
    assert res["net_gamma"] == 0.22


# =====================================================================
# Unit Tests: Exposure Safeguards
# =====================================================================

def test_run_exposure_safeguards():
    config = PortfolioConfigSchema(
        account={
            "total_nav": 10000.0,
            "broker": "Schwab",
            "account_type": "Roth",
            "options_approval": "Level 3",
            "execution_mode": "PAPER"
        },
        risk_profile={
            "max_trade_risk_pct": 15.0,
            "max_trade_risk_dollars": 1500.0,
            "max_underlying_concentration_pct": 35.0,
            "max_correlated_index_pct": 50.0,
            "minimum_cash_reserve_pct": 15.0,
            "max_simultaneous_positions": 2, # Limit = 2
            "max_capital_deployed_pct": 80.0
        },
        portfolio_greek_limits={
            "max_net_delta": 50.0,
            "max_net_vega": 100.0,
            "max_net_gamma": 10.0
        }
    )

    # 1. 2 open positions, both in SPY
    # Pos 1: SPY, max_loss = 25.0, contracts = 1 => Capital = $2,500
    # Pos 2: SPY, max_loss = 15.0, contracts = 1 => Capital = $1,500
    # Total capital in SPY = $4,000 (40.0% concentration - triggers SPY concentration limit > 35%)
    # Total index capital = $4,000 (40.0% index concentration - OK, since 40.0 <= 50.0)
    # Total open positions = 2 (triggers Position Count limit >= 2)
    # Total capital deployed = $4,000 (40.0% - OK, since 40.0 < 80.0)
    pos1 = PositionSchema(
        id="pos1", underlying="SPY", strategy_type="LONG_STRADDLE", execution_mode="PAPER", legs=[],
        entry_date="2026-06-01", expiration_date="2026-07-30", contracts=1, entry_premium=25.0,
        premium_direction="DEBIT", current_value_per_share=25.0, max_profit=999999.0, max_loss=25.0, status="OPEN", notes="", journal=_TEST_JOURNAL,
    )
    pos2 = PositionSchema(
        id="pos2", underlying="SPY", strategy_type="LONG_STRADDLE", execution_mode="PAPER", legs=[],
        entry_date="2026-06-01", expiration_date="2026-07-30", contracts=1, entry_premium=15.0,
        premium_direction="DEBIT", current_value_per_share=15.0, max_profit=999999.0, max_loss=15.0, status="OPEN", notes="", journal=_TEST_JOURNAL,
    )

    warnings = run_exposure_safeguards([pos1, pos2], config)
    types = [w["type"] for w in warnings]
    
    assert "POSITION_COUNT" in types
    assert "UNDERLYING_CONCENTRATION" in types
    assert "CORRELATED_INDEX_CONCENTRATION" not in types
    assert "CAPITAL_DEPLOYED" not in types


# =====================================================================
# HTTP Integration Tests: Endpoints & Flow
# =====================================================================

@pytest.mark.anyio
async def test_api_market_state_get_post(client):
    # GET market state
    response = await client.get("/api/market/state")
    assert response.status_code == 200
    data = response.json()
    assert data["current_regime"] == "CALM_BULL"
    assert data["spy_price"] == 758.0

    # POST bearish telemetry — regime is recomputed server-side to TRENDING_BEAR
    updated_state = {
        "spy_price": 720.0,       # well below SMA → BELOW_FALLING
        "spy_sma20": 750.0,
        "vix_close": 35.0,        # VIX_HIGH
        "underlying_ivrs": {"SPY": 75.0},  # IVR_HIGH
        "spy_daily_return": -0.025,        # DAY_DOWN_2PLUS
        "catalyst_dates": ["2026-06-15"],
        "current_regime": "CALM_BULL",  # ignored — recomputed
        "regime_scores": {},
    }
    post_res = await client.post("/api/market/state", json=updated_state)
    assert post_res.status_code == 200
    post_data = post_res.json()
    assert post_data["current_regime"] == "TRENDING_BEAR"
    assert post_data["spy_price"] == 720.0
    assert "2026-06-15" in post_data["catalyst_dates"]

    # Re-GET to verify database persistence
    get_res = await client.get("/api/market/state")
    assert get_res.json()["current_regime"] == "TRENDING_BEAR"

@pytest.mark.anyio
async def test_api_portfolio_observation(client):
    # Retrieve observation dashboard scan
    response = await client.get("/api/portfolio/observation")
    assert response.status_code == 200
    data = response.json()
    
    assert "scanned_positions" in data
    assert "greeks" in data
    assert "safeguards" in data
    assert "market_state" in data
    
    # Check that seeded straddles are present
    scanned_pos = data["scanned_positions"]
    ids = [pos["position_id"] for pos in scanned_pos]
    assert "seed_pos_spy_straddle_jun18" in ids
    assert "seed_pos_spy_straddle_jul18" in ids

    # Check Greeks structure
    greeks = data["greeks"]
    assert "net_delta" in greeks
    assert "net_gamma" in greeks
