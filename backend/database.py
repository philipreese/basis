import os
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select

from backend.models import Base, PortfolioConfigModel, PositionModel, PlaybookDefinitionModel, MarketStateModel, ClosurePostMortemModel, OpportunityRecordModel
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
        "execution_mode": "PAPER"
    },
    "risk_profile": {
        "max_trade_risk_pct": 15.0,
        "max_trade_risk_dollars": 1500.0,
        "max_underlying_concentration_pct": 35.0,
        "max_correlated_index_pct": 50.0,
        "minimum_cash_reserve_pct": 15.0,
        "max_simultaneous_positions": 3,
        "max_capital_deployed_pct": 85.0
    },
    "portfolio_greek_limits": {
        "max_net_delta": 50.0,
        "max_net_vega": 100.0,
        "max_net_gamma": 10.0
    }
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
            "min_ivr": 50.0, "max_ivr": 100.0,
            "vix_range": [15.0, 35.0],
            "required_trend": "ANY",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.16,
            "long_leg_delta": 0.05,
            "spread_width_dollars": 5.0,
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
            "min_ivr": 20.0, "max_ivr": 60.0,
            "vix_range": [10.0, 25.0],
            "required_trend": "ABOVE_SMA20",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 45,
            "short_leg_delta": 0.25,
            "long_leg_delta": 0.50,
            "spread_width_dollars": 10.0,
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
            "min_ivr": 20.0, "max_ivr": 70.0,
            "vix_range": [15.0, 40.0],
            "required_trend": "BELOW_SMA20",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 45,
            "short_leg_delta": 0.25,
            "long_leg_delta": 0.50,
            "spread_width_dollars": 10.0,
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
            "min_ivr": 20.0, "max_ivr": 100.0,
            "vix_range": [10.0, 30.0],
            "required_trend": "ABOVE_SMA20",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.30,
            "long_leg_delta": 0.10,
            "spread_width_dollars": 5.0,
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
            "min_ivr": 25.0, "max_ivr": 100.0,
            "vix_range": [15.0, 45.0],
            "required_trend": "BELOW_SMA20",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.30,
            "long_leg_delta": 0.10,
            "spread_width_dollars": 5.0,
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
            "min_ivr": 30.0, "max_ivr": 100.0,
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
            "min_ivr": 30.0, "max_ivr": 100.0,
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

SEED_POSITIONS = [
    {
        "id": "seed_pos_spy_straddle_jun18",
        "underlying": "SPY",
        "strategy_type": "LONG_STRADDLE",
        "execution_mode": "PAPER",
        "legs": [
            { "option_type": "CALL", "direction": "LONG", "strike": 759.0, "expiration": "2026-06-18", "delta": 0.5, "theta": -0.1, "vega": 0.2, "gamma": 0.05 },
            { "option_type": "PUT",  "direction": "LONG", "strike": 759.0, "expiration": "2026-06-18", "delta": -0.5, "theta": -0.1, "vega": 0.2, "gamma": 0.05 }
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
            "pre_trade_confidence_rating": 3
        }
    },
    {
        "id": "seed_pos_spy_straddle_jul18",
        "underlying": "SPY",
        "strategy_type": "LONG_STRADDLE",
        "execution_mode": "PAPER",
        "legs": [
            { "option_type": "CALL", "direction": "LONG", "strike": 757.0, "expiration": "2026-07-18", "delta": 0.5, "theta": -0.05, "vega": 0.3, "gamma": 0.03 },
            { "option_type": "PUT",  "direction": "LONG", "strike": 757.0, "expiration": "2026-07-18", "delta": -0.5, "theta": -0.05, "vega": 0.3, "gamma": 0.03 }
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
            "pre_trade_confidence_rating": 4
        }
    }
]

async def _needs_migration(conn) -> bool:
    """Return True if schema is missing any required table or column."""
    from sqlalchemy import text
    try:
        # Sprint 3 check: market_state extended columns
        result = await conn.execute(text("PRAGMA table_info(market_state)"))
        columns = {row[1] for row in result.fetchall()}
        if "spy_sma20" not in columns:
            return True
        # Sprint 4 check: playbooks table exists
        result2 = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='playbooks'"))
        if result2.fetchone() is None:
            return True
        # Sprint 5 check: closure_post_mortems table
        result3 = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='closure_post_mortems'"))
        if result3.fetchone() is None:
            return True
        # Sprint 5 check: opportunity_records table
        result4 = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='opportunity_records'"))
        if result4.fetchone() is None:
            return True
        # Sprint 5 check: warnings_acknowledged column on positions
        result5 = await conn.execute(text("PRAGMA table_info(positions)"))
        pos_columns = {row[1] for row in result5.fetchall()}
        if "warnings_acknowledged" not in pos_columns:
            return True
        # Credit-spread playbooks check: enabled column on playbooks
        result6 = await conn.execute(text("PRAGMA table_info(playbooks)"))
        pb_columns = {row[1] for row in result6.fetchall()}
        if "enabled" not in pb_columns:
            return True
        return False
    except Exception:
        return True


async def init_db(force_seed: bool = False):
    async with engine.begin() as conn:
        if await _needs_migration(conn):
            # Schema is stale — drop everything and recreate
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


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
                portfolio_greek_limits=SEED_PORTFOLIO_CONFIG["portfolio_greek_limits"]
            )
            session.add(new_config)

        # Check if positions exist
        pos_result = await session.execute(select(PositionModel))
        positions = pos_result.scalars().all()
        
        if not positions or force_seed:
            for p in positions:
                await session.delete(p)
            for p_data in SEED_POSITIONS:
                # Add default playbook_snapshot dummy or empty values if not provided
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

        # Seed playbooks
        pb_result = await session.execute(select(PlaybookDefinitionModel))
        existing_playbooks = pb_result.scalars().all()
        if not existing_playbooks or force_seed:
            for pb in existing_playbooks:
                await session.delete(pb)
            for pb_data in SEED_PLAYBOOKS:
                session.add(PlaybookDefinitionModel(
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
                ))

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
