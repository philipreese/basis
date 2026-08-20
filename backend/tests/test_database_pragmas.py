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
