"""SQLite hardening (#271): every connection runs in WAL with a busy
timeout, so the console server and a scheduled entrypoint touching the
database at the same moment wait briefly instead of crashing on
'database is locked'."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.database import _install_sqlite_pragmas


@pytest.mark.asyncio
async def test_connections_get_wal_and_busy_timeout(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'pragmas.db').as_posix()}")
    _install_sqlite_pragmas(engine.sync_engine)
    async with engine.connect() as conn:
        journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        busy_timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
    await engine.dispose()
    assert journal_mode == "wal"
    assert busy_timeout == 5000


def test_missing_nullable_column_is_added_in_place(tmp_path):
    # #280: the database holds Live Gate evidence — delete-and-restart is
    # dead. A model gaining a nullable column must migrate additively.
    from sqlalchemy import create_engine, inspect

    from backend.database import _ensure_schema_sync
    from backend.models import Base

    url = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"
    sync_engine = create_engine(url)
    Base.metadata.create_all(sync_engine)
    with sync_engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE positions DROP COLUMN last_priced_at")  # yesterday's schema
    sync_engine.dispose()
    _ensure_schema_sync(f"sqlite+aiosqlite:///{(tmp_path / 'mig.db').as_posix()}")
    sync_engine = create_engine(url)
    cols = {c["name"] for c in inspect(sync_engine).get_columns("positions")}
    sync_engine.dispose()
    assert "last_priced_at" in cols
