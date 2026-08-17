import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from contextlib import asynccontextmanager
from datetime import UTC

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.console import book_summaries, executor_status
from backend.database import get_db, init_db
from backend.market_data import (
    fetch_market_telemetry,
    fetch_options_latest_quotes,
    format_occ_symbol,
)
from backend.models import (
    AuditEventModel,
    AuditEventSchema,
    BenchmarkData,
    BooksView,
    ClosePositionRequest,
    ClosurePostMortemModel,
    ClosurePostMortemSchema,
    ExecutorStatusSchema,
    MarketStateModel,
    MarketStateSchema,
    OpportunityRecordModel,
    OpportunityRecordSchema,
    OpportunityScanResult,
    PerformanceDiagnosticsSchema,
    PlaybookDefinitionModel,
    PlaybookDefinitionSchema,
    PlaybookMetrics,
    PortfolioConfigModel,
    PortfolioConfigSchema,
    PortfolioObservationSchema,
    PositionModel,
    PositionSchema,
    RollPositionRequest,
    TradeSpecResult,
    TradingControlModel,
    TradingControlUpdateRequest,
    TradingControlView,
    UpdateOutcomeRequest,
)
from backend.observation import (
    MAX_ROLLS,
    aggregate_portfolio_greeks,
    derive_roll_candidate,
    run_exposure_safeguards,
    run_lifecycle_scan,
)
from backend.opportunity import generate_trade_spec, scan_opportunities
from backend.regime import compute_regime
from backend.trading_control import GLOBAL_SCOPE, sentinel_halt_active, set_control


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize database and run seeding
    await init_db()
    yield


app = FastAPI(
    title="Options Playbook Automation Engine",
    description="Daily decision-support & automated playbook matching API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS: the only legitimate browser client is the local Vite dev server.
# Override via CORS_ORIGINS (comma-separated) if the frontend ever moves.
_cors_origins = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
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


@app.get("/api/positions", response_model=list[PositionSchema])
async def get_positions(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PositionModel))
    positions = result.scalars().all()
    return [p.to_schema() for p in positions]


@app.post("/api/positions/refresh", response_model=list[PositionSchema])
async def refresh_position_prices(db: AsyncSession = Depends(get_db)):
    """
    Fetch delayed market prices for all open positions from IB Gateway
    and update their current_value_per_share in the database.
    """
    result = await db.execute(select(PositionModel).filter_by(status="OPEN"))
    open_positions = result.scalars().all()
    if not open_positions:
        all_pos_result = await db.execute(select(PositionModel))
        return [p.to_schema() for p in all_pos_result.scalars().all()]

    occ_symbols = []
    for pos in open_positions:
        for leg in pos.legs:
            occ_sym = format_occ_symbol(
                underlying=pos.underlying,
                expiration=leg["expiration"],
                option_type=leg["option_type"],
                strike=leg["strike"],
            )
            occ_symbols.append(occ_sym)

    # Fetch quotes from IB Gateway
    quotes = fetch_options_latest_quotes(occ_symbols)
    if not quotes:
        # If fetch failed or no quotes returned, return existing positions without change
        all_pos_result = await db.execute(select(PositionModel))
        return [p.to_schema() for p in all_pos_result.scalars().all()]

    # Update positions
    for pos in open_positions:
        leg_prices_fetched = True
        long_val = 0.0
        short_val = 0.0

        for leg in pos.legs:
            occ_sym = format_occ_symbol(
                underlying=pos.underlying,
                expiration=leg["expiration"],
                option_type=leg["option_type"],
                strike=leg["strike"],
            )
            if occ_sym in quotes:
                price = quotes[occ_sym]
                if leg["direction"] == "LONG":
                    long_val += price
                else:
                    short_val += price
            else:
                leg_prices_fetched = False
                break

        if leg_prices_fetched:
            if pos.premium_direction == "DEBIT":
                new_val = long_val - short_val
            else:
                new_val = short_val - long_val
            pos.current_value_per_share = round(new_val, 2)

    await db.commit()
    # Refresh all positions to make sure we return clean data
    for pos in open_positions:
        await db.refresh(pos)

    all_pos_result = await db.execute(select(PositionModel))
    return [p.to_schema() for p in all_pos_result.scalars().all()]


@app.get("/api/positions/post-mortems", response_model=list[ClosurePostMortemSchema])
async def get_post_mortems(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ClosurePostMortemModel))
    return [pm.to_schema() for pm in result.scalars().all()]


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
        journal=new_pos.journal.model_dump(),
        warnings_acknowledged=new_pos.warnings_acknowledged,
    )

    db.add(pos_model)
    await db.commit()
    await db.refresh(pos_model)
    return pos_model.to_schema()


# =====================================================================
# Playbook Endpoints
# =====================================================================


@app.get("/api/playbooks", response_model=list[PlaybookDefinitionSchema])
async def get_playbooks(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PlaybookDefinitionModel))
    playbooks = result.scalars().all()
    return [pb.to_schema() for pb in playbooks]


@app.post("/api/playbooks", response_model=PlaybookDefinitionSchema)
async def create_playbook(new_pb: PlaybookDefinitionSchema, db: AsyncSession = Depends(get_db)):
    exists_result = await db.execute(select(PlaybookDefinitionModel).filter_by(id=new_pb.id, version=new_pb.version))
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
        exit_rules=new_pb.exit_rules.model_dump(),
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
            spy_price=758.0,
            spy_sma20=750.0,
            vix_close=14.5,
            underlying_ivrs={"SPY": 25.0},
            spy_daily_return=0.005,
            catalyst_dates=["2026-06-08"],
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
    Triggers a live fetch from IB Gateway for SPY price/SMA20/return and VIX.
    Recomputes and saves the regime. Returns 503 if the Gateway is unreachable
    or the fetch fails.
    """
    telemetry = fetch_market_telemetry()
    if telemetry is None:
        raise HTTPException(
            status_code=503,
            detail="Live market data unavailable. Is IB Gateway running and logged in (paper, port 4002)?",
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


@app.get("/api/portfolio/observation", response_model=PortfolioObservationSchema)
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
        state = MarketStateSchema(
            current_regime="CALM_BULL",
            spy_price=758.0,
            spy_sma20=750.0,
            vix_close=14.5,
            spy_daily_return=0.005,
            catalyst_dates=["2026-06-08"],
        )
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
            catalyst_dates=state.catalyst_dates,
        )
        scanned_positions.append(
            {
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
                "legs": [leg.model_dump() for leg in pos.legs],
                "roll": derive_roll_candidate(pos),
            }
        )

    # 5. Aggregate Greeks
    greeks = aggregate_portfolio_greeks(positions)

    # 6. Run safeguards
    safeguards = run_exposure_safeguards(positions, config)

    # 7. Check if greeks exceed limits
    greek_warnings = []
    limits = config.portfolio_greek_limits
    if abs(greeks["net_delta"]) > limits.max_net_delta:
        greek_warnings.append(
            {
                "type": "GREEK_LIMIT_DELTA",
                "severity": "CRITICAL",
                "message": f"Portfolio Net Delta limit exceeded: absolute value {abs(greeks['net_delta'])} exceeds limit of {limits.max_net_delta}.",
            }
        )
    if abs(greeks["net_vega"]) > limits.max_net_vega:
        greek_warnings.append(
            {
                "type": "GREEK_LIMIT_VEGA",
                "severity": "CRITICAL",
                "message": f"Portfolio Net Vega limit exceeded: absolute value {abs(greeks['net_vega'])} exceeds limit of {limits.max_net_vega}.",
            }
        )
    if abs(greeks["net_gamma"]) > limits.max_net_gamma:
        greek_warnings.append(
            {
                "type": "GREEK_LIMIT_GAMMA",
                "severity": "CRITICAL",
                "message": f"Portfolio Net Gamma limit exceeded: absolute value {abs(greeks['net_gamma'])} exceeds limit of {limits.max_net_gamma}.",
            }
        )

    return {
        "scanned_positions": scanned_positions,
        "greeks": greeks,
        "safeguards": safeguards + greek_warnings,
        "market_state": state,
    }


# =====================================================================
# Layer C: Opportunity Engine Endpoints
# =====================================================================


@app.get("/api/opportunity/scan", response_model=OpportunityScanResult)
async def scan_opportunity(db: AsyncSession = Depends(get_db)):
    """
    Scan all active playbooks against current market telemetry.
    Returns only eligible candidates (ineligible are hidden per spec).
    Portfolio-level gate blocks return an empty list with block_reason set.
    """
    pb_result = await db.execute(select(PlaybookDefinitionModel))
    playbooks = [pb.to_schema() for pb in pb_result.scalars().all()]

    pos_result = await db.execute(select(PositionModel))
    positions = [p.to_schema() for p in pos_result.scalars().all()]

    config_result = await db.execute(select(PortfolioConfigModel).filter_by(id=1))
    config_model = config_result.scalar_one_or_none()
    if not config_model:
        raise HTTPException(status_code=404, detail="Portfolio config not found")
    config = config_model.to_schema()

    state_result = await db.execute(select(MarketStateModel).filter_by(id=1))
    state_model = state_result.scalar_one_or_none()
    if not state_model:
        raise HTTPException(status_code=404, detail="Market state not found")
    state = state_model.to_schema()

    result = scan_opportunities(playbooks, state, positions, config)
    # Return all candidates — frontend separates eligible from suppressed and shows
    # suppression reasons with per-card override capability.
    return result


@app.post("/api/opportunity/spec/{playbook_id}", response_model=TradeSpecResult)
async def get_trade_spec(playbook_id: str, db: AsyncSession = Depends(get_db)):
    """
    Generate a full trade specification for the given playbook.
    Runs hard blocks first — if any fire, spec is null and blocks are returned.
    Warnings require explicit user confirmation but do not suppress the spec.
    """
    pb_result = await db.execute(select(PlaybookDefinitionModel).filter_by(id=playbook_id))
    pb_model = pb_result.scalar_one_or_none()
    if not pb_model:
        raise HTTPException(status_code=404, detail=f"Playbook {playbook_id!r} not found")
    playbook = pb_model.to_schema()

    pos_result = await db.execute(select(PositionModel))
    positions = [p.to_schema() for p in pos_result.scalars().all()]

    config_result = await db.execute(select(PortfolioConfigModel).filter_by(id=1))
    config_model = config_result.scalar_one_or_none()
    if not config_model:
        raise HTTPException(status_code=404, detail="Portfolio config not found")
    config = config_model.to_schema()

    state_result = await db.execute(select(MarketStateModel).filter_by(id=1))
    state_model = state_result.scalar_one_or_none()
    if not state_model:
        raise HTTPException(status_code=404, detail="Market state not found")
    state = state_model.to_schema()

    return generate_trade_spec(playbook, state, positions, config)


# =====================================================================
# Sprint 5: Close Position, Post-Mortems, Opportunity Ledger, Diagnostics
# =====================================================================


@app.post("/api/positions/{position_id}/close", response_model=ClosurePostMortemSchema)
async def close_position(position_id: str, req: ClosePositionRequest, db: AsyncSession = Depends(get_db)):
    import uuid
    from datetime import date as _date

    result = await db.execute(select(PositionModel).filter_by(id=position_id))
    position = result.scalar_one_or_none()
    if not position:
        raise HTTPException(status_code=404, detail="Position not found")
    if position.status != "OPEN":
        raise HTTPException(status_code=400, detail="Position is not OPEN")

    if position.premium_direction == "DEBIT":
        realized_pnl = (req.current_value_per_share - position.entry_premium) * 100 * position.contracts
    else:
        realized_pnl = (position.entry_premium - req.current_value_per_share) * 100 * position.contracts

    realized_pnl = round(realized_pnl, 2)

    if realized_pnl > 0.01:
        outcome = "WIN"
    elif realized_pnl < -0.01:
        outcome = "LOSS"
    else:
        outcome = "BREAKEVEN"

    warnings_ack = position.warnings_acknowledged or []
    user_override_logged = len(warnings_ack) > 0

    pm = ClosurePostMortemModel(
        id=str(uuid.uuid4()),
        position_id=position_id,
        outcome=outcome,
        realized_pnl=realized_pnl,
        actual_underlying_move_pct=req.actual_underlying_move_pct,
        exit_date=str(_date.today()),
        exit_trigger=req.exit_trigger,
        lesson_tags=req.lesson_tags,
        user_override_logged=user_override_logged,
        playbook_id=position.playbook_id,
        playbook_version=position.playbook_version,
    )
    position.status = "CLOSED"

    db.add(pm)
    await db.commit()
    await db.refresh(pm)
    return pm.to_schema()


@app.post("/api/positions/{position_id}/roll", response_model=PositionSchema)
async def roll_position(position_id: str, req: RollPositionRequest, db: AsyncSession = Depends(get_db)):
    """Execute a defensive roll (domain-rules.md): net-credit only, max 2 rolls,
    down-and-out for puts / up-and-out for calls. Debit rolls are blocked —
    the correct action there is taking the loss."""
    import datetime as _dt

    result = await db.execute(select(PositionModel).filter_by(id=position_id))
    position = result.scalar_one_or_none()
    if not position:
        raise HTTPException(status_code=404, detail="Position not found")
    if position.status != "OPEN":
        raise HTTPException(status_code=400, detail="Position is not OPEN")
    if position.premium_direction != "CREDIT" or position.strategy_type not in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"):
        raise HTTPException(status_code=400, detail="NOT_ROLLABLE: only credit verticals roll; close instead")
    if position.rolls >= MAX_ROLLS:
        raise HTTPException(
            status_code=400,
            detail=f"ROLL_CAP_REACHED: {position.rolls} rolls used — forced exit, no exceptions",
        )

    net_credit = round(req.new_credit_per_share - req.close_cost_per_share, 4)
    if net_credit <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"DEBIT_ROLL_BLOCKED: roll nets {net_credit:+.2f}/share — take the loss instead",
        )

    if req.new_expiration <= position.expiration_date:
        raise HTTPException(status_code=400, detail="ROLL_DIRECTION: new expiration must be later (ISO dates)")
    try:
        _dt.date.fromisoformat(req.new_expiration)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="ROLL_DIRECTION: new expiration must be an ISO date") from exc

    old_strikes = sorted(leg["strike"] for leg in position.legs)
    new_strikes = sorted(leg.strike for leg in req.new_legs)
    if position.strategy_type == "BULL_PUT_SPREAD":
        if not all(n < o for n, o in zip(new_strikes, old_strikes, strict=True)):
            raise HTTPException(
                status_code=400, detail="ROLL_DIRECTION: puts roll DOWN — every new strike must be lower"
            )
        if any(leg.option_type != "PUT" for leg in req.new_legs):
            raise HTTPException(status_code=400, detail="ROLL_DIRECTION: a put spread rolls into puts")
    else:
        if not all(n > o for n, o in zip(new_strikes, old_strikes, strict=True)):
            raise HTTPException(
                status_code=400, detail="ROLL_DIRECTION: calls roll UP — every new strike must be higher"
            )
        if any(leg.option_type != "CALL" for leg in req.new_legs):
            raise HTTPException(status_code=400, detail="ROLL_DIRECTION: a call spread rolls into calls")

    # Apply: same position row continues (the rolls counter lives on it).
    # entry_premium becomes the CUMULATIVE net credit collected per share, so
    # the 50%-profit / 2×-loss exit rules keep operating on real economics.
    cumulative_credit = round(position.entry_premium + net_credit, 4)
    width = round(abs(new_strikes[-1] - new_strikes[0]), 4)
    position.legs = [
        {
            "option_type": leg.option_type,
            "direction": leg.direction,
            "strike": leg.strike,
            "expiration": leg.expiration,
            "delta": 0.0,
            "theta": 0.0,
            "vega": 0.0,
            "gamma": 0.0,
        }
        for leg in req.new_legs
    ]
    position.expiration_date = req.new_expiration
    position.entry_premium = cumulative_credit
    position.current_value_per_share = req.new_credit_per_share
    position.max_profit = cumulative_credit
    position.max_loss = round(max(width - cumulative_credit, 0.01), 4)
    position.rolls += 1
    position.notes = (
        f"{position.notes}\nRolled {_dt.date.today().isoformat()}: closed @{req.close_cost_per_share:.2f}, "
        f"reopened @{req.new_credit_per_share:.2f} (net +{net_credit:.2f}), roll {position.rolls}/{MAX_ROLLS}"
    ).strip()

    await db.commit()
    await db.refresh(position)
    return position.to_schema()


@app.get("/api/positions/{position_id}/post-mortem", response_model=ClosurePostMortemSchema)
async def get_post_mortem(position_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ClosurePostMortemModel).filter_by(position_id=position_id))
    pm = result.scalar_one_or_none()
    if not pm:
        raise HTTPException(status_code=404, detail="Post-mortem not found for this position")
    return pm.to_schema()


@app.get("/api/opportunity/ledger", response_model=list[OpportunityRecordSchema])
async def get_opportunity_ledger(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(OpportunityRecordModel))
    return [r.to_schema() for r in result.scalars().all()]


@app.post("/api/opportunity/ledger", response_model=OpportunityRecordSchema)
async def create_opportunity_record(record: OpportunityRecordSchema, db: AsyncSession = Depends(get_db)):
    import uuid

    model = OpportunityRecordModel(
        id=str(uuid.uuid4()),
        playbook_id=record.playbook_id,
        playbook_version=record.playbook_version,
        generated_at=record.generated_at,
        accepted=record.accepted,
        outcome_if_taken=record.outcome_if_taken,
        bypass_reason=record.bypass_reason,
    )
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model.to_schema()


@app.patch("/api/opportunity/ledger/{record_id}", response_model=OpportunityRecordSchema)
async def update_opportunity_outcome(record_id: str, req: UpdateOutcomeRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(OpportunityRecordModel).filter_by(id=record_id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Opportunity record not found")
    record.outcome_if_taken = req.outcome_if_taken
    await db.commit()
    await db.refresh(record)
    return record.to_schema()


@app.get("/api/performance/diagnostics", response_model=PerformanceDiagnosticsSchema)
async def get_performance_diagnostics(db: AsyncSession = Depends(get_db)):
    from collections import defaultdict
    from datetime import datetime

    pm_result = await db.execute(select(ClosurePostMortemModel))
    post_mortems = pm_result.scalars().all()

    pos_result = await db.execute(select(PositionModel))
    positions_by_id = {p.id: p for p in pos_result.scalars().all()}

    groups: dict[tuple[str, str], list] = defaultdict(list)
    for pm in post_mortems:
        pb_id = pm.playbook_id or "MANUAL_TRADE"
        pb_ver = pm.playbook_version or "N/A"
        groups[(pb_id, pb_ver)].append(pm)

    playbook_metrics = []
    for (pb_id, pb_ver), pms in groups.items():
        total = len(pms)
        wins = sum(1 for pm in pms if pm.outcome == "WIN")
        win_rate = wins / total if total > 0 else None

        total_profit = sum(pm.realized_pnl for pm in pms if pm.realized_pnl > 0)
        total_loss = abs(sum(pm.realized_pnl for pm in pms if pm.realized_pnl < 0))
        profit_factor = (total_profit / total_loss) if total_loss > 0 else None

        returns_on_risk = []
        for pm in pms:
            pos = positions_by_id.get(pm.position_id)
            if pos and pos.max_loss and pos.max_loss > 0:
                returns_on_risk.append(pm.realized_pnl / pos.max_loss)
        avg_ror = sum(returns_on_risk) / len(returns_on_risk) if returns_on_risk else None

        playbook_metrics.append(
            PlaybookMetrics(
                playbook_id=pb_id,
                playbook_version=pb_ver,
                total_trades=total,
                win_rate=win_rate,
                profit_factor=profit_factor,
                avg_return_on_risk=avg_ror,
            )
        )

    return PerformanceDiagnosticsSchema(
        generated_at=datetime.now(UTC).isoformat(),
        playbook_metrics=playbook_metrics,
        benchmarks=BenchmarkData(),
    )


# =====================================================================
# Trading Control (kill switch) Endpoints — ADR-0008, spec/supervision.md
# =====================================================================


@app.get("/api/trading-control", response_model=TradingControlView)
async def get_trading_control(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(TradingControlModel))).scalars().all()
    return TradingControlView(
        controls=[r.to_schema() for r in rows],
        sentinel_halt=sentinel_halt_active(),
    )


@app.post("/api/trading-control", response_model=TradingControlView)
async def update_trading_control(request: TradingControlUpdateRequest, db: AsyncSession = Depends(get_db)):
    """The console control surface — the ONLY place RESUME exists (ADR-0008)."""
    if request.scope != GLOBAL_SCOPE:
        from backend.models import BookModel

        if await db.get(BookModel, request.scope) is None:
            raise HTTPException(status_code=404, detail=f"Unknown control scope {request.scope!r}")
    if request.state == "ACTIVE" and sentinel_halt_active():
        raise HTTPException(
            status_code=409,
            detail="Sentinel HALT file present — remove it before resuming (it overrides the database state).",
        )
    await set_control(db, request.scope, request.state, reason=request.reason, actor="console", allow_resume=True)
    rows = (await db.execute(select(TradingControlModel))).scalars().all()
    return TradingControlView(controls=[r.to_schema() for r in rows], sentinel_halt=sentinel_halt_active())


# =====================================================================
# Supervision Console Endpoints — design §6.5 (#73)
# =====================================================================


@app.get("/api/books", response_model=BooksView)
async def get_books(db: AsyncSession = Depends(get_db)):
    """Per-book summaries with the ADR-0006 Live Gate checklist (Books tab)."""
    return BooksView(books=await book_summaries(db))


@app.get("/api/audit-events", response_model=list[AuditEventSchema])
async def get_audit_events(
    book_id: str | None = None,
    date: str | None = None,
    event_type: str | None = None,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
):
    """The append-only audit trail, newest first. `date` prefix-matches run_at
    (e.g. '2026-08' for a month, '2026-08-18' for a day)."""
    query = select(AuditEventModel)
    if book_id:
        query = query.filter(AuditEventModel.book_id == book_id)
    if date:
        query = query.filter(AuditEventModel.run_at.startswith(date))
    if event_type:
        query = query.filter(AuditEventModel.event_type == event_type)
    query = query.order_by(AuditEventModel.id.desc()).limit(min(max(limit, 1), 1000))
    rows = (await db.execute(query)).scalars().all()
    return [
        AuditEventSchema(
            id=r.id, run_at=r.run_at, book_id=r.book_id, event_type=r.event_type, actor=r.actor, payload=r.payload
        )
        for r in rows
    ]


@app.get("/api/executor/status", response_model=ExecutorStatusSchema)
async def get_executor_status(db: AsyncSession = Depends(get_db)):
    """Heartbeat + last reconciliation for the status strip."""
    return await executor_status(db)
