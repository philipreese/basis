import asyncio
import logging
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

# Trading-mode isolation (ADR-0006, #204): PAPER and LIVE are different
# universes with different evidence, and the paper lab keeps running
# ALONGSIDE live once live exists — so each mode gets its own database file,
# and every database is stamped with the mode that created it. A process in
# one mode refuses a database stamped with the other, hard.
TRADING_MODE = os.getenv("IBKR_TRADING_MODE", "paper").strip().lower()
if TRADING_MODE not in ("paper", "live"):
    raise RuntimeError(f"IBKR_TRADING_MODE must be 'paper' or 'live', got {TRADING_MODE!r}")


def default_database_url(mode: str) -> str:
    return "sqlite+aiosqlite:///basis.db" if mode == "paper" else "sqlite+aiosqlite:///basis.live.db"


DATABASE_URL = os.getenv("DATABASE_URL", default_database_url(TRADING_MODE))

# One-time file rename (#313): the databases carried the project's pre-basis
# name. A URL that points at the NEW name while only the OLD file exists gets
# the file moved under it — an explicit DATABASE_URL naming the old file is
# respected untouched.
_LEGACY_DATABASE_FILES = {"basis.db": "options_playbook.db", "basis.live.db": "options_playbook.live.db"}


def _migrate_legacy_database_file(url: str) -> tuple[str, str] | None:
    """Move the legacy-named database (and -wal/-shm siblings) under the new
    name. Returns ("renamed", legacy_name) on success, ("locked", legacy_name)
    when another process holds the file (#340 — on Windows a RUNNING console
    server makes the rename raise PermissionError; the caller must then use
    the LEGACY file rather than let the engine create an empty new one), and
    None when there is nothing to do.

    The three renames are attempted main-file first — the file a live
    process holds — so a lock fails before anything moved; a partial move
    (AV/sync tools grabbing a sibling) is rolled back rather than splitting
    the WAL from its database."""
    if not url.startswith("sqlite+aiosqlite:///"):
        return None
    from pathlib import Path

    new_path = Path(url.removeprefix("sqlite+aiosqlite:///"))
    legacy_name = _LEGACY_DATABASE_FILES.get(new_path.name)
    if legacy_name is None:
        return None
    legacy_path = new_path.with_name(legacy_name)
    if not legacy_path.exists() or new_path.exists():
        return None
    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in ("", "-wal", "-shm"):
            src = legacy_path.with_name(legacy_path.name + suffix)
            if src.exists():
                dst = new_path.with_name(new_path.name + suffix)
                src.rename(dst)
                moved.append((src, dst))
    except OSError:
        if new_path.exists() and not legacy_path.exists() and not moved:
            # Concurrent-import race (#340): another process completed the
            # move between our exists() check and the rename — the new file
            # is real, proceed against it.
            return None
        for src, dst in reversed(moved):
            try:
                dst.rename(src)
            except OSError:  # pragma: no cover - double-fault, keep going
                logging.getLogger(__name__).critical("DB rename rollback failed for %s", dst)
        return ("locked", legacy_name)
    return ("renamed", legacy_name)


_RENAMED_FROM: str | None = None
_migration = _migrate_legacy_database_file(DATABASE_URL)
if _migration is not None:
    _status, _legacy = _migration
    if _status == "renamed":
        _RENAMED_FROM = _legacy
    else:
        # The legacy file is held open (a running console server, most
        # likely). Falling through to the new URL would make the engine
        # CREATE AN EMPTY DATABASE and run the night against it — instead
        # this process uses the legacy file; the rename retries on the next
        # process start once the holder restarts.
        logging.getLogger(__name__).warning(
            "Legacy database %s is locked by another process — running against it under its old name; "
            "the rename to %s will retry on the next start.",
            _legacy,
            DATABASE_URL.rpartition("/")[2],
        )
        DATABASE_URL = DATABASE_URL.rpartition("/")[0] + "/" + _legacy


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
        backed_up = False
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in inspector.get_columns(table.name)}
            missing_cols = [c for c in table.columns if c.name not in have]
            if missing_cols and not backed_up:
                _backup_before_migration(sync_url)
                backed_up = True
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


def _backup_before_migration(sync_url: str) -> None:
    """ADR-0006: the database is backed up before any schema migration —
    an ALTER that goes sideways must never be the end of the evidence."""
    import shutil
    from pathlib import Path

    if not sync_url.startswith("sqlite:///"):
        return
    db_path = Path(sync_url.removeprefix("sqlite:///"))
    if db_path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        shutil.copy2(db_path, db_path.with_name(f"{db_path.name}.pre-migration-{stamp}.bak"))


async def _assert_trading_mode_stamp(session_maker=None, mode: str | None = None) -> None:
    """Refuse a database stamped with the OTHER mode (#204). First open of a
    fresh database stamps it with the process's mode."""
    from backend.models import DbMetaModel

    session_maker = session_maker or async_session_maker
    mode = mode or TRADING_MODE
    async with session_maker() as session:
        row = await session.get(DbMetaModel, "trading_mode")
        if row is None:
            session.add(DbMetaModel(key="trading_mode", value=mode))
            await session.commit()
        elif row.value != mode:
            raise RuntimeError(
                f"Trading-mode mismatch: this database is stamped {row.value!r} but the process is running "
                f"in {mode!r} mode (IBKR_TRADING_MODE). Paper and live evidence never share a file "
                "(ADR-0006, #204) — point DATABASE_URL at the correct mode's database."
            )


async def init_db(force_seed: bool = False):
    global _RENAMED_FROM
    await asyncio.to_thread(_ensure_schema_sync, DATABASE_URL)
    await _assert_trading_mode_stamp()

    if _RENAMED_FROM:
        from backend.models import AuditEventModel

        async with async_session_maker() as session:
            session.add(
                AuditEventModel(
                    run_at=datetime.now(UTC).isoformat(),
                    book_id=None,
                    event_type="DATABASE_RENAMED",
                    actor="system",
                    payload={"from": _RENAMED_FROM, "to": DATABASE_URL.rpartition("/")[2]},
                )
            )
            await session.commit()
        _RENAMED_FROM = None

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
