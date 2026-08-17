"""Tests for the Executor (Paper) multi-book schema (#61).

Pins three properties: the migration chain and Base.metadata produce the same
schema (fresh DBs take the create_all + stamp-head shortcut, so drift between
the two paths would split the world in half); upgrading an existing database
backfills positions into the B00 legacy book and seeds the control rows; and
the append-only tables reject UPDATE/DELETE at the ORM layer — they are the
Live Gate's evidence (ADR-0006).
"""

import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command as alembic_command
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import _alembic_config, _run_migrations_sync
from backend.models import (
    AppendOnlyViolationError,
    AuditEventModel,
    Base,
    FillModel,
    GateEventModel,
)

EXECUTOR_TABLES = {
    "books",
    "orders",
    "fills",
    "reconciliation_runs",
    "gate_events",
    "audit_events",
    "trading_control",
    "regime_readings",
    "index_history",
}


def _url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


def _schema_map(db_path: Path) -> dict[str, set[str]]:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        insp = inspect(engine)
        return {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names() if t != "alembic_version"}
    finally:
        engine.dispose()


class TestMigrationMetadataParity:
    def test_full_migration_chain_matches_create_all(self, tmp_path: Path) -> None:
        migrated = tmp_path / "migrated.db"
        alembic_command.upgrade(_alembic_config(_url(migrated)), "head")

        fresh = tmp_path / "fresh.db"
        engine = create_engine(f"sqlite:///{fresh.as_posix()}")
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()

        assert _schema_map(migrated) == _schema_map(fresh)

    def test_executor_tables_exist_on_fresh_database(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.db"
        _run_migrations_sync(_url(db))
        assert EXECUTOR_TABLES <= set(_schema_map(db))


class TestUpgradeFromPre061Schema:
    """A database at the previous head (b7f2e4a9c1d0) with an existing position."""

    def _make_db(self, db_path: Path) -> None:
        alembic_command.upgrade(_alembic_config(_url(db_path)), "b7f2e4a9c1d0")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO positions"
                " (id, underlying, strategy_type, execution_mode, legs, entry_date,"
                "  expiration_date, entry_premium, premium_direction,"
                "  current_value_per_share, contracts, max_profit, max_loss,"
                "  notes, rolls, status)"
                " VALUES ('pos1', 'SPY', 'IRON_CONDOR', 'PAPER', '[]', '2026-08-01',"
                "  '2026-09-18', 1.5, 'CREDIT', 1.5, 1, 1.5, 3.5, '', 0, 'OPEN')"
            )
            conn.commit()

    def test_existing_positions_backfilled_into_legacy_book(self, tmp_path: Path) -> None:
        db = tmp_path / "existing.db"
        self._make_db(db)
        _run_migrations_sync(_url(db))
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT book_id FROM positions WHERE id = 'pos1'").fetchone() == ("B00",)
            assert conn.execute("SELECT status FROM books WHERE id = 'B00'").fetchone() == ("LEGACY",)

    def test_global_trading_control_seeded_active(self, tmp_path: Path) -> None:
        db = tmp_path / "existing.db"
        self._make_db(db)
        _run_migrations_sync(_url(db))
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT state FROM trading_control WHERE scope = 'GLOBAL'").fetchone()
        assert row == ("ACTIVE",)

    def test_create_all_database_without_stamp_is_detected(self, tmp_path: Path) -> None:
        """A DB built by create_all (has books) but missing alembic_version must
        be stamped at the executor revision, not re-migrated into a crash."""
        db = tmp_path / "unstamped.db"
        engine = create_engine(f"sqlite:///{db.as_posix()}")
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
        _run_migrations_sync(_url(db))  # must not raise "table books already exists"
        with sqlite3.connect(db) as conn:
            rev = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert rev is not None


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield maker
    await engine.dispose()


def _fill(exec_id: str = "0001.abc.01") -> FillModel:
    return FillModel(
        exec_id=exec_id,
        order_id="o_1",
        book_id="B01",
        con_id=123,
        side="BOT",
        quantity=1.0,
        price=1.25,
        commission=1.1,
        fill_time="2026-08-18T22:00:00+00:00",
        raw={},
    )


class TestAppendOnlyEnforcement:
    @pytest.mark.asyncio
    async def test_insert_is_allowed(self, session_maker) -> None:
        async with session_maker() as session:
            session.add(_fill())
            await session.commit()
            rows = (await session.execute(select(FillModel))).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_update_is_rejected(self, session_maker) -> None:
        async with session_maker() as session:
            session.add(_fill())
            await session.commit()
            fill = (await session.execute(select(FillModel))).scalar_one()
            fill.price = 99.0
            with pytest.raises(AppendOnlyViolationError, match="UPDATE rejected"):
                await session.commit()

    @pytest.mark.asyncio
    async def test_delete_is_rejected(self, session_maker) -> None:
        async with session_maker() as session:
            session.add(_fill())
            await session.commit()
            fill = (await session.execute(select(FillModel))).scalar_one()
            await session.delete(fill)
            with pytest.raises(AppendOnlyViolationError, match="DELETE rejected"):
                await session.commit()

    @pytest.mark.asyncio
    async def test_gate_and_audit_events_are_append_only(self, session_maker) -> None:
        async with session_maker() as session:
            gate = GateEventModel(book_id="B01", run_at="2026-08-18", gate="MAX_POSITIONS", result="PASS", context={})
            audit = AuditEventModel(
                run_at="2026-08-18", book_id=None, event_type="ORDER_STAGED", actor="executor", payload={}
            )
            session.add_all([gate, audit])
            await session.commit()

            gate.result = "BLOCK"
            with pytest.raises(AppendOnlyViolationError):
                await session.commit()
            await session.rollback()

            audit_row = (await session.execute(select(AuditEventModel))).scalar_one()
            await session.delete(audit_row)
            with pytest.raises(AppendOnlyViolationError):
                await session.commit()
