from contextlib import asynccontextmanager
from typing import List
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.database import get_db, init_db
from backend.models import (
    PortfolioConfigSchema, PortfolioConfigModel,
    PositionSchema, PositionModel,
    PlaybookDefinitionSchema, PlaybookDefinitionModel,
    MarketStateSchema, MarketStateModel
)
from backend.observation import (
    run_lifecycle_scan,
    aggregate_portfolio_greeks,
    run_exposure_safeguards
)
from backend.regime import compute_regime
from backend.market_data import fetch_market_telemetry

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize database and run seeding
    await init_db()
    yield

app = FastAPI(
    title="Options Playbook Automation Engine",
    description="Daily decision-support & automated playbook matching API",
    version="0.1.0",
    lifespan=lifespan
)

# CORS middleware for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =====================================================================
# Portfolio Config Endpoints
# =====================================================================

@app.get("/api/portfolio/config", response_model=PortfolioConfigSchema)
async def get_portfolio_config(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PortfolioConfigModel).filter_by(id=1))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Portfolio configuration not found")
    return config.to_schema()

@app.post("/api/portfolio/config", response_model=PortfolioConfigSchema)
async def update_portfolio_config(new_config: PortfolioConfigSchema, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PortfolioConfigModel).filter_by(id=1))
    config = result.scalar_one_or_none()
    
    if not config:
        config = PortfolioConfigModel(id=1)
        db.add(config)
        
    config.account = new_config.account.model_dump()
    config.risk_profile = new_config.risk_profile.model_dump()
    config.portfolio_greek_limits = new_config.portfolio_greek_limits.model_dump()
    
    await db.commit()
    await db.refresh(config)
    return config.to_schema()

# =====================================================================
# Positions Endpoints
# =====================================================================

@app.get("/api/positions", response_model=List[PositionSchema])
async def get_positions(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PositionModel))
    positions = result.scalars().all()
    return [p.to_schema() for p in positions]

@app.get("/api/positions/{position_id}", response_model=PositionSchema)
async def get_position(position_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PositionModel).filter_by(id=position_id))
    position = result.scalar_one_or_none()
    if not position:
        raise HTTPException(status_code=404, detail="Position not found")
    return position.to_schema()

@app.post("/api/positions", response_model=PositionSchema)
async def create_position(new_pos: PositionSchema, db: AsyncSession = Depends(get_db)):
    # Check if position already exists
    exists_result = await db.execute(select(PositionModel).filter_by(id=new_pos.id))
    if exists_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Position with this ID already exists")
    
    pos_model = PositionModel(
        id=new_pos.id,
        underlying=new_pos.underlying,
        strategy_type=new_pos.strategy_type,
        execution_mode=new_pos.execution_mode,
        legs=[leg.model_dump() for leg in new_pos.legs],
        entry_date=new_pos.entry_date,
        expiration_date=new_pos.expiration_date,
        entry_premium=new_pos.entry_premium,
        premium_direction=new_pos.premium_direction,
        current_value_per_share=new_pos.current_value_per_share,
        contracts=new_pos.contracts,
        max_profit=new_pos.max_profit,
        max_loss=new_pos.max_loss,
        profit_target_per_share=new_pos.profit_target_per_share,
        loss_limit_per_share=new_pos.loss_limit_per_share,
        break_even_upside=new_pos.break_even_upside,
        break_even_downside=new_pos.break_even_downside,
        notes=new_pos.notes,
        rolls=new_pos.rolls,
        status=new_pos.status,
        playbook_id=new_pos.playbook_id,
        playbook_version=new_pos.playbook_version,
        playbook_snapshot=new_pos.playbook_snapshot.model_dump() if new_pos.playbook_snapshot else None,
        journal=new_pos.journal.model_dump() if new_pos.journal else None
    )
    
    db.add(pos_model)
    await db.commit()
    await db.refresh(pos_model)
    return pos_model.to_schema()

# =====================================================================
# Playbook Endpoints
# =====================================================================

@app.get("/api/playbooks", response_model=List[PlaybookDefinitionSchema])
async def get_playbooks(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PlaybookDefinitionModel))
    playbooks = result.scalars().all()
    return [pb.to_schema() for pb in playbooks]

@app.post("/api/playbooks", response_model=PlaybookDefinitionSchema)
async def create_playbook(new_pb: PlaybookDefinitionSchema, db: AsyncSession = Depends(get_db)):
    exists_result = await db.execute(
        select(PlaybookDefinitionModel).filter_by(id=new_pb.id, version=new_pb.version)
    )
    if exists_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Playbook with this ID and version already exists")
    
    pb_model = PlaybookDefinitionModel(
        id=new_pb.id,
        version=new_pb.version,
        name=new_pb.name,
        underlying_ticker=new_pb.underlying_ticker,
        strategy_type=new_pb.strategy_type,
        execution_mode=new_pb.execution_mode,
        entry_filters=new_pb.entry_filters.model_dump(),
        execution_specs=new_pb.execution_specs.model_dump(),
        exit_rules=new_pb.exit_rules.model_dump()
    )
    
    db.add(pb_model)
    await db.commit()
    await db.refresh(pb_model)
    return pb_model.to_schema()


# =====================================================================
# Market State and Observation Endpoints
# =====================================================================

@app.get("/api/market/state", response_model=MarketStateSchema)
async def get_market_state(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(MarketStateModel).filter_by(id=1))
    state = result.scalar_one_or_none()
    if not state:
        _regime, _scores = compute_regime(
            spy_price=758.0, spy_sma20=750.0, vix_close=14.5,
            underlying_ivrs={"SPY": 25.0}, spy_daily_return=0.005,
            catalyst_dates=["2026-06-08"]
        )
        state = MarketStateModel(
            id=1,
            current_regime=_regime,
            spy_price=758.0,
            spy_sma20=750.0,
            vix_close=14.5,
            underlying_ivrs={"SPY": 25.0},
            spy_daily_return=0.005,
            catalyst_dates=["2026-06-08"],
            regime_scores={k: float(v) for k, v in _scores.items()},
        )
        db.add(state)
        await db.commit()
        await db.refresh(state)
    return state.to_schema()


@app.post("/api/market/state", response_model=MarketStateSchema)
async def update_market_state(new_state: MarketStateSchema, db: AsyncSession = Depends(get_db)):
    """Manually set all telemetry inputs. Regime is recomputed from the provided values."""
    result = await db.execute(select(MarketStateModel).filter_by(id=1))
    state = result.scalar_one_or_none()
    if not state:
        state = MarketStateModel(id=1)
        db.add(state)

    # Recompute regime from the provided telemetry
    winning_regime, scores = compute_regime(
        spy_price=new_state.spy_price,
        spy_sma20=new_state.spy_sma20,
        vix_close=new_state.vix_close,
        underlying_ivrs=new_state.underlying_ivrs,
        spy_daily_return=new_state.spy_daily_return,
        catalyst_dates=new_state.catalyst_dates,
    )

    state.spy_price = new_state.spy_price
    state.spy_sma20 = new_state.spy_sma20
    state.vix_close = new_state.vix_close
    state.underlying_ivrs = new_state.underlying_ivrs
    state.spy_daily_return = new_state.spy_daily_return
    state.catalyst_dates = new_state.catalyst_dates
    state.current_regime = winning_regime
    state.regime_scores = {k: float(v) for k, v in scores.items()}

    await db.commit()
    await db.refresh(state)
    return state.to_schema()


@app.post("/api/market/fetch", response_model=MarketStateSchema)
async def fetch_live_market_state(db: AsyncSession = Depends(get_db)):
    """
    Triggers a live fetch from the Alpaca API for SPY price/SMA20/return and VIX.
    Recomputes and saves the regime. Returns 503 if credentials are not configured
    or the fetch fails.
    """
    telemetry = fetch_market_telemetry()
    if telemetry is None:
        raise HTTPException(
            status_code=503,
            detail="Live market data unavailable. Check ALPACA_API_KEY_ID and ALPACA_SECRET_KEY environment variables."
        )

    result = await db.execute(select(MarketStateModel).filter_by(id=1))
    state = result.scalar_one_or_none()
    if not state:
        state = MarketStateModel(id=1)
        db.add(state)

    # Preserve existing IVRs and catalyst dates — only SPY/VIX are fetched live
    existing_ivrs = state.underlying_ivrs or {}
    existing_catalysts = state.catalyst_dates or []

    winning_regime, scores = compute_regime(
        spy_price=telemetry["spy_price"],
        spy_sma20=telemetry["spy_sma20"],
        vix_close=telemetry["vix_close"],
        underlying_ivrs=existing_ivrs,
        spy_daily_return=telemetry["spy_daily_return"],
        catalyst_dates=existing_catalysts,
    )

    state.spy_price = telemetry["spy_price"]
    state.spy_sma20 = telemetry["spy_sma20"]
    state.vix_close = telemetry["vix_close"]
    state.spy_daily_return = telemetry["spy_daily_return"]
    state.current_regime = winning_regime
    state.regime_scores = {k: float(v) for k, v in scores.items()}

    await db.commit()
    await db.refresh(state)
    return state.to_schema()

@app.get("/api/portfolio/observation")
async def get_portfolio_observation(db: AsyncSession = Depends(get_db)):
    # 1. Load config
    config_result = await db.execute(select(PortfolioConfigModel).filter_by(id=1))
    config_model = config_result.scalar_one_or_none()
    if not config_model:
        raise HTTPException(status_code=404, detail="Portfolio config not found")
    config = config_model.to_schema()

    # 2. Load positions
    pos_result = await db.execute(select(PositionModel))
    positions = [p.to_schema() for p in pos_result.scalars().all()]

    # 3. Load market state
    state_result = await db.execute(select(MarketStateModel).filter_by(id=1))
    state_model = state_result.scalar_one_or_none()
    if not state_model:
        state = MarketStateSchema(current_regime="CALM_BULL", spy_price=758.0, spy_sma20=750.0, vix_close=14.5, spy_daily_return=0.005, catalyst_dates=["2026-06-08"])
    else:
        state = state_model.to_schema()

    # 4. Perform lifecycle scan on open positions
    open_positions = [p for p in positions if p.status == "OPEN"]
    scanned_positions = []
    for pos in open_positions:
        scan_res = run_lifecycle_scan(
            pos,
            current_regime=state.current_regime,
            spy_price=state.spy_price,
            catalyst_dates=state.catalyst_dates
        )
        scanned_positions.append({
            "position_id": pos.id,
            "underlying": pos.underlying,
            "strategy_type": pos.strategy_type,
            "contracts": pos.contracts,
            "max_loss": pos.max_loss,
            "max_profit": pos.max_profit,
            "entry_premium": pos.entry_premium,
            "current_value_per_share": pos.current_value_per_share,
            "expiration_date": pos.expiration_date,
            "priority": scan_res["priority"],
            "action": scan_res["action"],
            "reason": scan_res["reason"],
            "math_detail": scan_res["math_detail"],
            "legs": [leg.model_dump() for leg in pos.legs]
        })

    # 5. Aggregate Greeks
    greeks = aggregate_portfolio_greeks(positions)

    # 6. Run safeguards
    safeguards = run_exposure_safeguards(positions, config)

    # 7. Check if greeks exceed limits
    greek_warnings = []
    limits = config.portfolio_greek_limits
    if abs(greeks["net_delta"]) > limits.max_net_delta:
        greek_warnings.append({
            "type": "GREEK_LIMIT_DELTA",
            "severity": "CRITICAL",
            "message": f"Portfolio Net Delta limit exceeded: absolute value {abs(greeks['net_delta'])} exceeds limit of {limits.max_net_delta}."
        })
    if abs(greeks["net_vega"]) > limits.max_net_vega:
        greek_warnings.append({
            "type": "GREEK_LIMIT_VEGA",
            "severity": "CRITICAL",
            "message": f"Portfolio Net Vega limit exceeded: absolute value {abs(greeks['net_vega'])} exceeds limit of {limits.max_net_vega}."
        })
    if abs(greeks["net_gamma"]) > limits.max_net_gamma:
        greek_warnings.append({
            "type": "GREEK_LIMIT_GAMMA",
            "severity": "CRITICAL",
            "message": f"Portfolio Net Gamma limit exceeded: absolute value {abs(greeks['net_gamma'])} exceeds limit of {limits.max_net_gamma}."
        })

    return {
        "scanned_positions": scanned_positions,
        "greeks": greeks,
        "safeguards": safeguards + greek_warnings,
        "market_state": state
    }
