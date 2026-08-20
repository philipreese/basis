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

    def test_missing_required_column_refuses_with_actionable_error(self, tmp_path: Path) -> None:
        # #280: nullable/defaulted columns migrate additively (the database is
        # Live Gate evidence now — never deleted); a missing NON-NULLABLE
        # column with no default still fails loudly.
        db = tmp_path / "stale.db"
        engine = create_engine(f"sqlite:///{db.as_posix()}")
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
        with closing(sqlite3.connect(db)) as conn:
            # Simulate a much older database: positions without strategy_type.
            conn.executescript(
                "CREATE TABLE positions_old AS SELECT id, underlying FROM positions;"
                "DROP TABLE positions;"
                "ALTER TABLE positions_old RENAME TO positions;"
            )
        with pytest.raises(RuntimeError, match="hand-written migration"):
            _ensure_schema_sync(_url(db))


class TestClosurePostMortemUniqueIndex:
    """#463 (Audit II R3 F3): duplicate position_id must be impossible on a
    fresh database, and the index must be backfilled onto an existing one
    that predates it (SQLite has no ADD CONSTRAINT — this is not covered by
    the missing-column loop above)."""

    def _insert_post_mortem(self, conn: sqlite3.Connection, pm_id: str, position_id: str) -> None:
        conn.execute(
            "INSERT INTO closure_post_mortems"
            " (id, position_id, outcome, realized_pnl, actual_underlying_move_pct,"
            "  exit_date, exit_trigger, lesson_tags, user_override_logged)"
            " VALUES (?, ?, 'WIN', 1.0, 0.0, '2026-08-01', 'MANUAL', '[]', 0)",
            (pm_id, position_id),
        )

    def test_fresh_database_rejects_a_duplicate_position_id(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.db"
        _ensure_schema_sync(_url(db))
        with closing(sqlite3.connect(db)) as conn:
            self._insert_post_mortem(conn, "pm1", "pos1")
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                self._insert_post_mortem(conn, "pm2", "pos1")

    def test_existing_database_predating_the_index_gets_it_backfilled(self, tmp_path: Path) -> None:
        db = tmp_path / "old.db"
        # An older database: the table exists (as it always has) but without
        # the unique index this migration adds.
        with closing(sqlite3.connect(db)) as conn:
            conn.executescript(
                "CREATE TABLE closure_post_mortems ("
                " id TEXT PRIMARY KEY, position_id TEXT, outcome TEXT, realized_pnl REAL,"
                " actual_underlying_move_pct REAL, exit_date TEXT, exit_trigger TEXT,"
                " lesson_tags TEXT, user_override_logged INTEGER,"
                " playbook_id TEXT, playbook_version TEXT);"
            )
            self._insert_post_mortem(conn, "pm1", "pos1")
            conn.commit()
        _ensure_schema_sync(_url(db))
        with closing(sqlite3.connect(db)) as conn:
            index_names = {row[1] for row in conn.execute("PRAGMA index_list(closure_post_mortems)")}
            assert "ix_closure_post_mortems_position_id" in index_names
            rows = conn.execute("SELECT id FROM closure_post_mortems").fetchall()
            assert rows == [("pm1",)]  # existing data untouched
            with pytest.raises(sqlite3.IntegrityError):
                self._insert_post_mortem(conn, "pm2", "pos1")

    def test_preexisting_duplicates_are_quarantined_not_bricking(self, tmp_path: Path) -> None:
        # Audit II R4 (#532): raising on pre-existing duplicates bricked
        # every entrypoint INCLUDING the console — the only sanctioned
        # repair path. Surplus rows are quarantined into the audit trail
        # (earliest per position kept) and the index still lands.
        db = tmp_path / "dupes.db"
        with closing(sqlite3.connect(db)) as conn:
            conn.executescript(
                "CREATE TABLE closure_post_mortems ("
                " id TEXT PRIMARY KEY, position_id TEXT, outcome TEXT, realized_pnl REAL,"
                " actual_underlying_move_pct REAL, exit_date TEXT, exit_trigger TEXT,"
                " lesson_tags TEXT, user_override_logged INTEGER,"
                " playbook_id TEXT, playbook_version TEXT);"
            )
            self._insert_post_mortem(conn, "pm1", "pos1")
            self._insert_post_mortem(conn, "pm2", "pos1")  # double-submit residue
            self._insert_post_mortem(conn, "pm3", "pos2")
            conn.commit()
        _ensure_schema_sync(_url(db))  # must NOT raise
        with closing(sqlite3.connect(db)) as conn:
            index_names = {row[1] for row in conn.execute("PRAGMA index_list(closure_post_mortems)")}
            assert "ix_closure_post_mortems_position_id" in index_names
            survivors = conn.execute("SELECT id FROM closure_post_mortems ORDER BY id").fetchall()
            assert survivors == [("pm1",), ("pm3",)]  # earliest per position kept
            quarantined = conn.execute(
                "SELECT payload FROM audit_events WHERE event_type = 'POST_MORTEM_DUPLICATE_QUARANTINED'"
            ).fetchall()
            assert len(quarantined) == 1
            assert '"pm2"' in quarantined[0][0]  # the full surplus row preserved
        assert list(tmp_path.glob("dupes.db.pre-migration-*.bak"))  # evidence backed up first
        _ensure_schema_sync(_url(db))  # rerun stays idempotent, nothing more quarantined
        with closing(sqlite3.connect(db)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type = 'POST_MORTEM_DUPLICATE_QUARANTINED'"
            ).fetchone()[0]
            assert count == 1

    def test_rerun_stays_idempotent(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.db"
        _ensure_schema_sync(_url(db))
        _ensure_schema_sync(_url(db))  # must not raise on the second pass
        with closing(sqlite3.connect(db)) as conn:
            index_names = {row[1] for row in conn.execute("PRAGMA index_list(closure_post_mortems)")}
            assert "ix_closure_post_mortems_position_id" in index_names


class TestNoDestructivePath:
    def test_drop_all_is_gone(self) -> None:
        """The destructive heuristic must never come back."""
        source = Path("backend/database.py").read_text(encoding="utf-8")
        assert "drop_all" not in source
        assert "_needs_migration" not in source
