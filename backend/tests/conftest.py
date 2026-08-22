"""Test-wide safety rails.

operator.py loads the developer's real .env at import, so any test that
reaches send_ntfy WITHOUT patching it would push to the REAL phone topic
(this happened — #278's receipt tests spammed the operator's phone with
'remote HALT applied'). Strip every ntfy variable before each test: a test
that wants a topic sets its own, and httpx is then the only thing left to
patch.

#561: the same class of bug hit the database. backend.database computes
DATABASE_URL, engine, and async_session_maker ONCE at import time — a
per-test fixture that runs AFTER that first import can't redirect the
already-created engine/session_maker, only code that re-reads
backend.database.DATABASE_URL at call time (e.g. operator.alert_crash).
TestBackupAfterRun (test_db_backup.py) patched db_backup.DATABASE_URL — a
separate binding created by db_backup's own `from backend.database import
DATABASE_URL`, disconnected from backend.database.DATABASE_URL — so
alert_crash's crash-path audit write read the REAL DATABASE_URL and wrote
~200 rows, including 3 fake SCHEDULER_ALERT "disk full" rows, straight into
the repo-root basis.db. The module-level line below runs at pytest
COLLECTION, before any test file (and therefore before backend.database
itself) is ever imported, so the module's very FIRST binding of
DATABASE_URL/engine/async_session_maker points at an isolated sandbox file,
never the repo-root basis.db, regardless of which test or code path touches
it first.
"""

import os
import sqlite3
import sqlite3.dbapi2
import tempfile
from pathlib import Path

import pytest

_SESSION_DB_DIR = tempfile.mkdtemp(prefix="basis-test-db-")
_SESSION_DB_PATH = Path(_SESSION_DB_DIR) / "session-isolated.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_SESSION_DB_PATH.as_posix()}"

# The exact path DATABASE_URL's relative default ("sqlite+aiosqlite:///basis.db",
# backend/database.py) resolves to when a process's CWD is the repo root —
# which pixi's test task uses. This is the ONE file no test may ever open.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL_DB_PATH = (_REPO_ROOT / "basis.db").resolve()

# #649: the same class of bug as #561, one seam over — db_backup._backup_dir()
# defaults to the operator's real OneDrive folder. The 2026-08-20 backup
# (4096 bytes, zero tables) is believed to have been clobbered by exactly
# this: a test running the real backup path with DB_BACKUP_DIR unisolated.
# No test may write into this tree, verified or not.
_REAL_BACKUP_DIR = (Path.home() / "OneDrive" / "basis-db-backups").resolve()


@pytest.fixture(autouse=True)
def _no_real_ntfy(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("NTFY_COMMAND_TOPIC", raising=False)
    monkeypatch.setenv("NTFY_SERVER", "http://ntfy.invalid")  # unroutable — belt and braces


@pytest.fixture(autouse=True)
def _isolated_database(tmp_path, monkeypatch):
    """#561: per-test DATABASE_URL isolation on top of the session-wide
    guard above. Most tests build their own dedicated engine against a
    tmp_path DB via a session_maker fixture and never touch this at all —
    but any code path that reads backend.database.DATABASE_URL fresh at
    call time (alert_crash) gets its own per-test tmp path here too, not
    just the one shared session sandbox.

    Then a loud failure — never a silent redirect — if anything still
    manages to open the real repo-root basis.db during a test. sqlite3 is
    the floor every driver in this codebase goes through: aiosqlite calls
    sqlite3.connect(...) directly (attribute lookup at call time, so
    patching the module attribute here catches it), and SQLAlchemy's
    pysqlite/aiosqlite dialects both resolve their driver via
    `from sqlite3 import dbapi2 as sqlite` — a SEPARATE binding on the
    dbapi2 submodule that a plain `sqlite3.connect = ...` patch does not
    reach, so both are patched explicitly. Verified empirically: patching
    only sqlite3.connect leaves SQLAlchemy engines uncaught.

    #649: DB_BACKUP_DIR gets the identical treatment for the identical
    reason — db_backup._backup_dir() reads it fresh at call time, so a
    per-test override here reaches every code path, not just tests that
    remember to point it at tmp_path themselves (test_db_backup.py's own
    _point_at helper does that explicitly too; this is the structural
    floor for everything else, e.g. a test that reaches
    gateway_lifecycle._backup_after_run indirectly)."""
    test_db = tmp_path / "isolated.db"
    test_url = f"sqlite+aiosqlite:///{test_db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", test_url)
    monkeypatch.setattr("backend.database.DATABASE_URL", test_url, raising=False)
    monkeypatch.setenv("DB_BACKUP_DIR", str(tmp_path / "backups"))

    real_connect = sqlite3.connect

    def _guarded_connect(database, *args, **kwargs):
        if isinstance(database, (str, os.PathLike)) and str(database) not in ("", ":memory:"):
            try:
                resolved = Path(database).resolve()
            except (OSError, ValueError):
                resolved = None
            if resolved == _REAL_DB_PATH:
                raise RuntimeError(
                    f"BLOCKED (#561): a test tried to open the real database at {_REAL_DB_PATH}. "
                    "DATABASE_URL isolation should have prevented this — fix the code path that "
                    "bypassed it instead of removing this guard."
                )
            if resolved is not None and _REAL_BACKUP_DIR in resolved.parents:
                raise RuntimeError(
                    f"BLOCKED (#649): a test tried to open a file under the real OneDrive backup dir "
                    f"{_REAL_BACKUP_DIR}. DB_BACKUP_DIR isolation should have prevented this — fix the "
                    "code path that bypassed it instead of removing this guard."
                )
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _guarded_connect)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", _guarded_connect)
