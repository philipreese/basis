import asyncio
import hashlib
import json
import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.models import (
    Base,
    BookModel,
    MarketStateModel,
    PlaybookDefinitionModel,
    PortfolioConfigModel,
    TradingControlModel,
)
from backend.regime import compute_regime

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///options_playbook.db")

engine = create_async_engine(DATABASE_URL)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


# Seed Data from Section 9
SEED_PORTFOLIO_CONFIG = {
    "account": {
        "total_nav": 10000.0,
        "broker": "Charles Schwab",
        "account_type": "Roth IRA",
        "options_approval": "Level 3 — Spreads",
        "execution_mode": "PAPER",
    },
    "risk_profile": {
        "max_trade_risk_pct": 15.0,
        "max_trade_risk_dollars": 1500.0,
        "max_underlying_concentration_pct": 35.0,
        "max_correlated_index_pct": 50.0,
        "minimum_cash_reserve_pct": 15.0,
        "max_simultaneous_positions": 3,
        "max_capital_deployed_pct": 85.0,
    },
    "portfolio_greek_limits": {
        "max_net_delta": 50.0,
        "max_net_vega": 100.0,
        "max_net_gamma": 10.0,
    },
}

SEED_PLAYBOOKS = [
    {
        "id": "spy_iron_condor_v1",
        "version": "1.0",
        "name": "SPY Iron Condor — High-Vol Neutral",
        "underlying_ticker": "SPY",
        "strategy_type": "IRON_CONDOR",
        "execution_mode": "PAPER",
        "entry_filters": {
            "min_ivr": 50.0,
            "max_ivr": 100.0,
            "vix_range": [15.0, 35.0],
            "required_trend": "ANY",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.16,
            "long_leg_delta": 0.05,
            # $3 wings keep max loss under the ADR-0006 2.5%/trade cap (#94)
            "spread_width_dollars": 3.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 50.0,
            "stop_loss_pct": 200.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_bull_call_spread_v1",
        "version": "1.0",
        "name": "SPY Bull Call Spread — Calm Bull",
        "underlying_ticker": "SPY",
        "strategy_type": "BULL_CALL_SPREAD",
        "execution_mode": "PAPER",
        "entry_filters": {
            "min_ivr": 20.0,
            "max_ivr": 60.0,
            "vix_range": [10.0, 25.0],
            "required_trend": "ABOVE_SMA20",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 45,
            "short_leg_delta": 0.25,
            "long_leg_delta": 0.50,
            # $5 wide caps the debit near the per-trade limit (#94)
            "spread_width_dollars": 5.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 100.0,
            "stop_loss_pct": 50.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_bear_put_spread_v1",
        "version": "1.0",
        "name": "SPY Bear Put Spread — Trending Bear",
        "underlying_ticker": "SPY",
        "strategy_type": "BEAR_PUT_SPREAD",
        "execution_mode": "PAPER",
        "entry_filters": {
            "min_ivr": 20.0,
            "max_ivr": 70.0,
            "vix_range": [15.0, 40.0],
            "required_trend": "BELOW_SMA20",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 45,
            "short_leg_delta": 0.25,
            "long_leg_delta": 0.50,
            # $5 wide caps the debit near the per-trade limit (#94)
            "spread_width_dollars": 5.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 100.0,
            "stop_loss_pct": 50.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_bull_put_spread_v1",
        "version": "1.0",
        "name": "SPY Bull Put Spread — Calm Bull Income",
        "underlying_ticker": "SPY",
        "strategy_type": "BULL_PUT_SPREAD",
        "execution_mode": "PAPER",
        "enabled": True,
        "entry_filters": {
            "min_ivr": 20.0,
            "max_ivr": 100.0,
            "vix_range": [10.0, 30.0],
            "required_trend": "ABOVE_SMA20",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.30,
            "long_leg_delta": 0.10,
            # $3 wings keep max loss under the ADR-0006 2.5%/trade cap (#94)
            "spread_width_dollars": 3.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 50.0,
            "stop_loss_pct": 200.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_bear_call_spread_v1",
        "version": "1.0",
        "name": "SPY Bear Call Spread — Trending Bear Income",
        "underlying_ticker": "SPY",
        "strategy_type": "BEAR_CALL_SPREAD",
        "execution_mode": "PAPER",
        "enabled": True,
        "entry_filters": {
            "min_ivr": 25.0,
            "max_ivr": 100.0,
            "vix_range": [15.0, 45.0],
            "required_trend": "BELOW_SMA20",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.30,
            "long_leg_delta": 0.10,
            # $3 wings keep max loss under the ADR-0006 2.5%/trade cap (#94)
            "spread_width_dollars": 3.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 50.0,
            "stop_loss_pct": 200.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_long_straddle_v1",
        "version": "1.0",
        "name": "SPY Long Straddle — Event Catalyst",
        "underlying_ticker": "SPY",
        "strategy_type": "LONG_STRADDLE",
        "execution_mode": "PAPER",
        # Disabled by default: buying vol into known catalysts fights pre-event
        # IV inflation and post-event crush. Kept for catalyst-study use only.
        "enabled": False,
        "entry_filters": {
            "min_ivr": 30.0,
            "max_ivr": 100.0,
            "vix_range": [0.0, 100.0],
            "required_trend": "ANY",
            "block_catalyst_14dte": False,
            "require_catalyst_14dte": True,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.50,
            "long_leg_delta": 0.50,
            "spread_width_dollars": 0.0,
            "straddle_atm": True,
        },
        "exit_rules": {
            "profit_take_pct": 100.0,
            "stop_loss_pct": 50.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_long_strangle_v1",
        "version": "1.0",
        "name": "SPY Long Strangle — Event Catalyst (OTM)",
        "underlying_ticker": "SPY",
        "strategy_type": "LONG_STRANGLE",
        "execution_mode": "PAPER",
        # Disabled by default — same rationale as the long straddle above.
        "enabled": False,
        "entry_filters": {
            "min_ivr": 30.0,
            "max_ivr": 100.0,
            "vix_range": [15.0, 100.0],
            "required_trend": "ANY",
            "block_catalyst_14dte": False,
            "require_catalyst_14dte": True,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.25,
            "long_leg_delta": 0.25,
            "spread_width_dollars": 0.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 100.0,
            "stop_loss_pct": 50.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
]

# Test-fixture data only — NOT seeded into real databases (#53). These June/July
# 2026 demo straddles are long expired; test fixtures import them to build
# in-memory databases with known positions.
SEED_POSITIONS = [
    {
        "id": "seed_pos_spy_straddle_jun18",
        "underlying": "SPY",
        "strategy_type": "LONG_STRADDLE",
        "execution_mode": "PAPER",
        "legs": [
            {
                "option_type": "CALL",
                "direction": "LONG",
                "strike": 759.0,
                "expiration": "2026-06-18",
                "delta": 0.5,
                "theta": -0.1,
                "vega": 0.2,
                "gamma": 0.05,
            },
            {
                "option_type": "PUT",
                "direction": "LONG",
                "strike": 759.0,
                "expiration": "2026-06-18",
                "delta": -0.5,
                "theta": -0.1,
                "vega": 0.2,
                "gamma": 0.05,
            },
        ],
        "entry_date": "2026-06-07",
        "expiration_date": "2026-06-18",
        "contracts": 1,
        "premium_direction": "DEBIT",
        "entry_premium": 16.61,
        "current_value_per_share": 16.61,
        "max_profit": 999999.0,
        "max_loss": 16.61,
        "profit_target_per_share": 33.22,
        "loss_limit_per_share": 8.31,
        "notes": "Learning exercise. Expiration BEFORE SpaceX IPO date. Treat as short-term straddle mechanics study. Do not extend or roll.",
        "rolls": 0,
        "status": "OPEN",
        "journal": {
            "core_thesis_rationale": "Short-term volatility study around SpaceX roadshow June 8. Not the primary IPO thesis trade.",
            "structural_invalidation": "SPY remains pinned within 1% of 759 through June 15.",
            "expected_underlying_move_pct": 2.2,
            "pre_trade_emotional_state": "Calm",
            "pre_trade_confidence_rating": 3,
        },
    },
    {
        "id": "seed_pos_spy_straddle_jul18",
        "underlying": "SPY",
        "strategy_type": "LONG_STRADDLE",
        "execution_mode": "PAPER",
        "legs": [
            {
                "option_type": "CALL",
                "direction": "LONG",
                "strike": 757.0,
                "expiration": "2026-07-18",
                "delta": 0.5,
                "theta": -0.05,
                "vega": 0.3,
                "gamma": 0.03,
            },
            {
                "option_type": "PUT",
                "direction": "LONG",
                "strike": 757.0,
                "expiration": "2026-07-18",
                "delta": -0.5,
                "theta": -0.05,
                "vega": 0.3,
                "gamma": 0.03,
            },
        ],
        "entry_date": "2026-06-07",
        "expiration_date": "2026-07-18",
        "contracts": 1,
        "premium_direction": "DEBIT",
        "entry_premium": 28.18,
        "current_value_per_share": 28.18,
        "max_profit": 999999.0,
        "max_loss": 28.18,
        "profit_target_per_share": 56.36,
        "loss_limit_per_share": 14.09,
        "break_even_upside": 785.18,
        "break_even_downside": 728.82,
        "notes": "Primary SpaceX IPO thesis trade. Roadshow June 8. IPO target late June. Close within 5 trading days after IPO fires regardless of profit target. Do not hold through IV crush.",
        "rolls": 0,
        "status": "OPEN",
        "journal": {
            "core_thesis_rationale": "Largest IPO in history creates market volatility regardless of direction. Vol expansion expected across roadshow and IPO window.",
            "structural_invalidation": "Implied volatility collapses before IPO date or SPY remains pinned through late June.",
            "expected_underlying_move_pct": 2.2,
            "pre_trade_emotional_state": "Calm",
            "pre_trade_confidence_rating": 4,
        },
    },
]


# The six-book lab allocation (design §5 race design + resolved decision 5):
# variant × underlying, identical playbook mixes and envelopes per axis.
LAB_BOOKS: list[tuple[str, str, str]] = [
    ("B01", "V0", "XSP"),
    ("B02", "V1", "XSP"),
    ("B03", "V2", "XSP"),
    ("B04", "V0", "SPY"),
    ("B05", "V1", "SPY"),
    ("B06", "V2", "SPY"),
]


def _config_hash(config: dict) -> str:
    """Stable fingerprint of a book config — the Live Gate attaches to
    (book_id, config_hash), the multi-book extension of ADR-0003."""
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


def _ensure_schema_sync(database_url: str) -> None:
    """Create or verify the schema directly from the models. Sync — call via
    asyncio.to_thread.

    Pre-launch policy (#94, decided 2026-08-18): until the first real paper
    fill exists there is no data worth migrating, so there are no migrations —
    the models ARE the schema. create_all is additive (new tables appear
    automatically); a column-level mismatch on an existing table means the
    database predates a schema change and must be deleted by hand. Nothing
    here ever drops or alters existing data.

    Migrations return the day the first paper fill lands — from then on the
    fills/gate/audit tables are Live Gate evidence (ADR-0006) and can never
    be reset.
    """
    from sqlalchemy import create_engine, inspect

    sync_url = database_url.replace("sqlite+aiosqlite://", "sqlite://")
    sync_engine = create_engine(sync_url)
    try:
        Base.metadata.create_all(sync_engine)  # additive: creates missing tables only
        inspector = inspect(sync_engine)
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in inspector.get_columns(table.name)}
            want = {c.name for c in table.columns}
            if not want <= have:
                missing = ", ".join(sorted(want - have))
                raise RuntimeError(
                    f"Database schema is stale: table {table.name!r} is missing column(s) {missing}. "
                    "Pre-launch databases hold no real data — delete the database file and restart "
                    "to regenerate it from the current models."
                )
    finally:
        sync_engine.dispose()


async def init_db(force_seed: bool = False):
    await asyncio.to_thread(_ensure_schema_sync, DATABASE_URL)

    async with async_session_maker() as session:
        # Check if config exists
        config_result = await session.execute(select(PortfolioConfigModel))
        config = config_result.scalar_one_or_none()

        if config is None or force_seed:
            if config:
                await session.delete(config)
            new_config = PortfolioConfigModel(
                id=1,
                account=SEED_PORTFOLIO_CONFIG["account"],
                risk_profile=SEED_PORTFOLIO_CONFIG["risk_profile"],
                portfolio_greek_limits=SEED_PORTFOLIO_CONFIG["portfolio_greek_limits"],
            )
            session.add(new_config)

        # Positions are NOT seeded — real databases start empty (#53).
        # SEED_POSITIONS above exists only for test fixtures.

        # Lab books B01–B06 (#69): the six-book allocation decided 2026-08-18 —
        # regime variants V0/V1/V2 crossed with underlyings XSP/SPY, identical
        # playbook mixes and envelopes. Each book gets its own trading-control
        # row (a book without one is halted fail-closed, ADR-0008). B07–B10
        # stay unseeded, reserved for second-generation experiments.
        for book_id, variant, underlying in LAB_BOOKS:
            if await session.get(BookModel, book_id) is None:
                config = {"engine_variant": variant, "underlying": underlying, "envelope": {}}
                session.add(
                    BookModel(
                        id=book_id,
                        name=f"{variant} on {underlying}",
                        config=config,
                        config_version=1,
                        config_hash=_config_hash(config),
                        starting_capital=10000.0,
                        cash_balance=10000.0,
                        status="ACTIVE",
                        created_at=datetime.now(UTC).isoformat(),
                    )
                )
            if await session.get(TradingControlModel, book_id) is None:
                session.add(
                    TradingControlModel(
                        scope=book_id,
                        state="ACTIVE",
                        reason="Initial state",
                        actor="system",
                        changed_at=datetime.now(UTC).isoformat(),
                    )
                )

        # Executor bootstrap rows. The migration inserts these for existing
        # databases; this covers the fresh-DB create_all path (#61).
        if await session.get(BookModel, "B00") is None:
            session.add(
                BookModel(
                    id="B00",
                    name="Legacy — pre-executor manual positions",
                    config={},
                    config_version=1,
                    config_hash="",
                    starting_capital=10000.0,
                    cash_balance=10000.0,
                    status="LEGACY",
                    created_at=datetime.now(UTC).isoformat(),
                )
            )
        if await session.get(TradingControlModel, "GLOBAL") is None:
            session.add(
                TradingControlModel(
                    scope="GLOBAL",
                    state="ACTIVE",
                    reason="Initial state",
                    actor="system",
                    changed_at=datetime.now(UTC).isoformat(),
                )
            )

        # Seed playbooks
        pb_result = await session.execute(select(PlaybookDefinitionModel))
        existing_playbooks = pb_result.scalars().all()
        if not existing_playbooks or force_seed:
            for pb in existing_playbooks:
                await session.delete(pb)
            for pb_data in SEED_PLAYBOOKS:
                session.add(
                    PlaybookDefinitionModel(
                        id=pb_data["id"],
                        version=pb_data["version"],
                        name=pb_data["name"],
                        underlying_ticker=pb_data["underlying_ticker"],
                        strategy_type=pb_data["strategy_type"],
                        execution_mode=pb_data["execution_mode"],
                        enabled=pb_data.get("enabled", True),
                        entry_filters=pb_data["entry_filters"],
                        execution_specs=pb_data["execution_specs"],
                        exit_rules=pb_data["exit_rules"],
                    )
                )

        # Check if market state exists
        mstate_result = await session.execute(select(MarketStateModel))
        mstate = mstate_result.scalar_one_or_none()
        if mstate is None or force_seed:
            if mstate:
                await session.delete(mstate)
            # Seed telemetry values (June 2026 baseline)
            _spy_price = 758.0
            _spy_sma20 = 750.0
            _vix_close = 14.5
            _ivrs = {"SPY": 25.0}
            _daily_return = 0.005  # +0.5 %
            _catalyst_dates = ["2026-06-08"]
            _regime, _scores = compute_regime(
                spy_price=_spy_price,
                spy_sma20=_spy_sma20,
                vix_close=_vix_close,
                underlying_ivrs=_ivrs,
                spy_daily_return=_daily_return,
                catalyst_dates=_catalyst_dates,
            )
            new_mstate = MarketStateModel(
                id=1,
                current_regime=_regime,
                spy_price=_spy_price,
                spy_sma20=_spy_sma20,
                vix_close=_vix_close,
                underlying_ivrs=_ivrs,
                spy_daily_return=_daily_return,
                catalyst_dates=_catalyst_dates,
                regime_scores={k: float(v) for k, v in _scores.items()},
            )
            session.add(new_mstate)

        await session.commit()
