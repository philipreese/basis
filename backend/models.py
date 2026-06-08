from typing import List, Optional, Literal, Tuple
from pydantic import BaseModel, Field
from sqlalchemy import Column, JSON, String, Float, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

# =====================================================================
# Pydantic Schemas (for Validation & API Serialization)
# =====================================================================

class EntryFilters(BaseModel):
    min_ivr: float
    max_ivr: float
    vix_range: Tuple[float, float]
    required_trend: Literal['ABOVE_SMA20', 'BELOW_SMA20', 'ANY']
    block_catalyst_14dte: bool
    require_catalyst_14dte: bool

class ExecutionSpecs(BaseModel):
    target_dte: int
    short_leg_delta: float
    long_leg_delta: float
    spread_width_dollars: float
    straddle_atm: bool

class ExitRules(BaseModel):
    profit_take_pct: float
    stop_loss_pct: float
    mandatory_exit_dte: int
    catalyst_exit_days_after: int

class PlaybookDefinitionSchema(BaseModel):
    id: str
    version: str
    name: str
    underlying_ticker: str
    strategy_type: Literal['BULL_CALL_SPREAD', 'BEAR_PUT_SPREAD', 'IRON_CONDOR', 'LONG_STRADDLE', 'LONG_STRANGLE']
    execution_mode: Literal['LIVE', 'PAPER']
    entry_filters: EntryFilters
    execution_specs: ExecutionSpecs
    exit_rules: ExitRules

class OptionLegSchema(BaseModel):
    option_type: Literal['CALL', 'PUT']
    direction: Literal['LONG', 'SHORT']
    strike: float
    expiration: str  # ISO date: "2026-07-18"
    delta: float
    theta: float
    vega: float
    gamma: float = 0.0

class OperationalJournalEntrySchema(BaseModel):
    core_thesis_rationale: str
    structural_invalidation: str
    expected_underlying_move_pct: float
    pre_trade_emotional_state: Literal['Calm', 'Anxious', 'Chasing', 'Bored']
    pre_trade_confidence_rating: Literal[1, 2, 3, 4, 5]

class PositionSchema(BaseModel):
    id: str
    underlying: str
    strategy_type: Literal['BULL_CALL_SPREAD', 'BEAR_PUT_SPREAD', 'IRON_CONDOR', 'LONG_STRADDLE', 'LONG_STRANGLE']
    execution_mode: Literal['LIVE', 'PAPER']
    legs: List[OptionLegSchema]
    entry_date: str
    expiration_date: str
    entry_premium: float
    premium_direction: Literal['CREDIT', 'DEBIT']
    current_value_per_share: float
    contracts: int
    max_profit: float
    max_loss: float
    profit_target_per_share: Optional[float] = None
    loss_limit_per_share: Optional[float] = None
    break_even_upside: Optional[float] = None
    break_even_downside: Optional[float] = None
    notes: str
    rolls: int = 0
    status: Literal['OPEN', 'CLOSED', 'EXPIRED']
    playbook_id: Optional[str] = None
    playbook_version: Optional[str] = None
    playbook_snapshot: Optional[PlaybookDefinitionSchema] = None
    journal: Optional[OperationalJournalEntrySchema] = None

class AccountConfig(BaseModel):
    total_nav: float
    broker: str
    account_type: str
    options_approval: str
    execution_mode: Literal['LIVE', 'PAPER']

class RiskProfile(BaseModel):
    max_trade_risk_pct: float
    max_trade_risk_dollars: float
    max_underlying_concentration_pct: float
    max_correlated_index_pct: float
    minimum_cash_reserve_pct: float
    max_simultaneous_positions: int
    max_capital_deployed_pct: float

class PortfolioGreekLimits(BaseModel):
    max_net_delta: float
    max_net_vega: float
    max_net_gamma: float

class PortfolioConfigSchema(BaseModel):
    account: AccountConfig
    risk_profile: RiskProfile
    portfolio_greek_limits: PortfolioGreekLimits


# =====================================================================
# SQLAlchemy Models (for Database Persistence)
# =====================================================================

class PlaybookDefinitionModel(Base):
    __tablename__ = 'playbooks'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    underlying_ticker: Mapped[str] = mapped_column(String)
    strategy_type: Mapped[str] = mapped_column(String)
    execution_mode: Mapped[str] = mapped_column(String)
    # Store nested schemas as JSON columns
    entry_filters: Mapped[dict] = mapped_column(JSON)
    execution_specs: Mapped[dict] = mapped_column(JSON)
    exit_rules: Mapped[dict] = mapped_column(JSON)

    def to_schema(self) -> PlaybookDefinitionSchema:
        return PlaybookDefinitionSchema(
            id=self.id,
            version=self.version,
            name=self.name,
            underlying_ticker=self.underlying_ticker,
            strategy_type=self.strategy_type,  # type: ignore
            execution_mode=self.execution_mode,  # type: ignore
            entry_filters=EntryFilters(**self.entry_filters),
            execution_specs=ExecutionSpecs(**self.execution_specs),
            exit_rules=ExitRules(**self.exit_rules),
        )


class PositionModel(Base):
    __tablename__ = 'positions'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    underlying: Mapped[str] = mapped_column(String)
    strategy_type: Mapped[str] = mapped_column(String)
    execution_mode: Mapped[str] = mapped_column(String)
    legs: Mapped[list] = mapped_column(JSON)  # List of OptionLegSchema
    entry_date: Mapped[str] = mapped_column(String)
    expiration_date: Mapped[str] = mapped_column(String)
    entry_premium: Mapped[float] = mapped_column(Float)
    premium_direction: Mapped[str] = mapped_column(String)
    current_value_per_share: Mapped[float] = mapped_column(Float)
    contracts: Mapped[int] = mapped_column(Integer)
    max_profit: Mapped[float] = mapped_column(Float)
    max_loss: Mapped[float] = mapped_column(Float)
    profit_target_per_share: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    loss_limit_per_share: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    break_even_upside: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    break_even_downside: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(String)
    rolls: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String)
    playbook_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    playbook_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    playbook_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    journal: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    def to_schema(self) -> PositionSchema:
        return PositionSchema(
            id=self.id,
            underlying=self.underlying,
            strategy_type=self.strategy_type,  # type: ignore
            execution_mode=self.execution_mode,  # type: ignore
            legs=[OptionLegSchema(**leg) for leg in self.legs],
            entry_date=self.entry_date,
            expiration_date=self.expiration_date,
            entry_premium=self.entry_premium,
            premium_direction=self.premium_direction,  # type: ignore
            current_value_per_share=self.current_value_per_share,
            contracts=self.contracts,
            max_profit=self.max_profit,
            max_loss=self.max_loss,
            profit_target_per_share=self.profit_target_per_share,
            loss_limit_per_share=self.loss_limit_per_share,
            break_even_upside=self.break_even_upside,
            break_even_downside=self.break_even_downside,
            notes=self.notes,
            rolls=self.rolls,
            status=self.status,  # type: ignore
            playbook_id=self.playbook_id,
            playbook_version=self.playbook_version,
            playbook_snapshot=PlaybookDefinitionSchema(**self.playbook_snapshot) if self.playbook_snapshot else None,
            journal=OperationalJournalEntrySchema(**self.journal) if self.journal else None,
        )


class PortfolioConfigModel(Base):
    __tablename__ = 'portfolio_config'

    id: Mapped[int] = mapped_column(primary_key=True, default=1)  # Only 1 config record
    account: Mapped[dict] = mapped_column(JSON)
    risk_profile: Mapped[dict] = mapped_column(JSON)
    portfolio_greek_limits: Mapped[dict] = mapped_column(JSON)

    def to_schema(self) -> PortfolioConfigSchema:
        return PortfolioConfigSchema(
            account=AccountConfig(**self.account),
            risk_profile=RiskProfile(**self.risk_profile),
            portfolio_greek_limits=PortfolioGreekLimits(**self.portfolio_greek_limits),
        )


class MarketStateSchema(BaseModel):
    current_regime: Literal['CALM_BULL', 'HIGH_VOL_NEUTRAL', 'TRENDING_BEAR', 'EVENT_CATALYST']
    spy_price: float
    catalyst_dates: List[str] = Field(default_factory=list)


class MarketStateModel(Base):
    __tablename__ = 'market_state'

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    current_regime: Mapped[str] = mapped_column(String)
    spy_price: Mapped[float] = mapped_column(Float)
    catalyst_dates: Mapped[list] = mapped_column(JSON)

    def to_schema(self) -> MarketStateSchema:
        return MarketStateSchema(
            current_regime=self.current_regime,  # type: ignore
            spy_price=self.spy_price,
            catalyst_dates=self.catalyst_dates
        )
