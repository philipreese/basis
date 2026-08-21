"""Tests for the test-DB isolation guard in conftest.py (#561).

TestBackupAfterRun (test_db_backup.py) used to write real audit rows —
including 3 fake SCHEDULER_ALERT "disk full" rows — straight into the
repo-root basis.db, because it patched db_backup.DATABASE_URL (a separate
binding from backend.database.DATABASE_URL) while operator.alert_crash reads
backend.database.DATABASE_URL fresh at call time. These tests pin the fix:
DATABASE_URL isolation actually redirects the modules that matter, and the
sqlite3-level tripwire actually fires if something still reaches for the
real file.
"""

import sqlite3
import sqlite3.dbapi2
from pathlib import Path

import pytest

import backend.tests.conftest as conftest_mod


class TestDatabaseUrlIsolation:
    def test_backend_database_module_attribute_is_isolated(self):
        # The exact binding alert_crash reads at call time (`from
        # backend.database import DATABASE_URL`, executed fresh on every
        # call) — this must never be the repo-root default.
        import backend.database as db_mod

        assert db_mod.DATABASE_URL != "sqlite+aiosqlite:///basis.db"
        assert "basis-test-db-" in db_mod.DATABASE_URL or str(Path.cwd()) not in db_mod.DATABASE_URL

    def test_db_backup_module_attribute_is_also_isolated(self):
        # The separate binding TestBackupAfterRun's bug hid behind — patching
        # this one alone is not enough (that was the whole bug), but it must
        # never default back to the real path either.
        from backend import db_backup

        assert db_backup.DATABASE_URL != "sqlite+aiosqlite:///basis.db"


class TestRealDatabaseTripwire:
    def test_sqlite3_connect_to_the_real_path_is_blocked(self):
        with pytest.raises(RuntimeError, match="BLOCKED"):
            sqlite3.connect(str(conftest_mod._REAL_DB_PATH))

    def test_sqlite3_dbapi2_connect_to_the_real_path_is_blocked(self):
        # SQLAlchemy's pysqlite/aiosqlite dialects resolve via
        # `from sqlite3 import dbapi2 as sqlite` — a plain `sqlite3.connect`
        # patch alone does not reach this separate binding.
        with pytest.raises(RuntimeError, match="BLOCKED"):
            sqlite3.dbapi2.connect(str(conftest_mod._REAL_DB_PATH))

    def test_sqlalchemy_sync_engine_against_the_real_path_is_blocked(self):
        # This is the exact shape of operator.alert_crash's write path
        # (create_engine on a sync sqlite URL) — the guard must catch it
        # through SQLAlchemy, not just a direct sqlite3.connect call.
        from sqlalchemy import create_engine, text

        engine = create_engine(f"sqlite:///{conftest_mod._REAL_DB_PATH}")
        with pytest.raises(RuntimeError, match="BLOCKED"), engine.begin() as conn:
            conn.execute(text("SELECT 1"))

    def test_memory_and_tmp_paths_are_unaffected(self, tmp_path):
        # The guard must be surgical — every other test in the suite opens
        # tmp_path-backed or :memory: databases constantly.
        conn = sqlite3.connect(":memory:")
        conn.close()
        real_path = tmp_path / "fine.db"
        conn = sqlite3.connect(str(real_path))
        conn.close()
        assert real_path.exists()
