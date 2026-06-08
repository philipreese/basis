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
    PlaybookDefinitionSchema, PlaybookDefinitionModel
)

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
