from typing import List, Optional, Literal, Tuple, Dict
from pydantic import BaseModel, Field
from sqlalchemy import Column, JSON, String, Float, Integer, Boolean
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
    strategy_type: Literal['BULL_CALL_SPREAD', 'BEAR_PUT_SPREAD', 'BULL_PUT_SPREAD', 'BEAR_CALL_SPREAD', 'IRON_CONDOR', 'LONG_STRADDLE', 'LONG_STRANGLE']
    execution_mode: Literal['LIVE', 'PAPER']
    enabled: bool = True
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
    strategy_type: Literal['BULL_CALL_SPREAD', 'BEAR_PUT_SPREAD', 'BULL_PUT_SPREAD', 'BEAR_CALL_SPREAD', 'IRON_CONDOR', 'LONG_STRADDLE', 'LONG_STRANGLE']
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
    journal: OperationalJournalEntrySchema
    warnings_acknowledged: List[str] = Field(default_factory=list)

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
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
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
            enabled=self.enabled if self.enabled is not None else True,
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
    warnings_acknowledged: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

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
            journal=OperationalJournalEntrySchema(**self.journal),  # type: ignore[arg-type]
            warnings_acknowledged=self.warnings_acknowledged or [],
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
    spy_sma20: float = 0.0
    vix_close: float = 0.0
    underlying_ivrs: Dict[str, float] = Field(default_factory=dict)
    spy_daily_return: float = 0.0
    catalyst_dates: List[str] = Field(default_factory=list)
    regime_scores: Dict[str, float] = Field(default_factory=dict)


class MarketStateModel(Base):
    __tablename__ = 'market_state'

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    current_regime: Mapped[str] = mapped_column(String)
    spy_price: Mapped[float] = mapped_column(Float)
    spy_sma20: Mapped[float] = mapped_column(Float, default=0.0)
    vix_close: Mapped[float] = mapped_column(Float, default=0.0)
    underlying_ivrs: Mapped[dict] = mapped_column(JSON, default=dict)
    spy_daily_return: Mapped[float] = mapped_column(Float, default=0.0)
    catalyst_dates: Mapped[list] = mapped_column(JSON, default=list)
    regime_scores: Mapped[dict] = mapped_column(JSON, default=dict)

    def to_schema(self) -> MarketStateSchema:
        return MarketStateSchema(
            current_regime=self.current_regime,  # type: ignore
            spy_price=self.spy_price,
            spy_sma20=self.spy_sma20,
            vix_close=self.vix_close,
            underlying_ivrs=self.underlying_ivrs,
            spy_daily_return=self.spy_daily_return,
            catalyst_dates=self.catalyst_dates,
            regime_scores=self.regime_scores
        )


# =====================================================================
# Sprint 4: Layer C — Opportunity Engine Schemas
# =====================================================================

class StrikeDerivedParams(BaseModel):
    """Documents how strikes were derived so every output is traceable."""
    underlying: str
    current_price: float
    target_dte: int
    short_leg_delta: Optional[float] = None
    long_leg_delta: Optional[float] = None
    spread_width_dollars: Optional[float] = None
    one_sigma_move: Optional[float] = None  # derived from VIX
    derivation_note: str


class CandidateCard(BaseModel):
    playbook: PlaybookDefinitionSchema
    eligible: bool
    suppressed_reason: Optional[str] = None
    strike_params: Optional[StrikeDerivedParams] = None


class OpportunityScanResult(BaseModel):
    portfolio_blocked: bool
    block_reason: Optional[str] = None
    candidates: List[CandidateCard]


class TradeSpecLeg(BaseModel):
    action: Literal['BUY', 'SELL']
    option_type: Literal['CALL', 'PUT']
    strike: float
    expiration_date: str
    quantity: int
    delta_target: Optional[float] = None


class TradeSpec(BaseModel):
    playbook_id: str
    playbook_name: str
    underlying: str
    strategy_type: str
    legs: List[TradeSpecLeg]
    expiration_date: str
    dte_at_entry: int
    order_type: Literal['LIMIT'] = 'LIMIT'
    limit_price_per_share: float
    max_loss_dollars: float
    max_gain_dollars: Optional[float] = None  # None = unlimited
    max_gain_note: str
    break_even_prices: List[float]
    profit_target_dollars: float
    profit_target_pct: float
    loss_limit_dollars: float
    loss_limit_pct: float
    closing_order_instructions: str
    derivation_params: StrikeDerivedParams


class HardBlock(BaseModel):
    check: str
    reason: str


class TradeWarning(BaseModel):
    check: str
    message: str


class TradeSpecResult(BaseModel):
    hard_blocks: List[HardBlock]
    warnings: List[TradeWarning]
    spec: Optional[TradeSpec] = None


# =====================================================================
# Sprint 5: Post-Mortem, Opportunity Ledger, Performance Diagnostics
# =====================================================================

class ClosePositionRequest(BaseModel):
    current_value_per_share: float
    exit_trigger: Literal['PROFIT_TARGET', 'LOSS_LIMIT', 'TIME_RULE', 'CATALYST_RULE', 'MANUAL']
    actual_underlying_move_pct: float
    lesson_tags: List[str] = Field(default_factory=list)


class ClosurePostMortemSchema(BaseModel):
    id: str
    position_id: str
    outcome: Literal['WIN', 'LOSS', 'BREAKEVEN']
    realized_pnl: float
    actual_underlying_move_pct: float
    exit_date: str
    exit_trigger: Literal['PROFIT_TARGET', 'LOSS_LIMIT', 'TIME_RULE', 'CATALYST_RULE', 'MANUAL']
    lesson_tags: List[str]
    user_override_logged: bool
    playbook_id: Optional[str] = None
    playbook_version: Optional[str] = None


class OpportunityRecordSchema(BaseModel):
    id: str
    playbook_id: str
    playbook_version: str
    generated_at: str
    accepted: bool
    outcome_if_taken: Optional[float] = None
    bypass_reason: Optional[str] = None


class UpdateOutcomeRequest(BaseModel):
    outcome_if_taken: float


class PlaybookMetrics(BaseModel):
    playbook_id: str
    playbook_version: str
    total_trades: int
    win_rate: Optional[float] = None
    profit_factor: Optional[float] = None
    avg_return_on_risk: Optional[float] = None
    cagr: str = "N/A (insufficient data)"
    max_drawdown: str = "N/A (insufficient data)"
    sharpe: str = "N/A (insufficient data)"


class BenchmarkData(BaseModel):
    spy_cagr: Optional[float] = None
    bxm_cagr: Optional[float] = None
    note: str = "Benchmark data stubbed — live fetch not yet implemented"


class PerformanceDiagnosticsSchema(BaseModel):
    generated_at: str
    playbook_metrics: List[PlaybookMetrics]
    benchmarks: BenchmarkData


class ClosurePostMortemModel(Base):
    __tablename__ = 'closure_post_mortems'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    position_id: Mapped[str] = mapped_column(String)
    outcome: Mapped[str] = mapped_column(String)
    realized_pnl: Mapped[float] = mapped_column(Float)
    actual_underlying_move_pct: Mapped[float] = mapped_column(Float)
    exit_date: Mapped[str] = mapped_column(String)
    exit_trigger: Mapped[str] = mapped_column(String)
    lesson_tags: Mapped[list] = mapped_column(JSON)
    user_override_logged: Mapped[bool] = mapped_column(Boolean)
    playbook_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    playbook_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    def to_schema(self) -> ClosurePostMortemSchema:
        return ClosurePostMortemSchema(
            id=self.id,
            position_id=self.position_id,
            outcome=self.outcome,  # type: ignore
            realized_pnl=self.realized_pnl,
            actual_underlying_move_pct=self.actual_underlying_move_pct,
            exit_date=self.exit_date,
            exit_trigger=self.exit_trigger,  # type: ignore
            lesson_tags=self.lesson_tags,
            user_override_logged=self.user_override_logged,
            playbook_id=self.playbook_id,
            playbook_version=self.playbook_version,
        )


class OpportunityRecordModel(Base):
    __tablename__ = 'opportunity_records'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    playbook_id: Mapped[str] = mapped_column(String)
    playbook_version: Mapped[str] = mapped_column(String)
    generated_at: Mapped[str] = mapped_column(String)
    accepted: Mapped[bool] = mapped_column(Boolean)
    outcome_if_taken: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bypass_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    def to_schema(self) -> OpportunityRecordSchema:
        return OpportunityRecordSchema(
            id=self.id,
            playbook_id=self.playbook_id,
            playbook_version=self.playbook_version,
            generated_at=self.generated_at,
            accepted=self.accepted,
            outcome_if_taken=self.outcome_if_taken,
            bypass_reason=self.bypass_reason,
        )


# =====================================================================
# Automatic Evening Scan — orchestrated Layer B fetch + Layer A/C summary
# =====================================================================

class SessionScanStateSchema(BaseModel):
    last_scan_at: str
    last_scan_date: str
    p1_count: int = 0
    p2_count: int = 0
    eligible_candidate_count: int = 0
    market_fetch_status: Literal['OK', 'FAILED', 'UNCONFIGURED'] = 'UNCONFIGURED'
    position_refresh_status: Literal['OK', 'FAILED', 'UNCONFIGURED'] = 'UNCONFIGURED'


class EveningScanResponse(BaseModel):
    ran: bool
    state: SessionScanStateSchema


class SessionScanStateModel(Base):
    __tablename__ = 'session_scan_state'

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    last_scan_at: Mapped[str] = mapped_column(String)
    last_scan_date: Mapped[str] = mapped_column(String)
    p1_count: Mapped[int] = mapped_column(Integer, default=0)
    p2_count: Mapped[int] = mapped_column(Integer, default=0)
    eligible_candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    market_fetch_status: Mapped[str] = mapped_column(String, default="UNCONFIGURED")
    position_refresh_status: Mapped[str] = mapped_column(String, default="UNCONFIGURED")

    def to_schema(self) -> SessionScanStateSchema:
        return SessionScanStateSchema(
            last_scan_at=self.last_scan_at,
            last_scan_date=self.last_scan_date,
            p1_count=self.p1_count,
            p2_count=self.p2_count,
            eligible_candidate_count=self.eligible_candidate_count,
            market_fetch_status=self.market_fetch_status,  # type: ignore
            position_refresh_status=self.position_refresh_status,  # type: ignore
        )
