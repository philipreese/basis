"""Tests for the pre-launch schema bootstrap in backend.database (#94).

Policy: until the first real paper fill exists there are no migrations —
the models ARE the schema. These tests pin the three behaviors that make
that safe: fresh databases materialize completely, new tables appear
additively on existing databases, and a column-level mismatch refuses to
run (with a delete-and-restart instruction) instead of guessing. The
destructive drop_all heuristic stays dead.
"""

import json
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


class TestMigrationBackupUsesWalSafeSnapshot:
    """#543 (Audit II R4 MED-3): _backup_before_migration used to be a plain
    shutil.copy2 of the main db file — the engine runs WAL mode, so every
    commit since the last checkpoint lives solely in the -wal file and was
    silently absent from the .bak. Mirrors test_db_backup's #353 test but
    for the migration path."""

    def test_uncheckpointed_wal_frames_reach_the_pre_migration_backup(self, tmp_path: Path) -> None:
        db = tmp_path / "walmig.db"
        # An older database predating the closure_post_mortems unique index
        # (see TestClosurePostMortemUniqueIndex above), seeded with a
        # duplicate position_id: _ensure_schema_sync backs up before
        # quarantining it.
        with closing(sqlite3.connect(db)) as conn:
            conn.executescript(
                "CREATE TABLE closure_post_mortems ("
                " id TEXT PRIMARY KEY, position_id TEXT, outcome TEXT, realized_pnl REAL,"
                " actual_underlying_move_pct REAL, exit_date TEXT, exit_trigger TEXT,"
                " lesson_tags TEXT, user_override_logged INTEGER,"
                " playbook_id TEXT, playbook_version TEXT);"
            )
            conn.execute(
                "INSERT INTO closure_post_mortems"
                " (id, position_id, outcome, realized_pnl, actual_underlying_move_pct,"
                "  exit_date, exit_trigger, lesson_tags, user_override_logged)"
                " VALUES ('pm1', 'pos1', 'WIN', 1.0, 0.0, '2026-08-01', 'MANUAL', '[]', 0)"
            )
            conn.execute(
                "INSERT INTO closure_post_mortems"
                " (id, position_id, outcome, realized_pnl, actual_underlying_move_pct,"
                "  exit_date, exit_trigger, lesson_tags, user_override_logged)"
                " VALUES ('pm2', 'pos1', 'WIN', 1.0, 0.0, '2026-08-01', 'MANUAL', '[]', 0)"
            )
            conn.commit()

        # Keep a writer connection open in WAL mode so its commit lands only
        # in walmig.db-wal, never checkpointed into the main file — exactly
        # the frames a plain shutil.copy2 (the pre-#543 behavior) would miss.
        writer = sqlite3.connect(db)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute(
                "INSERT INTO closure_post_mortems"
                " (id, position_id, outcome, realized_pnl, actual_underlying_move_pct,"
                "  exit_date, exit_trigger, lesson_tags, user_override_logged)"
                " VALUES ('wal-only', 'pos2', 'WIN', 1.0, 0.0, '2026-08-01', 'MANUAL', '[]', 0)"
            )
            writer.commit()

            _ensure_schema_sync(_url(db))
        finally:
            writer.close()

        backups = list(tmp_path.glob("walmig.db.pre-migration-*.bak"))
        assert backups
        with closing(sqlite3.connect(backups[0])) as conn:
            rows = conn.execute("SELECT id FROM closure_post_mortems WHERE id = 'wal-only'").fetchall()
        assert rows == [("wal-only",)]


class TestTestPollutionQuarantine:
    """#561 (Audit II R4): a DATABASE_URL isolation bug let alert_crash write
    real audit rows during pytest runs, straight into the repo-root basis.db.
    The quarantine is deliberately narrow — only rows carrying a literal
    string that appears nowhere in application code (only in a test's raised
    exception) are touched, scoped to the two event types those specific
    tests actually produce."""

    def _insert(
        self, conn: sqlite3.Connection, event_type: str, payload: str, run_at: str = "2026-08-20T20:00:00+00:00"
    ):
        conn.execute(
            "INSERT INTO audit_events (run_at, book_id, event_type, actor, payload) VALUES (?, NULL, ?, 'system', ?)",
            (run_at, event_type, payload),
        )

    def test_disk_full_and_boom_rows_are_quarantined_not_deleted(self, tmp_path: Path) -> None:
        db = tmp_path / "polluted.db"
        _ensure_schema_sync(_url(db))  # fresh schema first
        with closing(sqlite3.connect(db)) as conn:
            self._insert(
                conn,
                "SCHEDULER_ALERT",
                '{"title": "basis: DB backup FAILED", "body": "Nightly database backup failed: disk full"}',
            )
            self._insert(conn, "CRASH_ALERT", '{"title": "basis fill check CRASHED", "body": "RuntimeError: boom"}')
            # Legitimate rows that must survive untouched.
            self._insert(conn, "CANDIDATE_UNPRICEABLE", '{"underlying": "SPY"}')
            self._insert(
                conn,
                "SCHEDULER_ALERT",
                '{"title": "basis: DB backup FAILED", "body": "Nightly database backup failed: [Errno 28] '
                'No space left on device"}',  # a REAL disk-full message never matches the test literal
            )
            conn.commit()
        _ensure_schema_sync(_url(db))  # runs the quarantine
        with closing(sqlite3.connect(db)) as conn:
            remaining = {row[0] for row in conn.execute("SELECT event_type FROM audit_events")}
            assert "SCHEDULER_ALERT" in remaining  # the real-message row survives
            assert "CANDIDATE_UNPRICEABLE" in remaining
            # The quarantine rows themselves legitimately nest the marker
            # text (the original payload, preserved as evidence) — scope
            # this check to the ORIGINAL event types the pollution used.
            polluted_left = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type IN ('SCHEDULER_ALERT', 'CRASH_ALERT') "
                "AND (payload LIKE '%disk full%' OR payload LIKE '%RuntimeError: boom%')"
            ).fetchone()[0]
            assert polluted_left == 0  # the two test-fixture rows are gone
            quarantined = conn.execute(
                "SELECT payload FROM audit_events WHERE event_type = 'TEST_POLLUTION_QUARANTINED'"
            ).fetchall()
            assert len(quarantined) == 2
            bodies = [json.loads(json.loads(p)["payload"])["body"] for (p,) in quarantined]
            assert "disk full" in " ".join(bodies)
            assert "RuntimeError: boom" in " ".join(bodies)
        assert list(tmp_path.glob("polluted.db.pre-migration-*.bak"))  # evidence backed up first

    def test_rerun_does_not_re_quarantine_or_loop(self, tmp_path: Path) -> None:
        # The quarantine row's payload nests the original row's payload,
        # which nests the same marker string — a naive re-match would
        # re-quarantine its own quarantine row forever.
        db = tmp_path / "polluted2.db"
        _ensure_schema_sync(_url(db))
        with closing(sqlite3.connect(db)) as conn:
            self._insert(conn, "SCHEDULER_ALERT", '{"title": "x", "body": "disk full"}')
            conn.commit()
        _ensure_schema_sync(_url(db))
        _ensure_schema_sync(_url(db))  # second pass must not raise or grow
        with closing(sqlite3.connect(db)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type = 'TEST_POLLUTION_QUARANTINED'"
            ).fetchone()[0]
        assert count == 1  # not re-quarantined, not duplicated

    def test_other_event_types_are_never_touched_even_with_the_marker_text(self, tmp_path: Path) -> None:
        # Scoped to SCHEDULER_ALERT/CRASH_ALERT only — a coincidental
        # marker-text match on an unrelated event type (which cannot happen
        # in practice, but the query itself must not be that permissive)
        # must not quarantine real evidence.
        db = tmp_path / "scoped.db"
        _ensure_schema_sync(_url(db))
        with closing(sqlite3.connect(db)) as conn:
            self._insert(conn, "CANDIDATE_UNPRICEABLE", '{"note": "disk full coincidentally mentioned"}')
            conn.commit()
        _ensure_schema_sync(_url(db))
        with closing(sqlite3.connect(db)) as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type = 'CANDIDATE_UNPRICEABLE'"
            ).fetchone()[0]
            quarantined = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type = 'TEST_POLLUTION_QUARANTINED'"
            ).fetchone()[0]
        assert remaining == 1
        assert quarantined == 0


class TestNoDestructivePath:
    def test_drop_all_is_gone(self) -> None:
        """The destructive heuristic must never come back."""
        source = Path("backend/database.py").read_text(encoding="utf-8")
        assert "drop_all" not in source
        assert "_needs_migration" not in source
