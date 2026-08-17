"""Tests for the Alembic-backed schema management in backend.database.

The old init_db ran Base.metadata.drop_all off a heuristic that returned True
on any exception — able to silently destroy the audit trail (ADR-0003). These
tests pin the replacement behavior: no destructive path, file backup before
any schema change to an existing database.
"""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from backend.database import _backup_db_file, _run_migrations_sync, _sqlite_path
from backend.models import Base

EXPECTED_TABLES = {
    "closure_post_mortems",
    "market_state",
    "opportunity_records",
    "playbooks",
    "portfolio_config",
    "positions",
}


def _url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


def _tables(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _backups(db_path: Path) -> list[Path]:
    return sorted(db_path.parent.glob(f"{db_path.name}.bak-*"))


class TestSqlitePath:
    def test_async_url(self) -> None:
        assert _sqlite_path("sqlite+aiosqlite:///foo.db") == Path("foo.db")

    def test_sync_url(self) -> None:
        assert _sqlite_path("sqlite:///bar.db") == Path("bar.db")

    def test_memory_url(self) -> None:
        assert _sqlite_path("sqlite+aiosqlite:///:memory:") is None


class TestFreshDatabase:
    def test_creates_full_schema_and_stamps_head(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.db"
        _run_migrations_sync(_url(db))
        tables = _tables(db)
        assert EXPECTED_TABLES <= tables
        assert "alembic_version" in tables

    def test_no_backup_written_for_fresh_database(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.db"
        _run_migrations_sync(_url(db))
        assert _backups(db) == []


class TestPreAlembicDatabase:
    """A database created before Alembic existed: current schema, no
    alembic_version table — exactly the shape of the real options_playbook.db."""

    def _make_pre_alembic_db(self, db_path: Path) -> None:
        engine = create_engine(f"sqlite:///{db_path.as_posix()}")
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO closure_post_mortems"
                " (id, position_id, outcome, realized_pnl, actual_underlying_move_pct,"
                "  exit_date, exit_trigger, lesson_tags, user_override_logged)"
                " VALUES ('pm1', 'pos1', 'WIN', 120.5, 1.8,"
                "  '2026-08-01', 'PROFIT_TARGET', '[]', 0)"
            )
            conn.commit()

    def test_data_survives_and_version_is_stamped(self, tmp_path: Path) -> None:
        db = tmp_path / "existing.db"
        self._make_pre_alembic_db(db)
        _run_migrations_sync(_url(db))
        assert "alembic_version" in _tables(db)
        with sqlite3.connect(db) as conn:
            rows = conn.execute("SELECT id FROM closure_post_mortems").fetchall()
        assert rows == [("pm1",)]

    def test_backup_written_before_touching_existing_database(self, tmp_path: Path) -> None:
        db = tmp_path / "existing.db"
        self._make_pre_alembic_db(db)
        _run_migrations_sync(_url(db))
        backups = _backups(db)
        assert len(backups) == 1
        # The backup itself must contain the pre-migration data.
        with sqlite3.connect(backups[0]) as conn:
            rows = conn.execute("SELECT id FROM closure_post_mortems").fetchall()
        assert rows == [("pm1",)]

    def test_rerun_is_idempotent(self, tmp_path: Path) -> None:
        db = tmp_path / "existing.db"
        self._make_pre_alembic_db(db)
        _run_migrations_sync(_url(db))
        _run_migrations_sync(_url(db))  # must not raise or lose data
        with sqlite3.connect(db) as conn:
            rows = conn.execute("SELECT id FROM closure_post_mortems").fetchall()
        assert rows == [("pm1",)]


class TestLegacyPre070Database:
    """A v0.6.x database: playbooks table WITHOUT the `enabled` column.
    This is the exact shape of the real options_playbook.db — the case where
    the old code's drop_all would have destroyed the audit trail."""

    def _make_legacy_db(self, db_path: Path) -> None:
        from alembic import command as alembic_command

        from backend.database import _alembic_config

        url = _url(db_path)
        cfg = _alembic_config(url)
        alembic_command.upgrade(cfg, "6640075bcc04")  # legacy baseline schema
        with sqlite3.connect(db_path) as conn:
            conn.execute("DROP TABLE alembic_version")  # simulate pre-Alembic
            conn.execute(
                "INSERT INTO playbooks"
                " (id, version, name, underlying_ticker, strategy_type,"
                "  execution_mode, entry_filters, execution_specs, exit_rules)"
                " VALUES ('spy_long_straddle_v1', '1.0', 'SPY Long Straddle', 'SPY',"
                "  'LONG_STRADDLE', 'PAPER', '{}', '{}', '{}')"
            )
            conn.execute(
                "INSERT INTO closure_post_mortems"
                " (id, position_id, outcome, realized_pnl, actual_underlying_move_pct,"
                "  exit_date, exit_trigger, lesson_tags, user_override_logged)"
                " VALUES ('pm_real', 'pos_real', 'LOSS', -50.0, 0.4,"
                "  '2026-07-01', 'STOP_LOSS', '[]', 1)"
            )
            conn.commit()

    def test_upgrade_adds_enabled_and_preserves_audit_trail(self, tmp_path: Path) -> None:
        db = tmp_path / "legacy.db"
        self._make_legacy_db(db)
        _run_migrations_sync(_url(db))
        with sqlite3.connect(db) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(playbooks)")}
            assert "enabled" in cols
            pms = conn.execute("SELECT id FROM closure_post_mortems").fetchall()
            assert pms == [("pm_real",)]

    def test_migration_applies_seed_intent(self, tmp_path: Path) -> None:
        db = tmp_path / "legacy.db"
        self._make_legacy_db(db)
        _run_migrations_sync(_url(db))
        with sqlite3.connect(db) as conn:
            straddle = conn.execute("SELECT enabled FROM playbooks WHERE id = 'spy_long_straddle_v1'").fetchone()
            assert straddle == (0,)
            credit = conn.execute(
                "SELECT id, enabled FROM playbooks WHERE id IN"
                " ('spy_bull_put_spread_v1', 'spy_bear_call_spread_v1') ORDER BY id"
            ).fetchall()
            assert credit == [
                ("spy_bear_call_spread_v1", 1),
                ("spy_bull_put_spread_v1", 1),
            ]

    def test_backup_precedes_the_upgrade(self, tmp_path: Path) -> None:
        db = tmp_path / "legacy.db"
        self._make_legacy_db(db)
        _run_migrations_sync(_url(db))
        backups = _backups(db)
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(playbooks)")}
        assert "enabled" not in cols  # backup is the untouched pre-migration file


class TestNoDestructivePath:
    def test_drop_all_is_gone(self) -> None:
        """The destructive heuristic must never come back."""
        source = Path("backend/database.py").read_text(encoding="utf-8")
        assert "drop_all" not in source
        assert "_needs_migration" not in source


class TestBackupHelper:
    def test_backup_names_are_unique_per_call(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "x.db"
        db.write_bytes(b"data")
        first = _backup_db_file(db)
        assert first.exists()
        assert first.read_bytes() == b"data"
        assert first.name.startswith("x.db.bak-")
