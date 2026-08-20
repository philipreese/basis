"""Trading-mode isolation (ADR-0006, #204): paper and live never share a
database file, every database is stamped with the mode that created it, and
the paper executor refuses to run in live mode at all."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import executor as executor_mod
from backend.database import _assert_trading_mode_stamp, default_database_url
from backend.executor import run_executor_evening
from backend.models import Base, DbMetaModel


@pytest_asyncio.fixture
async def session_maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'mode.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


def test_each_mode_gets_its_own_default_file():
    assert default_database_url("paper").endswith("options_playbook.db")
    assert default_database_url("live").endswith("options_playbook.live.db")
    assert default_database_url("paper") != default_database_url("live")


@pytest.mark.asyncio
async def test_fresh_database_is_stamped_with_the_process_mode(session_maker):
    await _assert_trading_mode_stamp(session_maker, mode="paper")
    async with session_maker() as session:
        row = await session.get(DbMetaModel, "trading_mode")
    assert row.value == "paper"
    # Re-open in the same mode: fine.
    await _assert_trading_mode_stamp(session_maker, mode="paper")


@pytest.mark.asyncio
async def test_mode_mismatch_refuses_hard(session_maker):
    await _assert_trading_mode_stamp(session_maker, mode="live")
    with pytest.raises(RuntimeError, match="Trading-mode mismatch"):
        await _assert_trading_mode_stamp(session_maker, mode="paper")


@pytest.mark.asyncio
async def test_paper_executor_refuses_to_run_in_live_mode(monkeypatch):
    # The live executor is a separate, unbuilt thing (approval-per-trade) —
    # this pipeline running against live money must be impossible.
    monkeypatch.setattr(executor_mod, "TRADING_MODE", "live")
    with pytest.raises(RuntimeError, match="PAPER executor"):
        await run_executor_evening(session_maker=None, broker_factory=None)
