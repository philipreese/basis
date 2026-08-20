import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from contextlib import asynccontextmanager
from datetime import UTC, date

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.catalyst_calendar import merge_catalysts
from backend.console import book_summaries, executor_status
from backend.database import get_db, init_db
from backend.digest import is_urgent_event_type
from backend.market_data import (
    fetch_market_telemetry,
)
from backend.models import (
    AuditEventModel,
    AuditEventSchema,
    BooksView,
    CashAdjustmentRequest,
    CashAdjustmentResult,
    ClosePositionRequest,
    ClosurePostMortemModel,
    ClosurePostMortemSchema,
    ExecutorStatusSchema,
    ExternalCloseRequest,
    FillQualityReport,
    IndexHistoryModel,
    LeaderboardReport,
    MarketStateModel,
    MarketStateSchema,
    OpportunityRecordModel,
    OpportunityRecordSchema,
    OpportunityScanResult,
    PartialOrderResolveRequest,
    PartialOrderResolveResult,
    PerformanceDiagnosticsSchema,
    PlaybookDefinitionModel,
    PlaybookDefinitionSchema,
    PortfolioConfigModel,
    PortfolioConfigSchema,
    PortfolioObservationSchema,
    PositionModel,
    PositionSchema,
    ReconciliationRunModel,
    ReconciliationRunSchema,
    RegimeHitRateReport,
    ResolveRunRequest,
    RollPositionRequest,
    TradeSpecResult,
    TradingControlModel,
    TradingControlUpdateRequest,
    TradingControlView,
    UpdateOutcomeRequest,
)
from backend.observation import RollError, apply_roll, compose_observation
from backend.operator import refresh_position_values
from backend.opportunity import generate_trade_spec, scan_opportunities
from backend.performance import compose_diagnostics
from backend.regime import catalyst_near_miss, compute_regime
from backend.trading_control import GLOBAL_SCOPE, sentinel_halt_active, set_control


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize database and run seeding
    await init_db()
    yield


app = FastAPI(
    title="basis",
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
    """Reprice open positions from IB Gateway quotes (the operator's nightly
    repricer, shared) and return the full position list."""
    await refresh_position_values(db)
    result = await db.execute(select(PositionModel))
    return [p.to_schema() for p in result.scalars().all()]


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
    # Manual mint restriction (#481 F12): executor books are machine-written
    # — a hand-minted OPEN position there manufactures reconciliation drift
    # and occupies gate slots; a hand-minted CLOSED one has no post-mortem
    # and skews every win-rate denominator. Manual journaling is B00, OPEN.
    if (new_pos.book_id or "B00") != "B00":
        raise HTTPException(
            status_code=400,
            detail="Manual positions are B00-only — executor book ledgers are machine-written; "
            "use the resolution panel to correct them",
        )
    if new_pos.status != "OPEN":
        raise HTTPException(
            status_code=400,
            detail="Manual positions enter OPEN — record an exit through the close endpoint so the "
            "post-mortem (and win-rate evidence) is written",
        )

    pos_model = PositionModel(
        id=new_pos.id,
        underlying=new_pos.underlying,
        strategy_type=new_pos.strategy_type,
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
        book_id=new_pos.book_id,
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
    # Catalyst near-miss guard (#354): 'AAPL:2026-10-29' would save cleanly
    # as a MARKET-WIDE catalyst and blackout every book for 14 days.
    problems = [msg for entry in new_state.catalyst_dates if (msg := catalyst_near_miss(entry))]
    if problems:
        raise HTTPException(status_code=400, detail="; ".join(problems))
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

    # Preserve existing IVRs; catalyst dates merge in the seeded FOMC/CPI
    # calendar additively (#131) — manual entries survive, past ones prune.
    existing_ivrs = state.underlying_ivrs or {}
    existing_catalysts = merge_catalysts(state.catalyst_dates or [], date.today())

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

    # 3. Load market state — never fabricated: a made-up regime and price
    # would render an observation that looks real (#180). init_db seeds the
    # row, so absence means the database was never initialized.
    state_result = await db.execute(select(MarketStateModel).filter_by(id=1))
    state_model = state_result.scalar_one_or_none()
    if not state_model:
        raise HTTPException(
            status_code=404, detail="Market state not found — initialize the database (init_db seeds it)"
        )

    # 4. Compose (lifecycle scan, greeks, safeguards, greek limits)
    return compose_observation(config, positions, state_model.to_schema())


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

    from backend.dates import market_today
    from backend.resolution import ResolutionError, terminalize_live_orders_or_refuse, validate_exit_value_per_share

    result = await db.execute(select(PositionModel).filter_by(id=position_id))
    position = result.scalar_one_or_none()
    if not position:
        raise HTTPException(status_code=404, detail="Position not found")
    if position.status != "OPEN":
        raise HTTPException(status_code=400, detail="Position is not OPEN")
    # Executor-managed positions have REAL legs resting at the broker; this
    # endpoint never touches the broker (#279, audit H6). Closing one here
    # guarantees reconciliation drift and a global halt tonight.
    if position.book_id != "B00" and not req.acknowledge_broker_divergence:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Position {position_id} belongs to executor book {position.book_id}: its legs are real at the "
                "broker, and this endpoint is bookkeeping-only. Closing it here WILL cause reconciliation drift "
                "and a global entry halt tonight. The executor closes its own positions; if the position was "
                "already closed AT the broker, use /api/resolution/external-close (the audited correction "
                "path). To force this endpoint anyway, resend with acknowledge_broker_divergence=true."
            ),
        )

    # #468: parity with record_external_close's guards — this endpoint is a
    # cash writer too (#412) and had neither. A NaN/inf current_value_per_share
    # would poison cash_balance permanently (#346); a resting STAGED/SUBMITTED
    # order (most commonly a GTC profit-taker) left alone here strands SUBMITTED
    # forever — the sync sees it OPEN and waits, and Layer A only iterates OPEN
    # positions so it never runs the cancel-first step. A future fill would
    # re-sell a position the books already call CLOSED.
    reason = f"console close (exit_trigger={req.exit_trigger})"
    try:
        validate_exit_value_per_share(req.current_value_per_share)
        await terminalize_live_orders_or_refuse(db, position_id, reason, req.acknowledge_cancelled)
    except ResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        # #468: siblings (record_external_close, executor closes) all stamp
        # the MARKET date, not the machine-local wall-clock date — a UTC
        # midnight rollover shifted this endpoint's post-mortems a day off
        # from every other close's evidence.
        exit_date=market_today().isoformat(),
        exit_trigger=req.exit_trigger,
        lesson_tags=req.lesson_tags,
        user_override_logged=user_override_logged,
        playbook_id=position.playbook_id,
        playbook_version=position.playbook_version,
    )
    # Conditional transition (#463, Audit II R3 F3): the OPEN check above the
    # broker-divergence guard is a plain SELECT — a double-submitted close
    # (two tabs, a retried request) can both pass it. This UPDATE is the
    # real guard: it only flips rows still OPEN, so the loser's WHERE
    # matches zero rows once the winner has committed, and the loser gets a
    # 409 instead of double-booking cash and a duplicate post-mortem.
    result = await db.execute(
        update(PositionModel)
        .where(PositionModel.id == position.id, PositionModel.status == "OPEN")
        .values(status="CLOSED")
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="Position was closed concurrently")
    position.status = "CLOSED"
    # Book the exit like every other close path (#412): before this, the
    # console close set CLOSED and realized P&L but moved NO cash and left
    # the mark at its last repriced value — console P&L diverged from the
    # book ledger and the post-mortem. Same signed convention as the
    # executor and record_external_close: buying back a credit COSTS the
    # exit value; selling a debit RECEIVES it.
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from backend.book_gates import credit_book_cash

    position.current_value_per_share = req.current_value_per_share
    position.last_priced_at = _datetime.now(_UTC).isoformat()
    flow = req.current_value_per_share if position.premium_direction == "DEBIT" else -req.current_value_per_share
    await credit_book_cash(db, position.book_id, flow * 100 * position.contracts)

    db.add(pm)
    # #468: every other cash mutator (executor closes, resolution) writes an
    # audit_events row — this endpoint moved cash invisibly.
    db.add(
        AuditEventModel(
            run_at=_datetime.now(_UTC).isoformat(),
            book_id=position.book_id,
            event_type="POSITION_CLOSED_CONSOLE",
            actor="console",
            payload={
                "position_id": position_id,
                "current_value_per_share": req.current_value_per_share,
                "realized_pnl": realized_pnl,
                "exit_trigger": req.exit_trigger,
            },
        )
    )
    await db.commit()
    await db.refresh(pm)
    return pm.to_schema()


@app.post("/api/positions/{position_id}/roll", response_model=PositionSchema)
async def roll_position(position_id: str, req: RollPositionRequest, db: AsyncSession = Depends(get_db)):
    """Execute a defensive roll (domain-rules.md): net-credit only, max 2 rolls,
    down-and-out for puts / up-and-out for calls. Debit rolls are blocked —
    the correct action there is taking the loss."""
    result = await db.execute(select(PositionModel).filter_by(id=position_id))
    position = result.scalar_one_or_none()
    if not position:
        raise HTTPException(status_code=404, detail="Position not found")
    try:
        apply_roll(position, req)
    except RollError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    from datetime import datetime

    post_mortems = list((await db.execute(select(ClosurePostMortemModel))).scalars().all())
    positions_by_id = {p.id: p for p in (await db.execute(select(PositionModel))).scalars().all()}
    spy_rows = (
        (await db.execute(select(IndexHistoryModel).filter_by(symbol="SPY").order_by(IndexHistoryModel.date)))
        .scalars()
        .all()
    )
    return compose_diagnostics(
        post_mortems,
        positions_by_id,
        [(r.date, r.close) for r in spy_rows],
        generated_at=datetime.now(UTC).isoformat(),
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
    (e.g. '2026-08' for a month, '2026-08-18' for a day). `event_type`
    substring-matches, case-insensitively (#479) — exact match made "reject"
    silently return nothing instead of REJECTED/ORDER_REJECTED/etc, which
    read as "no such events" rather than "wrong filter"."""
    query = select(AuditEventModel)
    if book_id:
        query = query.filter(AuditEventModel.book_id == book_id)
    if date:
        query = query.filter(AuditEventModel.run_at.startswith(date))
    if event_type:
        query = query.filter(AuditEventModel.event_type.ilike(f"%{event_type}%"))
    query = query.order_by(AuditEventModel.id.desc()).limit(min(max(limit, 1), 1000))
    rows = (await db.execute(query)).scalars().all()
    return [
        AuditEventSchema(
            id=r.id,
            run_at=r.run_at,
            book_id=r.book_id,
            event_type=r.event_type,
            actor=r.actor,
            payload=r.payload,
            urgent=is_urgent_event_type(r.event_type),
        )
        for r in rows
    ]


@app.get("/api/executor/status", response_model=ExecutorStatusSchema)
async def get_executor_status(db: AsyncSession = Depends(get_db)):
    """Heartbeat + last reconciliation for the status strip."""
    return await executor_status(db)


# ---------------------------------------------------------------------------
# Reconciliation resolution (#310) — audited corrections, never hand SQL
# ---------------------------------------------------------------------------


@app.get("/api/reconciliation/latest", response_model=ReconciliationRunSchema)
async def get_latest_reconciliation(db: AsyncSession = Depends(get_db)):
    from backend.reconciliation import latest_reconciliation_run

    # Prefer the newest UNRESOLVED drift run over a merely more recent CLEAN
    # snapshot (#474): a GHOST_ORDER halt from last night's DRIFT must stay
    # visible even when tonight's recon happens to read CLEAN — the halt
    # itself only clears on an explicit human RESUME (ADR-0008), so hiding
    # the unresolved run behind a later CLEAN one leaves no way to learn why
    # the system is still halted. Shared with executor_status() (#478) so
    # the strip badge and this panel can't disagree about "the latest run".
    run = await latest_reconciliation_run(db)
    if run is None:
        raise HTTPException(status_code=404, detail="No reconciliation run yet")
    return ReconciliationRunSchema(
        id=run.id,
        run_at=run.run_at,
        result=run.result,
        drift_details=run.drift_details,
        resolved_at=run.resolved_at,
        resolution=run.resolution,
    )


@app.post("/api/reconciliation/{run_id}/resolve", response_model=ReconciliationRunSchema)
async def resolve_reconciliation_run(run_id: int, req: ResolveRunRequest, db: AsyncSession = Depends(get_db)):
    """Record the human explanation on a drift run. Never auto-resumes (ADR-0008)."""
    from backend.reconciliation import resolve_reconciliation

    if len(req.resolution.strip()) < 3:
        raise HTTPException(status_code=400, detail="A resolution requires a reason (min 3 characters)")
    try:
        await resolve_reconciliation(db, run_id, req.resolution.strip())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    run = await db.get(ReconciliationRunModel, run_id)
    return ReconciliationRunSchema(
        id=run.id,
        run_at=run.run_at,
        result=run.result,
        drift_details=run.drift_details,
        resolved_at=run.resolved_at,
        resolution=run.resolution,
    )


@app.post("/api/resolution/external-close", response_model=ClosurePostMortemSchema)
async def resolution_external_close(req: ExternalCloseRequest, db: AsyncSession = Depends(get_db)):
    """'This position was closed at the broker' — CLOSED at the stated value,
    cash moved, MANUAL post-mortem, everything audited as actor=resolution."""
    from backend.resolution import ResolutionError, record_external_close

    try:
        pm = await record_external_close(
            db, req.position_id, req.exit_value_per_share, req.reason, req.acknowledge_cancelled
        )
    except ResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return pm.to_schema()


@app.post("/api/resolution/partial-order", response_model=PartialOrderResolveResult)
async def resolution_partial_order(req: PartialOrderResolveRequest, db: AsyncSession = Depends(get_db)):
    """Terminalize a PARTIAL order row (#414) — releases its encumbrance and
    slot. Record the partial's cash/position consequences FIRST (external
    close / cash adjust); this only clears the latch."""
    from backend.resolution import ResolutionError, resolve_partial_order

    try:
        status = await resolve_partial_order(db, req.order_ref, req.reason)
    except ResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # The row's actual terminal status (#479), not a hardcoded guess.
    return PartialOrderResolveResult(order_ref=req.order_ref, status=status)


@app.post("/api/resolution/cash", response_model=CashAdjustmentResult)
async def resolution_cash_adjustment(req: CashAdjustmentRequest, db: AsyncSession = Depends(get_db)):
    """A signed cash correction with a mandatory reason (audited)."""
    from backend.resolution import ResolutionError, adjust_book_cash

    try:
        balance = await adjust_book_cash(db, req.book_id, req.delta, req.reason)
    except ResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CashAdjustmentResult(book_id=req.book_id, cash_balance=balance)


# ---------------------------------------------------------------------------
# Analysis tab read models (#242-#244)
# ---------------------------------------------------------------------------


@app.get("/api/analysis/fill-quality", response_model=FillQualityReport)
async def get_fill_quality(db: AsyncSession = Depends(get_db)):
    """Measured slippage vs the decided mid, decomposed into ladder
    concession and market movement, against the $5/contract haircut."""
    from backend.analysis import fill_quality_report

    return await fill_quality_report(db)


@app.get("/api/analysis/leaderboard", response_model=LeaderboardReport)
async def get_leaderboard(db: AsyncSession = Depends(get_db)):
    """Books ranked by expectancy after haircut, plus the knob sweeps —
    verdicts only speak once every point has a minimum sample."""
    from backend.analysis import leaderboard_report

    return await leaderboard_report(db)


@app.get("/api/analysis/regime-hit-rate", response_model=RegimeHitRateReport)
async def get_regime_hit_rate(db: AsyncSession = Depends(get_db)):
    """Entry-day regime vs closed outcome — the observational complement to
    the B12 no-gate control arm."""
    from backend.analysis import regime_hit_rate_report

    return await regime_hit_rate_report(db)
