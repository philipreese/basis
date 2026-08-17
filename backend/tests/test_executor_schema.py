"""Tests for the executor tables' append-only enforcement (#61).

fills / gate_events / audit_events are the Live Gate's evidence (ADR-0006):
no code path may rewrite history. The ORM layer rejects UPDATE and DELETE
on them via a before_flush guard; these tests pin it.
"""

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.models import (
    AppendOnlyViolationError,
    AuditEventModel,
    Base,
    FillModel,
    GateEventModel,
)


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
