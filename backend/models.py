from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm import Session as SyncSession


class Base(DeclarativeBase):
    pass


# =====================================================================
# Pydantic Schemas (for Validation & API Serialization)
# =====================================================================


class EntryFilters(BaseModel):
    min_ivr: float
    max_ivr: float
    vix_range: tuple[float, float]
    required_trend: Literal["ABOVE_SMA20", "BELOW_SMA20", "ANY"]
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


# The strategy vocabulary, declared once — playbooks and positions share it,
# and STRATEGY_BUILDERS' registry keys must cover it (asserted in tests).
StrategyType = Literal[
    "BULL_CALL_SPREAD",
    "BEAR_PUT_SPREAD",
    "BULL_PUT_SPREAD",
    "BEAR_CALL_SPREAD",
    "IRON_CONDOR",
    "BROKEN_WING_BUTTERFLY",
    "CALENDAR_SPREAD",
    "LONG_STRADDLE",
    "LONG_STRANGLE",
]


class PlaybookDefinitionSchema(BaseModel):
    id: str
    version: str
    name: str
    underlying_ticker: str
    strategy_type: StrategyType
    execution_mode: Literal["LIVE", "PAPER"]
    enabled: bool = True
    entry_filters: EntryFilters
    execution_specs: ExecutionSpecs
    exit_rules: ExitRules


class OptionLegSchema(BaseModel):
    option_type: Literal["CALL", "PUT"]
    direction: Literal["LONG", "SHORT"]
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
    pre_trade_emotional_state: Literal["Calm", "Anxious", "Chasing", "Bored"]
    pre_trade_confidence_rating: Literal[1, 2, 3, 4, 5]


class PositionSchema(BaseModel):
    id: str
    underlying: str
    strategy_type: StrategyType
    execution_mode: Literal["LIVE", "PAPER"]
    legs: list[OptionLegSchema]
    entry_date: str
    expiration_date: str
    entry_premium: float
    premium_direction: Literal["CREDIT", "DEBIT"]
    current_value_per_share: float
    contracts: int
    max_profit: float
    max_loss: float
    profit_target_per_share: float | None = None
    loss_limit_per_share: float | None = None
    break_even_upside: float | None = None
    break_even_downside: float | None = None
    notes: str
    rolls: int = 0
    status: Literal["OPEN", "CLOSED", "EXPIRED"]
    playbook_id: str | None = None
    playbook_version: str | None = None
    playbook_snapshot: PlaybookDefinitionSchema | None = None
    journal: OperationalJournalEntrySchema
    warnings_acknowledged: list[str] = Field(default_factory=list)


class AccountConfig(BaseModel):
    total_nav: float
    broker: str
    account_type: str
    options_approval: str
    execution_mode: Literal["LIVE", "PAPER"]


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
    __tablename__ = "playbooks"

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
    __tablename__ = "positions"

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
    profit_target_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    loss_limit_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    break_even_upside: Mapped[float | None] = mapped_column(Float, nullable=True)
    break_even_downside: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(String)
    rolls: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String)
    playbook_id: Mapped[str | None] = mapped_column(String, nullable=True)
    playbook_version: Mapped[str | None] = mapped_column(String, nullable=True)
    playbook_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    journal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    warnings_acknowledged: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 'B00' = legacy/manual book; 'B01'..'B22' are executor lab books
    book_id: Mapped[str] = mapped_column(
        String, ForeignKey("books.id"), nullable=False, default="B00", server_default="B00", index=True
    )

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
    __tablename__ = "portfolio_config"

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
    current_regime: Literal["CALM_BULL", "HIGH_VOL_NEUTRAL", "TRENDING_BEAR", "EVENT_CATALYST"]
    spy_price: float
    spy_sma20: float = 0.0
    vix_close: float = 0.0
    underlying_ivrs: dict[str, float] = Field(default_factory=dict)
    spy_daily_return: float = 0.0
    catalyst_dates: list[str] = Field(default_factory=list)
    regime_scores: dict[str, float] = Field(default_factory=dict)
    # Per-underlying telemetry (#139), computed from index_history at scan
    # time — never persisted. Lookups fall back to spy_price/spy_sma20 for
    # SPY-scale tickers (SPY, XSP), so the manual console needs neither dict.
    underlying_prices: dict[str, float] = Field(default_factory=dict)
    underlying_sma20: dict[str, float] = Field(default_factory=dict)


class RollLegSchema(BaseModel):
    option_type: Literal["CALL", "PUT"]
    direction: Literal["LONG", "SHORT"]
    strike: float
    expiration: str


class RollCandidateSchema(BaseModel):
    """Layer A roll assessment for a credit vertical under pressure (domain-rules.md)."""

    eligible: bool
    reason: str  # why the roll is suggested, or why it is blocked
    rolls_used: int
    rolls_max: int
    suggested_expiration: str | None = None
    suggested_legs: list[RollLegSchema] | None = None


class RollPositionRequest(BaseModel):
    close_cost_per_share: float = Field(gt=0)  # buyback cost of the current spread
    new_credit_per_share: float = Field(gt=0)  # credit received for the new spread
    new_expiration: str
    new_legs: list[RollLegSchema] = Field(min_length=2, max_length=2)


class ScannedPositionSchema(BaseModel):
    """One open position with its Layer A lifecycle verdict (observation.py)."""

    position_id: str
    underlying: str
    strategy_type: str
    contracts: int
    max_loss: float
    max_profit: float
    entry_premium: float
    current_value_per_share: float
    expiration_date: str
    priority: Literal["P1 — CLOSE NOW", "P2 — CLOSE SOON", "P2 — REVIEW", "P3 — MONITOR", "OK"]
    action: str
    reason: str
    math_detail: str
    legs: list[OptionLegSchema]
    roll: RollCandidateSchema | None = None


class PortfolioGreeksSchema(BaseModel):
    net_delta: float
    net_theta: float
    net_vega: float
    net_gamma: float


class SafeguardWarningSchema(BaseModel):
    type: str
    severity: Literal["WARNING", "CRITICAL"]
    message: str


class PortfolioObservationSchema(BaseModel):
    """Response contract for GET /api/portfolio/observation."""

    scanned_positions: list[ScannedPositionSchema]
    greeks: PortfolioGreeksSchema
    safeguards: list[SafeguardWarningSchema]
    market_state: MarketStateSchema


class MarketStateModel(Base):
    __tablename__ = "market_state"

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
            regime_scores=self.regime_scores,
        )


# =====================================================================
# Sprint 4: Layer C — Opportunity Engine Schemas
# =====================================================================


class StrikeDerivedParams(BaseModel):
    """Documents how strikes were derived so every output is traceable."""

    underlying: str
    current_price: float
    target_dte: int
    short_leg_delta: float | None = None
    long_leg_delta: float | None = None
    spread_width_dollars: float | None = None
    one_sigma_move: float | None = None  # derived from VIX
    derivation_note: str


class CandidateCard(BaseModel):
    playbook: PlaybookDefinitionSchema
    eligible: bool
    suppressed_reason: str | None = None
    strike_params: StrikeDerivedParams | None = None


class OpportunityScanResult(BaseModel):
    portfolio_blocked: bool
    block_reason: str | None = None
    candidates: list[CandidateCard]


class TradeSpecLeg(BaseModel):
    action: Literal["BUY", "SELL"]
    option_type: Literal["CALL", "PUT"]
    strike: float
    expiration_date: str
    quantity: int
    delta_target: float | None = None


class TradeSpec(BaseModel):
    playbook_id: str
    playbook_name: str
    underlying: str
    strategy_type: str
    legs: list[TradeSpecLeg]
    expiration_date: str
    dte_at_entry: int
    order_type: Literal["LIMIT"] = "LIMIT"
    limit_price_per_share: float
    max_loss_dollars: float
    max_gain_dollars: float | None = None  # None = unlimited
    max_gain_note: str
    break_even_prices: list[float]
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
    hard_blocks: list[HardBlock]
    warnings: list[TradeWarning]
    spec: TradeSpec | None = None


# =====================================================================
# Sprint 5: Post-Mortem, Opportunity Ledger, Performance Diagnostics
# =====================================================================


class ClosePositionRequest(BaseModel):
    current_value_per_share: float
    exit_trigger: Literal["PROFIT_TARGET", "LOSS_LIMIT", "TIME_RULE", "CATALYST_RULE", "MANUAL"]
    actual_underlying_move_pct: float
    lesson_tags: list[str] = Field(default_factory=list)


class ClosurePostMortemSchema(BaseModel):
    id: str
    position_id: str
    outcome: Literal["WIN", "LOSS", "BREAKEVEN"]
    realized_pnl: float
    actual_underlying_move_pct: float
    exit_date: str
    exit_trigger: Literal["PROFIT_TARGET", "LOSS_LIMIT", "TIME_RULE", "CATALYST_RULE", "MANUAL"]
    lesson_tags: list[str]
    user_override_logged: bool
    playbook_id: str | None = None
    playbook_version: str | None = None


class OpportunityRecordSchema(BaseModel):
    id: str
    playbook_id: str
    playbook_version: str
    generated_at: str
    accepted: bool
    outcome_if_taken: float | None = None
    bypass_reason: str | None = None


class UpdateOutcomeRequest(BaseModel):
    outcome_if_taken: float


class PlaybookMetrics(BaseModel):
    playbook_id: str
    playbook_version: str
    total_trades: int
    win_rate: float | None = None
    profit_factor: float | None = None
    avg_return_on_risk: float | None = None
    # None = insufficient sample (backend/performance.py gates on N and span);
    # the UI must render that honestly, never as zero.
    cagr: float | None = None
    max_drawdown: float | None = None  # dollars
    sharpe: float | None = None


class BenchmarkData(BaseModel):
    spy_cagr: float | None = None
    bxm_cagr: float | None = None
    note: str = "No benchmark data available"


class PerformanceDiagnosticsSchema(BaseModel):
    generated_at: str
    playbook_metrics: list[PlaybookMetrics]
    benchmarks: BenchmarkData


class ClosurePostMortemModel(Base):
    __tablename__ = "closure_post_mortems"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    position_id: Mapped[str] = mapped_column(String)
    outcome: Mapped[str] = mapped_column(String)
    realized_pnl: Mapped[float] = mapped_column(Float)
    actual_underlying_move_pct: Mapped[float] = mapped_column(Float)
    exit_date: Mapped[str] = mapped_column(String)
    exit_trigger: Mapped[str] = mapped_column(String)
    lesson_tags: Mapped[list] = mapped_column(JSON)
    user_override_logged: Mapped[bool] = mapped_column(Boolean)
    playbook_id: Mapped[str | None] = mapped_column(String, nullable=True)
    playbook_version: Mapped[str | None] = mapped_column(String, nullable=True)

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
    __tablename__ = "opportunity_records"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    playbook_id: Mapped[str] = mapped_column(String)
    playbook_version: Mapped[str] = mapped_column(String)
    generated_at: Mapped[str] = mapped_column(String)
    accepted: Mapped[bool] = mapped_column(Boolean)
    outcome_if_taken: Mapped[float | None] = mapped_column(Float, nullable=True)
    bypass_reason: Mapped[str | None] = mapped_column(String, nullable=True)

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
# Executor (Paper) — multi-book lab persistence
# spec/data-models.md → "Executor (Paper) schema additions" (#61)
# =====================================================================


class BookModel(Base):
    __tablename__ = "books"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # 'B00' legacy, 'B01'..'B22' lab
    name: Mapped[str] = mapped_column(String)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    config_hash: Mapped[str] = mapped_column(String, default="")
    starting_capital: Mapped[float] = mapped_column(Float)
    cash_balance: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String)  # LEGACY | ACTIVE | RESERVED | RETIRED
    created_at: Mapped[str] = mapped_column(String)  # ISO 8601 UTC
    # Previous run's mark-to-market equity — the PNL_SHOCK baseline (#71)
    last_mtm: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_mtm_at: Mapped[str | None] = mapped_column(String, nullable=True)


class OrderModel(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(String, ForeignKey("books.id"), index=True)
    position_id: Mapped[str | None] = mapped_column(String, ForeignKey("positions.id"), nullable=True)
    # basis:{book_id}:{order_id}:{action} — echoed at the broker on every order
    order_ref: Mapped[str] = mapped_column(String, unique=True)
    ib_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ib_perm_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # durable cross-session key
    action: Mapped[str] = mapped_column(String)  # OPEN | CLOSE | ROLL
    # {"legs": [...], "quantity": n, "strategy_type": ..., "expiration_date": ..., "underlying": ...}
    combo_legs: Mapped[dict] = mapped_column(JSON)
    order_type: Mapped[str] = mapped_column(String, default="LIMIT")
    limit_price: Mapped[float] = mapped_column(Float)
    decision_midpoint: Mapped[float] = mapped_column(Float)  # slippage evidence — not reconstructible later
    status: Mapped[str] = mapped_column(String)  # STAGED | SUBMITTED | PARTIAL | FILLED | CANCELLED | REJECTED
    submitted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    # Capital reserved while the order is pending — counted by the deployed
    # gate until the order reaches a terminal status (#67).
    encumbered_risk: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")


class FillModel(Base):
    """Append-only: the Live Gate's expectancy evidence (ADR-0006)."""

    __tablename__ = "fills"

    exec_id: Mapped[str] = mapped_column(String, primary_key=True)  # IBKR execId; corrections get new ids
    order_id: Mapped[str] = mapped_column(String, ForeignKey("orders.id"), index=True)
    book_id: Mapped[str] = mapped_column(String, ForeignKey("books.id"))
    con_id: Mapped[int] = mapped_column(Integer)
    side: Mapped[str] = mapped_column(String)
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    fill_time: Mapped[str] = mapped_column(String)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class ReconciliationRunModel(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[str] = mapped_column(String)
    broker_snapshot: Mapped[dict] = mapped_column(JSON)
    books_expected: Mapped[dict] = mapped_column(JSON)
    result: Mapped[str] = mapped_column(String)  # CLEAN | DRIFT
    drift_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resolved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String, nullable=True)


class GateEventModel(Base):
    """Append-only: the Live Gate's "zero breaches" evidence (ADR-0006)."""

    __tablename__ = "gate_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[str] = mapped_column(String, ForeignKey("books.id"))
    run_at: Mapped[str] = mapped_column(String)
    gate: Mapped[str] = mapped_column(String)
    result: Mapped[str] = mapped_column(String)  # PASS | BLOCK
    context: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditEventModel(Base):
    """Append-only order/control audit trail (spec/supervision.md)."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[str] = mapped_column(String)
    book_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_type: Mapped[str] = mapped_column(String)
    actor: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class TradingControlModel(Base):
    __tablename__ = "trading_control"

    scope: Mapped[str] = mapped_column(String, primary_key=True)  # 'GLOBAL' or a book id
    state: Mapped[str] = mapped_column(String)  # ACTIVE | HALT_ENTRIES | FLATTEN_REQUESTED
    reason: Mapped[str] = mapped_column(String, default="")
    actor: Mapped[str] = mapped_column(String, default="")
    changed_at: Mapped[str] = mapped_column(String)

    def to_schema(self) -> "TradingControlSchema":
        return TradingControlSchema(
            scope=self.scope,
            state=self.state,  # type: ignore
            reason=self.reason,
            actor=self.actor,
            changed_at=self.changed_at,
        )


class TradingControlSchema(BaseModel):
    scope: str
    state: Literal["ACTIVE", "HALT_ENTRIES", "FLATTEN_REQUESTED"]
    reason: str
    actor: str
    changed_at: str


class TradingControlUpdateRequest(BaseModel):
    scope: str
    state: Literal["ACTIVE", "HALT_ENTRIES", "FLATTEN_REQUESTED"]
    reason: str = Field(min_length=3)  # clearing or setting a halt requires a typed reason


class TradingControlView(BaseModel):
    controls: list[TradingControlSchema]
    sentinel_halt: bool  # the HALT file overrides everything below it


class LiveGateChecklistSchema(BaseModel):
    """ADR-0006 Live Gate criteria, each with its current value and pass flag."""

    closed_trades: int
    closed_trades_required: int
    trades_ok: bool
    months_elapsed: float
    months_required: float
    months_ok: bool
    breaches: int
    breaches_ok: bool
    expectancy_after_haircut: float | None  # None until the first closed trade
    expectancy_ok: bool
    eligible: bool  # all four criteria met


class BookSummarySchema(BaseModel):
    id: str
    name: str
    status: str
    engine_variant: str
    underlying: str
    config_hash: str
    config_version: int
    starting_capital: float
    cash_balance: float
    last_mtm: float | None
    pnl: float
    closed_trades: int
    win_rate: float | None  # None until the first closed trade
    expectancy_after_haircut: float | None
    max_drawdown: float
    deployed_pct: float
    open_positions: int
    max_positions: int
    control_state: Literal["ACTIVE", "HALT_ENTRIES", "FLATTEN_REQUESTED"]
    live_gate: LiveGateChecklistSchema


class BooksView(BaseModel):
    books: list[BookSummarySchema]


class AuditEventSchema(BaseModel):
    id: int
    run_at: str
    book_id: str | None
    event_type: str
    actor: str
    payload: dict


class ExecutorStatusSchema(BaseModel):
    heartbeat_at: str | None  # None = executor has never run
    heartbeat_age_hours: float | None
    stale: bool  # missing or older than 24h — the console paints this red
    broker_ok: bool | None
    entries_placed: int | None
    closes_placed: int | None
    last_reconciliation_at: str | None
    last_reconciliation_result: str | None


class RegimeReadingModel(Base):
    __tablename__ = "regime_readings"

    date: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(String, primary_key=True)
    engine_variant: Mapped[str] = mapped_column(String, primary_key=True)  # V0 | V1 | V2 | V3
    regime: Mapped[str] = mapped_column(String)
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    scores: Mapped[dict] = mapped_column(JSON, default=dict)


class IndexHistoryModel(Base):
    __tablename__ = "index_history"

    date: Mapped[str] = mapped_column(String, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, primary_key=True)  # VIX, VIX3M
    close: Mapped[float] = mapped_column(Float)


class BookMtmHistoryModel(Base):
    """Nightly mark-to-market per book — the equity curve (#239). Without it
    last_mtm is overwritten each night and drawdown can only be reconstructed
    from closed trades; ADR-0010's stress verification wants the real curve.
    One row per book per date; a same-day rerun overwrites its row."""

    __tablename__ = "book_mtm_history"

    book_id: Mapped[str] = mapped_column(String, ForeignKey("books.id"), primary_key=True)
    date: Mapped[str] = mapped_column(String, primary_key=True)  # ISO date
    mtm: Mapped[float] = mapped_column(Float)


class AppendOnlyViolationError(RuntimeError):
    """Raised when an UPDATE or DELETE reaches an append-only table."""


_APPEND_ONLY_MODELS = (FillModel, GateEventModel, AuditEventModel)


@event.listens_for(SyncSession, "before_flush")
def _reject_append_only_mutations(session: SyncSession, _flush_context: object, _instances: object) -> None:
    # fills / gate_events / audit_events are the Live Gate's evidence — no code
    # path may rewrite history (spec/data-models.md, ADR-0006).
    for obj in session.dirty:
        if isinstance(obj, _APPEND_ONLY_MODELS) and session.is_modified(obj):
            raise AppendOnlyViolationError(f"{type(obj).__name__} is append-only; UPDATE rejected")
    for obj in session.deleted:
        if isinstance(obj, _APPEND_ONLY_MODELS):
            raise AppendOnlyViolationError(f"{type(obj).__name__} is append-only; DELETE rejected")
