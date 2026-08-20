"""Tests for the reconciliation resolution flow (backend/resolution.py, #310).

Drift corrections go through an audited path — external close moves cash with
the executor's own signed conventions and writes a MANUAL post-mortem; cash
adjustments demand a reason; the API surfaces the latest run and records the
human resolution without ever auto-resuming (ADR-0008).
"""

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import get_db
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    ClosurePostMortemModel,
    OrderModel,
    PositionModel,
    ReconciliationRunModel,
)
from backend.resolution import ResolutionError, adjust_book_cash, record_external_close


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def client(session_maker):
    from backend.main import app

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _book(book_id: str = "B01", cash: float = 10000.0) -> BookModel:
    return BookModel(
        id=book_id,
        name=f"lab {book_id}",
        config={"engine_variant": "V1", "underlying": "XSP", "envelope": {}},
        config_version=1,
        config_hash="cafe1234",
        starting_capital=10000.0,
        cash_balance=cash,
        status="ACTIVE",
        created_at="2026-08-01T00:00:00+00:00",
        last_mtm=None,
    )


def _position(pos_id: str = "p1", book_id: str = "B01", direction: str = "CREDIT", **overrides) -> PositionModel:
    defaults: dict = {
        "id": pos_id,
        "underlying": "XSP",
        "strategy_type": "BULL_PUT_SPREAD",
        "execution_mode": "PAPER",
        "legs": [],
        "entry_date": "2026-08-01",
        "expiration_date": "2026-09-18",
        "entry_premium": 1.0,
        "premium_direction": direction,
        "current_value_per_share": 1.0,
        "contracts": 2,
        "max_profit": 1.0,
        "max_loss": 2.0,
        "notes": "",
        "rolls": 0,
        "status": "OPEN",
        "journal": {},
        "book_id": book_id,
    }
    defaults.update(overrides)
    return PositionModel(**defaults)


async def _audit_events(maker, event_type: str) -> list[AuditEventModel]:
    async with maker() as session:
        rows = await session.execute(select(AuditEventModel).where(AuditEventModel.event_type == event_type))
        return list(rows.scalars())


class TestExternalClose:
    @pytest.mark.asyncio
    async def test_credit_close_costs_cash_and_writes_manual_post_mortem(self, session_maker):
        async with session_maker() as session:
            session.add(_book())
            session.add(_position())  # CREDIT, entry 1.0, 2 contracts
            await session.commit()
        async with session_maker() as session:
            pm = await record_external_close(session, "p1", 0.4, "closed by hand at IBKR")
        # Buying back a credit at 0.40 × 100 × 2 costs $80
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
            assert book.cash_balance == pytest.approx(10000.0 - 80.0)
            pos = await session.get(PositionModel, "p1")
            assert pos.status == "CLOSED"
            assert pos.current_value_per_share == 0.4
            assert pos.last_priced_at is not None
        # (1.0 - 0.4) × 100 × 2 = +120 realized
        assert pm.realized_pnl == pytest.approx(120.0)
        assert pm.outcome == "WIN"
        assert pm.exit_trigger == "MANUAL"
        assert pm.user_override_logged is True
        (event,) = await _audit_events(session_maker, "RESOLUTION_EXTERNAL_CLOSE")
        assert event.actor == "resolution"
        assert event.book_id == "B01"
        assert event.payload["reason"] == "closed by hand at IBKR"

    @pytest.mark.asyncio
    async def test_debit_close_receives_cash(self, session_maker):
        async with session_maker() as session:
            session.add(_book())
            session.add(_position(direction="DEBIT"))
            await session.commit()
        async with session_maker() as session:
            pm = await record_external_close(session, "p1", 1.8, "sold at broker")
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
            assert book.cash_balance == pytest.approx(10000.0 + 360.0)
        assert pm.realized_pnl == pytest.approx(160.0)

    @pytest.mark.asyncio
    async def test_refuses_non_open_position_missing_position_and_bad_inputs(self, session_maker):
        async with session_maker() as session:
            session.add(_book())
            session.add(_position(status="CLOSED"))
            await session.commit()
        async with session_maker() as session:
            with pytest.raises(ResolutionError, match="not OPEN"):
                await record_external_close(session, "p1", 0.4, "some reason")
            with pytest.raises(ResolutionError, match="No position"):
                await record_external_close(session, "nope", 0.4, "some reason")
            with pytest.raises(ResolutionError, match="reason"):
                await record_external_close(session, "p1", 0.4, "  ")
            with pytest.raises(ResolutionError, match="magnitude"):
                await record_external_close(session, "p1", -0.4, "some reason")

    @pytest.mark.asyncio
    async def test_refuses_while_live_orders_reference_the_position(self, session_maker):
        # Audit II (#345): a SUBMITTED close's fill can arrive on the next
        # sync — recording an external close now would book the exit twice.
        def order(order_id: str, status: str) -> OrderModel:
            return OrderModel(
                id=order_id,
                book_id="B01",
                position_id="p1",
                order_ref=f"basis:B01:{order_id}:close",
                ib_order_id=100,
                ib_perm_id=90100,
                action="CLOSE",
                combo_legs={"legs": [], "quantity": 2},
                order_type="LIMIT",
                limit_price=-0.4,
                decision_midpoint=-0.4,
                status=status,
                submitted_at="t0",
                completed_at=None,
                encumbered_risk=0.0,
            )

        async with session_maker() as session:
            session.add(_book())
            session.add(_position())
            session.add(order("o_live", "SUBMITTED"))
            await session.commit()
        async with session_maker() as session:
            with pytest.raises(ResolutionError, match="basis:B01:o_live:close"):
                await record_external_close(session, "p1", 0.4, "closed by hand")
        async with session_maker() as session:
            pos = await session.get(PositionModel, "p1")
            book = await session.get(BookModel, "B01")
        assert pos.status == "OPEN" and book.cash_balance == 10000.0  # nothing moved
        # PARTIAL is the latch this flow exists to clean up (#283) — allowed.
        async with session_maker() as session:
            live = await session.get(OrderModel, "o_live")
            live.status = "PARTIAL"
            await session.commit()
        async with session_maker() as session:
            pm = await record_external_close(session, "p1", 0.4, "partial cleanup")
        assert pm.exit_trigger == "MANUAL"


class TestCashAdjustment:
    @pytest.mark.asyncio
    async def test_adjusts_and_audits(self, session_maker):
        async with session_maker() as session:
            session.add(_book())
            await session.commit()
        async with session_maker() as session:
            balance = await adjust_book_cash(session, "B01", -12.5, "assignment fee not in fills")
        assert balance == pytest.approx(9987.5)
        (event,) = await _audit_events(session_maker, "RESOLUTION_CASH_ADJUSTED")
        assert event.actor == "resolution"
        assert event.payload["delta"] == -12.5
        assert event.payload["new_balance"] == pytest.approx(9987.5)

    @pytest.mark.asyncio
    async def test_refuses_zero_delta_missing_book_and_empty_reason(self, session_maker):
        async with session_maker() as session:
            session.add(_book())
            await session.commit()
        async with session_maker() as session:
            with pytest.raises(ResolutionError, match="zero adjustment"):
                await adjust_book_cash(session, "B01", 0.0, "some reason")
            with pytest.raises(ResolutionError, match="No book"):
                await adjust_book_cash(session, "B99", 5.0, "some reason")
            with pytest.raises(ResolutionError, match="reason"):
                await adjust_book_cash(session, "B01", 5.0, "")


class TestApi:
    @pytest.mark.asyncio
    async def test_latest_run_404_when_none_then_returns_newest(self, session_maker, client):
        resp = await client.get("/api/reconciliation/latest")
        assert resp.status_code == 404
        async with session_maker() as session:
            session.add(
                ReconciliationRunModel(
                    run_at="2026-08-19T22:45:00+00:00", broker_snapshot={}, books_expected={}, result="CLEAN"
                )
            )
            session.add(
                ReconciliationRunModel(
                    run_at="2026-08-20T22:45:00+00:00",
                    broker_snapshot={},
                    books_expected={},
                    result="DRIFT",
                    drift_details=[{"kind": "MISSING_AT_BROKER", "detail": "p1"}],
                )
            )
            await session.commit()
        resp = await client.get("/api/reconciliation/latest")
        assert resp.status_code == 200
        body = resp.json()
        assert body["result"] == "DRIFT"
        assert body["drift_details"] == [{"kind": "MISSING_AT_BROKER", "detail": "p1"}]
        assert body["resolved_at"] is None

    @pytest.mark.asyncio
    async def test_resolve_records_resolution_and_validates(self, session_maker, client):
        async with session_maker() as session:
            run = ReconciliationRunModel(
                run_at="2026-08-20T22:45:00+00:00", broker_snapshot={}, books_expected={}, result="DRIFT"
            )
            session.add(run)
            await session.commit()
            run_id = run.id
        resp = await client.post(f"/api/reconciliation/{run_id}/resolve", json={"resolution": "x"})
        assert resp.status_code == 400
        resp = await client.post("/api/reconciliation/9999/resolve", json={"resolution": "external close recorded"})
        assert resp.status_code == 404
        resp = await client.post(
            f"/api/reconciliation/{run_id}/resolve", json={"resolution": "external close recorded"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["resolution"] == "external close recorded"
        assert body["resolved_at"] is not None

    @pytest.mark.asyncio
    async def test_external_close_and_cash_routes_translate_errors_to_400(self, session_maker, client):
        async with session_maker() as session:
            session.add(_book())
            session.add(_position())
            await session.commit()
        resp = await client.post(
            "/api/resolution/external-close",
            json={"position_id": "p1", "exit_value_per_share": 0.4, "reason": "closed at broker"},
        )
        assert resp.status_code == 200
        assert resp.json()["exit_trigger"] == "MANUAL"
        async with session_maker() as session:
            pms = list((await session.execute(select(ClosurePostMortemModel))).scalars())
            assert len(pms) == 1
        # Second attempt: no longer OPEN → 400 with the reason in detail
        resp = await client.post(
            "/api/resolution/external-close",
            json={"position_id": "p1", "exit_value_per_share": 0.4, "reason": "closed at broker"},
        )
        assert resp.status_code == 400
        assert "not OPEN" in resp.json()["detail"]

        resp = await client.post("/api/resolution/cash", json={"book_id": "B01", "delta": 25.0, "reason": "fee refund"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["book_id"] == "B01"
        resp = await client.post("/api/resolution/cash", json={"book_id": "B01", "delta": 0.0, "reason": "fee refund"})
        assert resp.status_code == 400
