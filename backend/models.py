from enum import Enum
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
    # #317: require a catalyst scoped to THIS underlying ("EARNINGS:AAPL:date")
    # within 14 days — a market-wide FOMC date does not satisfy an earnings play.
    require_scoped_catalyst: bool = False


class ExecutionSpecs(BaseModel):
    target_dte: int
    short_leg_delta: float
    long_leg_delta: float
    spread_width_dollars: float
    straddle_atm: bool


class ExitRules(BaseModel):
    # catalyst_exit_days_after was removed (#360) — seeded everywhere,
    # consumed by nothing. Old DB rows and frozen playbook_snapshots that
    # still carry the key validate fine (pydantic ignores extras).
    profit_take_pct: float
    stop_loss_pct: float
    mandatory_exit_dte: int


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
    "LONG_PUT",
]


class PlaybookDefinitionSchema(BaseModel):
    # execution_mode was removed (#361): a pure pass-through label with zero
    # branches. The REAL mode is IBKR_TRADING_MODE + the per-mode DB file +
    # the db_meta stamp (#204); the console shows it from executor status.
    # Old rows/snapshots carrying the key validate fine (extras ignored).
    id: str
    version: str
    name: str
    underlying_ticker: str
    strategy_type: StrategyType
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
    book_id: str = "B00"  # 'B00' = manual book; executor books have real broker legs (#279)


class AccountConfig(BaseModel):
    total_nav: float
    broker: str
    account_type: str
    options_approval: str


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
    # Vestigial column (#361): unmapped from the API/schemas but kept here
    # with a default because existing databases created it NOT NULL and the
    # migration policy is additive-only (never ALTER/DROP existing columns).
    execution_mode: Mapped[str] = mapped_column(String, default="PAPER")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Store nested schemas as JSON columns
    entry_filters: Mapped[dict] = mapped_column(JSON)
    execution_specs: Mapped[dict] = mapped_column(JSON)
    exit_rules: Mapped[dict] = mapped_column(JSON)
    # Seed-sync tracking (#548 LOW-1): a fingerprint of this row's content,
    # so init_db can hash-compare against seeds.py on every start and
    # converge drift back (mirrors BookModel.config_hash/config_version,
    # ADR-0013). NULL on rows that predate the sync — treated as always
    # out of date, populated on the next start.
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    sync_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def to_schema(self) -> PlaybookDefinitionSchema:
        return PlaybookDefinitionSchema(
            id=self.id,
            version=self.version,
            name=self.name,
            underlying_ticker=self.underlying_ticker,
            strategy_type=self.strategy_type,  # type: ignore
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
    # Vestigial column (#361) — see PlaybookDefinitionModel.execution_mode.
    execution_mode: Mapped[str] = mapped_column(String, default="PAPER")
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
    # When current_value_per_share was last set from live quotes (#280):
    # exits must never chase the market off a mark of unknown age.
    last_priced_at: Mapped[str | None] = mapped_column(String, nullable=True)
    # The book config fingerprint this trade raced under (#284): gate
    # evidence must never pool trades across config changes (ADR-0003).
    config_hash: Mapped[str | None] = mapped_column(String, nullable=True)
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
            book_id=self.book_id,
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
    # #741: parity with ClosePositionRequest.acknowledge_broker_divergence —
    # this endpoint is bookkeeping-only and never touches the broker, so
    # rolling an executor-book position (real legs resting at the broker)
    # here manufactures instant DB-vs-broker drift unless explicitly forced.
    acknowledge_broker_divergence: bool = False


class ScannedPositionSchema(BaseModel):
    """One open position with its Layer A lifecycle verdict (observation.py)."""

    position_id: str
    underlying: str
    strategy_type: str
    contracts: int
    max_loss: float
    max_profit: float
    entry_premium: float
    # #479: server-side truth, not a frontend guess from legs[0].direction —
    # iron-condor/BWB leg orderings can put a SHORT leg first and mislabel a
    # credit spread as DEBIT.
    premium_direction: Literal["CREDIT", "DEBIT"]
    current_value_per_share: float
    expiration_date: str
    priority: Literal["P1 — CLOSE NOW", "P2 — CLOSE SOON", "P2 — REVIEW", "P3 — MONITOR", "OK"]
    action: str
    reason: str
    math_detail: str
    legs: list[OptionLegSchema]
    roll: RollCandidateSchema | None = None
    # #602: a non-terminal CLOSE order already exists for this position — the
    # console must not re-demand a close the system already submitted or
    # staged (risks a duplicate exit). close_in_flight_since is the order's
    # submitted_at, or None when it's STAGED and awaiting its next submission
    # attempt — close_in_flight itself is the reliable "in flight?" signal.
    close_in_flight: bool = False
    close_in_flight_since: str | None = None


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
    # #498: server truth from opportunity.py's _CREDIT_STRATEGIES, the same
    # single source PositionSchema.premium_direction traces back to — a
    # frontend-side strategy_type Set (TradeSpecCard's old DEBIT_STRATEGIES)
    # silently drifts every time a new strategy is added (it had already
    # missed CALENDAR_SPREAD and LONG_PUT).
    premium_direction: Literal["CREDIT", "DEBIT"]
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
    exit_trigger: Literal[
        "PROFIT_TARGET",
        "LOSS_LIMIT",
        "TIME_RULE",
        "CATALYST_RULE",
        "MANUAL",
        "REGIME_FLIP",
        "ASSIGNMENT_RISK",
        "EXPIRY",
    ]
    actual_underlying_move_pct: float
    lesson_tags: list[str] = Field(default_factory=list)
    # Executor-book positions have REAL legs at the broker; this endpoint is
    # bookkeeping-only, so closing one here guarantees books-vs-broker drift
    # and a global halt (#279). The caller must acknowledge that explicitly.
    acknowledge_broker_divergence: bool = False
    # #468: mirrors ExternalCloseRequest's field — the operator's assertion
    # that any STAGED/SUBMITTED order still referencing this position (most
    # commonly a resting GTC profit-taker) is already cancelled at the
    # broker. Without this, force-closing here strands that order forever:
    # the sync sees it OPEN and waits, Layer A only iterates OPEN positions
    # so it never runs the cancel-first step, and a future fill re-sells a
    # position the books already call CLOSED.
    acknowledge_cancelled: bool = False


class ClosurePostMortemSchema(BaseModel):
    id: str
    position_id: str
    outcome: Literal["WIN", "LOSS", "BREAKEVEN"]
    realized_pnl: float
    actual_underlying_move_pct: float
    exit_date: str
    exit_trigger: Literal[
        "PROFIT_TARGET",
        "LOSS_LIMIT",
        "TIME_RULE",
        "CATALYST_RULE",
        "MANUAL",
        "REGIME_FLIP",
        "ASSIGNMENT_RISK",
        "EXPIRY",
    ]
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
    # Unique (#463, Audit II R3 F3): a double-submitted external close (two
    # tabs, a retried request) that both slip past the OPEN check would each
    # write their own post-mortem for the same exit — this makes the second
    # write fail loudly instead of duplicating Live Gate expectancy evidence.
    # Fresh databases get this from create_all; existing ones are migrated in
    # database._ensure_schema_sync (a unique constraint isn't an ADD COLUMN).
    position_id: Mapped[str] = mapped_column(String, unique=True)
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
    # #713 (ADR-0014): promotion/demotion authority — reserved, UNENFORCED.
    # No book has ever been promoted, so these are None everywhere today; a
    # future promotion/demotion workflow is the first code to read or write
    # them (a separate issue, per the ADR). live_authority is deliberately
    # its own field rather than overloading `status` above, which already
    # carries the unrelated LEGACY/ACTIVE/RESERVED/RETIRED book lifecycle —
    # PAPER | LIVE | REVOKED is a different axis (paper-vs-live authority),
    # not a replacement for it. demotion_policy_version extends the #658
    # as-raced-config-hash provenance pattern to the demotion policy a live
    # grant was made under: recorded once at promotion, frozen for that
    # grant's lifetime per ADR-0014's immutability rule (a later policy
    # amendment governs only subsequent grants, never rewrites this one).
    live_authority: Mapped[str | None] = mapped_column(String, nullable=True)
    demotion_policy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    promoted_at: Mapped[str | None] = mapped_column(String, nullable=True)


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
    # The book config fingerprint this order was DECIDED under (#534): the
    # position inherits it at fill time, so a seed-sync landing between
    # stage and fill cannot mis-attribute the trade to a config that never
    # decided it.
    config_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    # #714: per-leg bid/ask/mid captured at (or immediately after) staging,
    # plus the derived pessimistic-edge net (fill-at-bid for sells, at-ask
    # for buys) and a capture timestamp — decision_midpoint alone is
    # unreconstructable evidence once the quote has moved on. Additive,
    # nullable: NULL on every pre-#714 row and on any row this capture
    # failed for (never fabricated) — capture-now-analyze-later, no
    # behavior change, nothing here ever gates an entry.
    quote_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)


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
    # Broker's execution timestamp (#539) — NOT the capture time above. NULL
    # on rows backfilled before this column existed; callers fall back to
    # fill_time. Additive column.
    exec_time: Mapped[str | None] = mapped_column(String, nullable=True)
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


class ExternalCloseRequest(BaseModel):
    """Resolution flow (#310): 'this position was closed at the broker'."""

    position_id: str
    # NaN/inf validation happens in resolution.py (#346), not here with
    # allow_inf_nan=False: pydantic's finite_number error embeds the NaN
    # input, which FastAPI's 422 encoder cannot serialize — the request
    # would 500. The function check returns a clean 400 instead.
    exit_value_per_share: float
    reason: str
    # Operator's assertion that any pending orders on this position are
    # already cancelled at the broker (#407) — lets resolution terminalize
    # the DB rows instead of refusing forever.
    acknowledge_cancelled: bool = False


class CashAdjustmentRequest(BaseModel):
    """Resolution flow (#310): a signed cash correction with a reason."""

    book_id: str
    delta: float  # finite-checked in resolution.py (#346) — see ExternalCloseRequest
    reason: str


class PartialOrderResolveRequest(BaseModel):
    """Resolution flow (#414): terminalize a PARTIAL order row, releasing its
    encumbrance and slot after the human has recorded the partial's cash and
    position consequences."""

    order_ref: str
    reason: str


class PartialOrderResolveResult(BaseModel):
    order_ref: str
    status: str  # the row's new terminal status


class CashAdjustmentResult(BaseModel):
    book_id: str
    cash_balance: float


class FlexAckRequest(BaseModel):
    """Resolution flow (#544): explain a Flex-audit discrepancy exec_id once
    so the weekly audit stops re-alerting a correction already made."""

    exec_ids: list[str]
    reason: str


class FlexAckResult(BaseModel):
    acked: list[str]  # newly acknowledged this call (already-acked ids are skipped, idempotent)
    already_acked: list[str]


class ResolveRunRequest(BaseModel):
    resolution: str


class ReconciliationRunSchema(BaseModel):
    id: int
    run_at: str
    result: str
    drift_details: list[dict] | None = None
    resolved_at: str | None = None
    resolution: str | None = None


class FillQualityRow(BaseModel):
    """One filled order's execution quality (#242). Per-share values are
    signed like order rows (negative = credit); slippage values are oriented
    so positive = worse than the decided mid."""

    order_ref: str
    book_id: str
    action: str  # OPEN | CLOSE | TP
    underlying: str
    contracts: int
    decision_midpoint: float
    limit_price: float
    net_fill_per_share: float | None = None  # None until fills backfill
    ladder_concession_per_share: float
    market_slippage_per_share: float | None = None
    total_slippage_per_share: float | None = None
    commissions: float


class FillQualityAggregate(BaseModel):
    label: str
    orders: int
    contracts: int
    avg_slippage_per_contract: float | None = None
    total_commissions: float


class FillQualityReport(BaseModel):
    generated_at: str
    orders_analyzed: int
    orders_awaiting_fills: int
    haircut_per_contract: float  # the ADR-0006 assumption to beat
    avg_slippage_per_contract: float | None = None
    total_commissions: float
    by_book: list[FillQualityAggregate]
    by_action: list[FillQualityAggregate]
    rows: list[FillQualityRow]


class KnobPointSchema(BaseModel):
    """One book's reading along a swept knob dimension (#243)."""

    book_id: str
    knob_value: str
    expectancy_after_haircut: float | None = None
    closed_trades: int


class KnobSweepSchema(BaseModel):
    dimension: str
    points: list[KnobPointSchema]
    verdict: str  # "monotonic ↑" | "monotonic ↓" | "non-monotonic" | "insufficient data"


class LeaderboardReport(BaseModel):
    generated_at: str
    min_trades_per_point: int
    ranked: list["BookSummarySchema"]
    sweeps: list[KnobSweepSchema]


class EvidenceVerdictSchema(BaseModel):
    """#716: the project's single reproducible 'why should I believe this'
    page. Every field is either a raw ledger aggregate or an EXISTING
    pre-registered judgment (the Live Gate checklist's own conditions, the
    #657 null-drill result when supplied) — the verdict enum's precedence
    order is the only new composition here, and it composes rather than
    invents thresholds. Stamped as_of/evidence_through/policy_version so a
    historical verdict is reproduced exactly by re-running the SAME pure
    function with the SAME cutoff, never by trusting a stored number that
    could drift from the ledger underneath it."""

    as_of: str  # when this report was computed
    evidence_through: str  # the ledger cutoff actually used
    policy_version: int  # this function's OWN composition-policy version

    closed_trades: int
    elapsed_months: float
    books_raced: int
    variants_tested: int
    variants_abandoned: int  # RETIRED — the denominator every blown-up fund fails to report

    expected_net_profit: float | None  # None with zero closed trades
    expected_net_profit_ci_low: float | None  # 95% CI; None below n=2
    expected_net_profit_ci_high: float | None
    max_drawdown: float
    worst_observed_loss: float

    spy_benchmark_line: str | None  # reused verbatim from benchmark.spy_benchmark_line — no new judgment

    envelope_breaches: int
    anomaly_events: int

    verdict: Literal["insufficient", "promising", "compelling", "failed"]
    verdict_basis: str  # one line naming which existing machinery drove the verdict


class RegimeHitRateRow(BaseModel):
    """Closed-trade outcomes for one entry-day regime (#244), optionally
    split by the engine variant that decided the entry."""

    regime: str
    engine_variant: str | None = None
    closed_trades: int
    wins: int
    win_rate: float | None = None
    avg_pnl: float | None = None
    total_pnl: float


class RegimeHitRateReport(BaseModel):
    generated_at: str
    closed_trades: int
    by_regime: list[RegimeHitRateRow]
    by_engine_regime: list[RegimeHitRateRow]


class DbMetaModel(Base):
    """Facts about the database FILE itself (#204): the trading-mode stamp
    lives here so a paper process can never open a live database or vice
    versa — mode mismatch refuses at startup, before any read or write."""

    __tablename__ = "db_meta"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)


class BrokerSnapshotModel(Base):
    """Last observed broker-account telemetry (#860). Single row (id=1),
    overwritten each capture. Display-only — NEVER a trading input: book
    gates read their envelope basis (executor._book_scan_config), and the
    order path must not consume this number."""

    __tablename__ = "broker_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    net_liquidation: Mapped[float] = mapped_column(Float)
    captured_at: Mapped[str] = mapped_column(String)


class PortfolioOverviewSchema(BaseModel):
    """Console headline (#860): the fleet's ledger NAV plus the broker's own
    last-seen account value — two provenances, labeled, never merged."""

    fleet_nav: float
    active_books: int
    broker_nav: float | None
    broker_nav_captured_at: str | None
    broker: str


class AuditEventModel(Base):
    """Append-only order/control audit trail (spec/supervision.md)."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[str] = mapped_column(String)
    book_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_type: Mapped[str] = mapped_column(String)
    actor: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class FlexAckModel(Base):
    """Append-only acknowledgment ledger for the weekly Flex audit (#544).

    Corrections made through the sanctioned resolution endpoints (external
    close, cash adjust) never create FillModel rows, and nothing else
    recorded "this exec_id was explained" — so a corrected discrepancy
    re-alerted at urgent priority forever, training the operator to file
    the push away and burying the next REAL missed fill. A human explains
    each exec_id here exactly once; audit_fills excludes acked ids from
    alerting but still reports them ("acknowledged: N")."""

    __tablename__ = "flex_acks"

    exec_id: Mapped[str] = mapped_column(String, primary_key=True)
    reason: Mapped[str] = mapped_column(String)
    acked_at: Mapped[str] = mapped_column(String)


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
    # Plain-English label (#600/#609) for a book-scoped control row — "B04 —
    # SPY 745/742 bull put (Oct 2 '26)" instead of the bare scope/book_id the
    # StatusStrip halt banner used to show. None for the GLOBAL scope
    # (there's no book to label) — to_schema() has no DB session, so this is
    # populated by the route after fetching the rows, same pattern as
    # AuditEventSchema.book_label.
    label: str | None = None


class TradingControlUpdateRequest(BaseModel):
    scope: str
    state: Literal["ACTIVE", "HALT_ENTRIES", "FLATTEN_REQUESTED"]
    reason: str = Field(min_length=3)  # clearing or setting a halt requires a typed reason


class TradingControlView(BaseModel):
    controls: list[TradingControlSchema]
    sentinel_halt: bool  # the HALT file overrides everything below it


class LiveGateConditionSchema(BaseModel):
    """One ADR-0010 promotion condition beyond the original ADR-0006 four
    (#655): stress-episode observation, the mechanical SPY benchmark
    comparison, the ADR-0009 same-engine-baseline rule, and the composition
    limit. None of these has detection machinery yet (#215 tracks it) — every
    row renders 'not_yet_evaluated' until its own PR lands. key values are
    chosen to match the detection machinery's eventual naming."""

    key: str
    label: str
    status: Literal["ok", "fail", "not_yet_evaluated"]
    detail: str = ""


class TailMagnitudeCheckSchema(BaseModel):
    """#717: an INFORMATIONAL, explicitly non-gating estimate of the book's
    CURRENT open positions' hypothetical loss under a tail move — 3× the
    largest single adverse trade the book's own gate-window closed trades
    have shown, per-position capped at that position's own max_loss (a
    defined-risk structure's real worst case, by construction, regardless
    of how large the hypothetical move gets — showing that cap BIND is the
    point of the row, not a bug in it). Deliberately kept OUT of
    additional_conditions and out of `eligible`'s computation: whether this
    ever becomes a gating condition is a future ADR decision made while
    looking at real numbers, not a decision this row makes for anyone."""

    largest_adverse_move: float  # abs($) of the single worst gate-window closed trade; 0.0 if none/no losses
    multiplier: float = 3.0
    hypothetical_tail_loss: (
        float  # sum over open positions of min(position max_loss $, multiplier * largest_adverse_move)
    )
    informational: bool = True  # always True — never read for gating, present so a client can't miss the intent


class TailHedgeMetricsSchema(BaseModel):
    """ADR-0012 (#772): the tail-hedge sleeve (B32) is judged on convexity,
    never expectancy — these three metrics REPLACE the standard Live Gate
    read for a book the console renders this way; the sleeve's Live Gate row
    stays permanently ineligible regardless of what these say."""

    bleed_rate_pct_per_month: float | None  # avg monthly cost as % of sleeve basis; None with <2 dated marks
    stress_episode_payoff: float | None  # sleeve P&L during ADR-0010 stress episodes; None until one is observed
    stress_episode_status: Literal["no_episode_yet", "measured"]
    # lab-wide max-drawdown delta: (without the sleeve) − (with the sleeve).
    # Positive means the sleeve REDUCED lab-wide drawdown; None below 2 dated
    # marks across the lab.
    portfolio_contribution: float | None


class LiveGateChecklistSchema(BaseModel):
    """ADR-0006/ADR-0010 Live Gate criteria, each with its current value and
    pass flag. eligible is un-claimable (#655) while any additional_conditions
    row is still 'not_yet_evaluated' — a materially weaker standard than the
    ADR grants must never render as a green checkmark."""

    closed_trades: int
    closed_trades_required: int
    trades_ok: bool
    months_elapsed: float
    months_required: float
    months_ok: bool
    breaches: int
    breaches_ok: bool
    expectancy_after_haircut: float | None  # None until the first closed trade
    expectancy_se: float | None  # #656: sample SE of per-trade haircut P&L; None below n=2
    expectancy_ok: bool  # #656: expectancy − 1·SE ≥ 0 (interim floor, ADR-0010 amendment)
    additional_conditions: list[LiveGateConditionSchema]  # ADR-0010, #655
    tail_magnitude_check: TailMagnitudeCheckSchema  # #717: informational only, never in `eligible`
    eligible: bool  # the original four criteria AND every additional_conditions row 'ok'
    # The config_hash whose era (#534) this checklist's trades/months/
    # expectancy were accumulated under (#658) — NOT necessarily the book's
    # CURRENT config_hash if it has since resynced to a new era with less
    # evidence. Provenance for a human reading a green leaderboard: seeing
    # several one-knob books race clean does not mean a config that grafts
    # their knobs together ever raced at all (ADR-0010's composition limit).
    # The mechanical promotion-time equality check (proposed live config's
    # hash must equal an as-raced hash whose own gate conditions passed)
    # lands with the promotion workflow (~#215-adjacent); this field only
    # surfaces the provenance now.
    as_raced_config_hash: str


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
    expectancy_se: float | None  # #656: sample SE of per-trade haircut P&L; None below n=2
    max_drawdown: float
    deployed_pct: float
    open_positions: int
    max_positions: int
    control_state: Literal["ACTIVE", "HALT_ENTRIES", "FLATTEN_REQUESTED"]
    live_gate: LiveGateChecklistSchema
    # ADR-0012 / #772: set only for the tail-hedge sleeve (B32) — the console
    # renders these INSTEAD of standard expectancy/win-rate for that row.
    tail_hedge_metrics: TailHedgeMetricsSchema | None = None


class BooksView(BaseModel):
    books: list[BookSummarySchema]


class AuditEventSchema(BaseModel):
    id: int
    run_at: str
    book_id: str | None
    event_type: str
    actor: str
    payload: dict
    # Server-computed from digest.is_urgent_event_type (#474) so the
    # console's "needs a human now" highlighting can never diverge from the
    # nightly urgent-push tiering — one exported set, not a second guess.
    urgent: bool
    # Plain-English book/instrument label (#600) — "B04 — SPY 745/742 bull
    # put (Oct 2 '26)" instead of a bare book_id the operator has to go look
    # up. None only when book_id itself is None (a run-wide event).
    book_label: str | None = None


class LiveOrderSchema(BaseModel):
    """A resting-at-broker order for the console's live-order panel (#601) —
    lets an operator directly compare against the IBKR app during an
    incident instead of reconstructing it from audit rows. Only orders in a
    non-terminal status (STAGED/SUBMITTED/PARTIAL) are ever returned."""

    order_ref: str
    book_id: str
    # Plain-English label (#600) built from this order's own combo_legs —
    # not book_label()'s "most recent OPEN position" guess, since a STAGED/
    # SUBMITTED entry order has no position row yet.
    label: str
    order_type: str
    # Derived from the ref convention (basis:...:open[:tp] vs :close): the
    # DB does not persist TIF, but placement is DAY except a `:tp` child
    # order, which is submitted GTC (backend/broker.py place_spread).
    tif: Literal["DAY", "GTC"]
    status: Literal["STAGED", "SUBMITTED", "PARTIAL"]
    submitted_at: str | None


class ExecutorStatusSchema(BaseModel):
    heartbeat_at: str | None  # None = executor has never run
    heartbeat_age_hours: float | None
    stale: bool  # missing or older than 24h — the console paints this red
    broker_ok: bool | None
    entries_placed: int | None
    closes_placed: int | None
    last_reconciliation_at: str | None
    last_reconciliation_result: str | None
    # #478: None = no run yet or the run wasn't DRIFT; True/False once a run
    # IS drift — a human recording a resolution doesn't change the STRIP's
    # halt state (ADR-0008), but the console must show the recon itself was
    # explained, not leave "DRIFT" looking identical before and after.
    last_reconciliation_resolved: bool | None = None
    # Digest delivery (#277): None = no digest ever composed; False = the
    # last composed digest failed to push (ntfy outage — check logs/audit).
    last_digest_at: str | None = None
    last_digest_pushed: bool | None = None
    # Urgent-push delivery (#478): None = the last digest had no urgent
    # events to push (nothing to deliver); False = urgent events existed but
    # the push failed — as invisible an outage as last_digest_pushed=False.
    last_urgent_pushed: bool | None = None
    # The REAL trading mode of the backend this console is talking to (#361):
    # IBKR_TRADING_MODE as resolved at process start — never a form field.
    trading_mode: Literal["paper", "live"] = "paper"


# =====================================================================
# GET /api/attention — the "what needs you" triage surface (#890)
# =====================================================================


class AttentionActionKind(str, Enum):
    ACK_HALT = "ack_halt"  # POST /api/trading-control (allow_resume=True path)
    RESOLVE_RECONCILIATION = "resolve_reconciliation"  # POST /api/reconciliation/{run_id}/resolve
    RESOLVE_PARTIAL_ORDER = "resolve_partial_order"  # POST /api/resolution/partial-order
    FLEX_ACK = "flex_ack"  # POST /api/resolution/flex-ack
    CLOSE_POSITION = "close_position"  # POST /api/positions/{id}/close
    VIEW_ONLY = "view_only"  # navigate to a tab/panel; no mutation
    ACKNOWLEDGE_ONLY = "acknowledge_only"  # nothing to submit — informational, never counted


# Kinds that carry no alarm weight: nothing to resolve, nothing to submit —
# informational only. attention.py's problem_count and the frontend's
# actionable/informational split both read this one set (#915) instead of
# each independently excluding ACKNOWLEDGE_ONLY (and silently missing
# VIEW_ONLY, which is exactly as inert but was never emitted until now).
NON_ALARM_ACTION_KINDS: frozenset[AttentionActionKind] = frozenset(
    {AttentionActionKind.ACKNOWLEDGE_ONLY, AttentionActionKind.VIEW_ONLY}
)

# The four kinds whose POST endpoints validate a >=3-char reason
# UNCONDITIONALLY server-side (TradingControlUpdateRequest.reason,
# resolve_reconciliation_run, resolution._require_reason x2). An action of
# one of these kinds constructed with requires_reason=False would make the
# client submit an empty reason and 400 — so that combination is forbidden,
# and test_attention.py pins it against every construction in attention.py.
REASON_VALIDATED_KINDS: frozenset[AttentionActionKind] = frozenset(
    {
        AttentionActionKind.ACK_HALT,
        AttentionActionKind.RESOLVE_RECONCILIATION,
        AttentionActionKind.RESOLVE_PARTIAL_ORDER,
        AttentionActionKind.FLEX_ACK,
    }
)


class AttentionAction(BaseModel):
    kind: AttentionActionKind
    label: str  # exact button text, e.g. "Review + Resume", "Resolve drift", "Close now"
    requires_reason: bool = False  # true forces the typed-reason step (ADR-0008) before submit
    endpoint: str | None = None  # POST target; None for VIEW_ONLY / ACKNOWLEDGE_ONLY
    # Opaque identifiers the client echoes back verbatim in the POST body —
    # never re-derived client-side (scope/run_id/order_ref/exec_ids/position_id).
    target: dict[str, str | list[str]] = Field(default_factory=dict)
    navigate_to: str | None = None  # for VIEW_ONLY: which tab ("books", "overview") and optional anchor


class HaltItem(BaseModel):
    """One latched TradingControlModel row, or the sentinel file.
    Source: TradingControlModel rows (same query GET /api/trading-control runs) +
    trading_control.sentinel_halt_active() + labels.book_label() for the
    plain-English scope name."""

    scope: str  # "GLOBAL", "SENTINEL", or a book id
    scope_label: str  # book_label() output, or "GLOBAL" / "HALT sentinel file"
    state: str  # "HALT_ENTRIES" | "FLATTEN_REQUESTED"
    reason: str
    actor: str
    since: str  # changed_at
    action: AttentionAction  # ACK_HALT (requires_reason=True) for DB rows;
    # ACKNOWLEDGE_ONLY for the sentinel (needs a file delete, not a console act)


class PositionActionItem(BaseModel):
    """A P1/P2 scanned position needing eyes. Source: observation.compose_observation
    (run_lifecycle_scan/derive_roll_candidate) — reuses the same scan already
    computed for /api/portfolio/observation, not a new one."""

    position_id: str
    book_id: str
    underlying: str
    strategy_type: str
    priority: str  # "P1 — CLOSE NOW" | "P2 — CLOSE SOON" | "P2 — REVIEW"
    reason: str
    close_in_flight: bool  # true -> action.kind is ACKNOWLEDGE_ONLY, never counted (#602 semantics)
    action: AttentionAction  # CLOSE_POSITION when actionable; ACKNOWLEDGE_ONLY when close_in_flight


class ReconciliationDriftItem(BaseModel):
    """Source: reconciliation.latest_reconciliation_run() — the SAME query the
    status strip badge and /api/reconciliation/latest use, so this view can
    never disagree with them about "the current drift"."""

    run_id: int
    run_at: str
    drift_count: int
    drift_summary: list[str]  # short kind:key strings, e.g. "GHOST_ORDER: basis:B04:o1:open"
    resolved: bool  # run.resolved_at is not None
    # RESOLVE_RECONCILIATION (requires_reason=True) while unresolved; None once
    # resolved — the linked HaltItem still carries the resume, so a second
    # acknowledge-only item here would just be noise.
    action: AttentionAction | None = None


class PartialOrderItem(BaseModel):
    """Source: OrderModel rows with status == states.ORDER_PARTIAL_STATUS
    (a human-resolved latch, never auto-clears — backend/states.py) joined to
    labels.order_label() for the plain-English leg."""

    order_ref: str
    book_id: str
    label: str
    action: AttentionAction  # RESOLVE_PARTIAL_ORDER, requires_reason=True


class FlexDiscrepancyItem(BaseModel):
    """Source: the latest FLEX_AUDIT audit event's payload.discrepancies —
    already excludes previously-acked exec_ids at generation time
    (flex_audit.audit_fills), so this list IS "awaiting ack", with zero extra
    filtering needed here."""

    exec_id: str | None  # None for a non-exec-scoped line (e.g. NO_ORDER_REFS_IN_EXPORT) — not ackable
    description: str  # the raw discrepancy line, unmodified
    action: AttentionAction  # FLEX_ACK (requires_reason=True) when exec_id is set;
    # ACKNOWLEDGE_ONLY otherwise


class DeliveryGapItem(BaseModel):
    """Source: console.executor_status() — reuses the exact fields the
    StatusStrip already renders (last_digest_pushed, last_urgent_pushed),
    just promoted into the triage block instead of a small strip badge an
    operator has to notice."""

    kind: str  # "digest" | "urgent_push"
    since: str | None  # last_digest_at
    action: AttentionAction  # ACKNOWLEDGE_ONLY — nothing to submit; self-clears on the next successful push


class BrokerErrorItem(BaseModel):
    """Source: AuditEventModel rows where event_type == 'EXECUTOR_BROKER_UNAVAILABLE'
    (digest.is_urgent_event_type-classified) since the shared urgent-events lookback,
    with the NEEDS_HUMAN_BROKER_ERRORS (backend/broker.py) instruction text
    resolved the same way digest.py's own lookup does — this endpoint reads
    the same classification, it does not invent a new one."""

    book_id: str | None  # None when it's a run-wide gateway failure
    at: str
    instruction: str  # the operator-facing fix, verbatim
    action: AttentionAction  # ACKNOWLEDGE_ONLY — resolved by fixing IBKR/Gateway, not a console call


class UnresolvedUrgentEvent(BaseModel):
    """Catch-all for urgent audit event types with no dedicated typed bucket above.
    Source: AuditEventModel rows since the shared urgent-events lookback where
    digest.is_urgent_event_type(event_type) is true and the event isn't already
    represented by a BrokerErrorItem above (dedup by audit event id, not by
    event_type — a book can have both a HALT and its own REPEATED_REJECTION
    event, and both are real)."""

    id: int
    run_at: str
    book_label: str | None
    event_type: str
    detail: str  # payload.reason/detail/error/order_ref, same fallback chain as digest.urgent_events
    action: AttentionAction  # ACKNOWLEDGE_ONLY — these are historical facts, not clearable state;
    # if the underlying condition also latched a halt, THAT item carries the ack


class AttentionResponse(BaseModel):
    generated_at: str
    status: str  # "ok" | "attention"
    headline: str  # "All clear" | "4 things need you"
    problem_count: int  # count of items above whose action.kind is not in NON_ALARM_ACTION_KINDS

    sentinel_halt: bool  # trading_control.sentinel_halt_active()
    halts: list[HaltItem]
    p1_actions: list[PositionActionItem]  # actionable + close-in-flight, both — UI splits by close_in_flight
    reconciliation_drift: ReconciliationDriftItem | None
    partial_orders: list[PartialOrderItem]
    flex_discrepancies: list[FlexDiscrepancyItem]
    delivery_gaps: list[DeliveryGapItem]
    broker_errors: list[BrokerErrorItem]
    unresolved_urgent_events: list[UnresolvedUrgentEvent]


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


class AnomalyAlertStateModel(Base):
    """Last-ALERTED magnitude per standing anomaly sub-breach (#922, #924):
    the ntfy-push dedup cache for anomaly.py's _should_alert. Persisted (not
    in-memory) — the executor is a fresh process every run, so an in-memory
    cache would forget every prior alert and never suppress anything. Same
    per-key-upsert shape as TradingControlModel/BookMtmHistoryModel; this
    table is NOT in models._APPEND_ONLY_MODELS — every update overwrites the
    prior row on purpose, unlike the append-only ledger tables it sits
    alongside conceptually, and anomaly.py explicitly DELETEs a row once its
    sub-breach resolves (_clear_resolved_sub_breaches) so a later, unrelated
    breach never inherits a stale baseline. *key* is
    f"{rule}|{scope}|{kind}" (anomaly.py owns the format) — one row per
    structurally distinct sub-check (e.g. "count", "deployed",
    "per_trade:{pos_id}", "bucket:{strategy}@{expiry}"), never one row per
    finding; *last_magnitude* is a dimensionless measured/cap ratio, not a
    dollar amount, comparable night over night WITHIN one kind's rows —
    never across different kinds' rows, which have unlike units (a position
    count and a dollar figure are both dimensionless as ratios but not
    severity-comparable)."""

    __tablename__ = "anomaly_alert_state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    last_magnitude: Mapped[float] = mapped_column(Float)
    last_alerted_at: Mapped[str] = mapped_column(String)


class AppendOnlyViolationError(RuntimeError):
    """Raised when an UPDATE or DELETE reaches an append-only table."""


_APPEND_ONLY_MODELS = (FillModel, GateEventModel, AuditEventModel, FlexAckModel)


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
