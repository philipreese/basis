"""Tests for the pre-launch schema bootstrap in backend.database (#94).

Policy: until the first real paper fill exists there are no migrations —
the models ARE the schema. These tests pin the three behaviors that make
that safe: fresh databases materialize completely, new tables appear
additively on existing databases, and a column-level mismatch refuses to
run (with a delete-and-restart instruction) instead of guessing. The
destructive drop_all heuristic stays dead.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from backend.database import _ensure_schema_sync
from backend.models import Base

EXPECTED_TABLES = {
    "closure_post_mortems",
    "market_state",
    "opportunity_records",
    "playbooks",
    "portfolio_config",
    "positions",
    "books",
    "orders",
    "fills",
    "trading_control",
    "audit_events",
    "gate_events",
    "reconciliation_runs",
    "regime_readings",
    "index_history",
}


def _url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


def _tables(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


class TestFreshDatabase:
    def test_creates_full_schema(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.db"
        _ensure_schema_sync(_url(db))
        assert EXPECTED_TABLES <= _tables(db)

    def test_rerun_is_idempotent(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.db"
        _ensure_schema_sync(_url(db))
        _ensure_schema_sync(_url(db))
        assert EXPECTED_TABLES <= _tables(db)


class TestExistingDatabase:
    def test_new_tables_appear_additively_and_data_survives(self, tmp_path: Path) -> None:
        db = tmp_path / "partial.db"
        # An older database: create the full current schema, then drop one
        # table and add a data row to prove additive re-sync preserves it.
        engine = create_engine(f"sqlite:///{db.as_posix()}")
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("DROP TABLE index_history")
            conn.execute(
                "INSERT INTO closure_post_mortems"
                " (id, position_id, outcome, realized_pnl, actual_underlying_move_pct,"
                "  exit_date, exit_trigger, lesson_tags, user_override_logged)"
                " VALUES ('pm1', 'pos1', 'WIN', 120.5, 1.8,"
                "  '2026-08-01', 'PROFIT_TARGET', '[]', 0)"
            )
            conn.commit()
        _ensure_schema_sync(_url(db))
        assert "index_history" in _tables(db)  # recreated additively
        with closing(sqlite3.connect(db)) as conn:
            rows = conn.execute("SELECT id FROM closure_post_mortems").fetchall()
        assert rows == [("pm1",)]  # existing data untouched

    def test_missing_column_refuses_with_actionable_error(self, tmp_path: Path) -> None:
        db = tmp_path / "stale.db"
        engine = create_engine(f"sqlite:///{db.as_posix()}")
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
        with closing(sqlite3.connect(db)) as conn:
            # Simulate a pre-schema-change database: positions without book_id.
            conn.executescript(
                "CREATE TABLE positions_old AS SELECT id, underlying FROM positions;"
                "DROP TABLE positions;"
                "ALTER TABLE positions_old RENAME TO positions;"
            )
        with pytest.raises(RuntimeError, match="delete the database file"):
            _ensure_schema_sync(_url(db))


class TestNoDestructivePath:
    def test_drop_all_is_gone(self) -> None:
        """The destructive heuristic must never come back."""
        source = Path("backend/database.py").read_text(encoding="utf-8")
        assert "drop_all" not in source
        assert "_needs_migration" not in source
