import asyncio
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


def _install_sqlite_pragmas(sync_engine) -> None:
    """WAL + busy_timeout on every connection (#271): the console server and
    a scheduled entrypoint can hold the database at the same moment, and
    SQLite's defaults (rollback journal, zero busy timeout) turn that overlap
    into an immediate 'database is locked' crash instead of a short wait."""
    from sqlalchemy import event as sa_event

    @sa_event.listens_for(sync_engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial glue
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


engine = create_async_engine(DATABASE_URL)
if DATABASE_URL.startswith("sqlite"):
    _install_sqlite_pragmas(engine.sync_engine)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


# Seed data and the lab-book matrix live in backend/seeds.py (#149);
# re-exported here so existing imports keep working.
from backend.seeds import (  # noqa: F401
    LAB_BOOKS,
    SEED_PLAYBOOKS,
    SEED_PORTFOLIO_CONFIG,
    SEED_POSITIONS,
    _config_hash,
)


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
    if sync_url.startswith("sqlite"):
        _install_sqlite_pragmas(sync_engine)
    try:
        Base.metadata.create_all(sync_engine)  # additive: creates missing tables only
        inspector = inspect(sync_engine)
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in inspector.get_columns(table.name)}
            missing_cols = [c for c in table.columns if c.name not in have]
            for col in missing_cols:
                # The delete-and-restart policy (#94) died with the first real
                # fill — the database is Live Gate evidence now (#280).
                # Nullable/defaulted columns are added in place; anything
                # stricter still fails loudly rather than guessing.
                if not (col.nullable or col.server_default is not None):
                    raise RuntimeError(
                        f"Database schema is stale: table {table.name!r} is missing NON-NULLABLE column "
                        f"{col.name!r} with no server default — this needs a hand-written migration; "
                        "the database holds Live Gate evidence and must never be deleted."
                    )
                from sqlalchemy.schema import CreateColumn

                ddl = CreateColumn(col).compile(dialect=sync_engine.dialect)
                with sync_engine.begin() as conn:
                    conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {ddl}")
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

        # Lab books (ADR-0009, #136): the experiment matrix — every book one
        # question. Each book gets its own trading-control row (a book without
        # one is halted fail-closed, ADR-0008).
        for spec in LAB_BOOKS:
            book_id = spec["id"]
            if await session.get(BookModel, book_id) is None:
                session.add(
                    BookModel(
                        id=book_id,
                        name=spec["name"],
                        config=spec["config"],
                        config_version=1,
                        config_hash=_config_hash(spec["config"]),
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
