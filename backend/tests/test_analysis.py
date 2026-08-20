"""Tests for the Analysis read models (backend/analysis.py).

Fill quality (#242): slippage math is pinned against hand-computed
examples in both premium directions, decomposition (ladder concession +
market movement = total) holds, orders without backfilled fills are
counted as awaiting rather than silently measured at limit economics.
"""

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.analysis import fill_quality_report
from backend.database import get_db
from backend.models import Base, BookModel, FillModel, OrderModel


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


def _book(book_id: str = "B01") -> BookModel:
    return BookModel(
        id=book_id,
        name=f"lab {book_id}",
        config={},
        config_version=1,
        config_hash="cafe1234",
        starting_capital=10000.0,
        cash_balance=10000.0,
        status="ACTIVE",
        created_at="2026-08-01T00:00:00+00:00",
    )


def _order(order_id: str, book_id: str = "B01", *, action: str = "OPEN", ref_suffix: str = "open", **overrides):
    defaults: dict = {
        "id": order_id,
        "book_id": book_id,
        "position_id": None,
        "order_ref": f"basis:{book_id}:{order_id}:{ref_suffix}",
        "ib_order_id": 1,
        "ib_perm_id": 1,
        "action": action,
        "combo_legs": {"quantity": 2, "underlying": "XSP"},
        "order_type": "LIMIT",
        "limit_price": -1.20,
        "decision_midpoint": -1.30,
        "status": "FILLED",
        "submitted_at": "2026-08-19T22:45:00+00:00",
        "completed_at": "2026-08-20T13:31:00+00:00",
    }
    defaults.update(overrides)
    return OrderModel(**defaults)


def _fill(exec_id: str, order_id: str, *, side: str, price: float, qty: float = 2.0, commission: float = 1.1):
    return FillModel(
        exec_id=exec_id,
        order_id=order_id,
        book_id="B01",
        con_id=1,
        side=side,
        quantity=qty,
        price=price,
        commission=commission,
        fill_time="2026-08-20T13:31:00+00:00",
    )


class TestFillQuality:
    @pytest.mark.asyncio
    async def test_credit_spread_slippage_decomposition(self, session_maker):
        # Decided mid -1.30 (credit 1.30), posted limit -1.20 (one rung in),
        # market filled short leg 2.00 / long leg 0.85 → net -1.15 credit.
        async with session_maker() as session:
            session.add(_book())
            session.add(_order("o1"))
            session.add(_fill("e1", "o1", side="SLD", price=2.00))
            session.add(_fill("e2", "o1", side="BOT", price=0.85))
            await session.commit()
        async with session_maker() as session:
            report = await fill_quality_report(session)
        (row,) = report.rows
        assert row.net_fill_per_share == pytest.approx(-1.15)
        assert row.ladder_concession_per_share == pytest.approx(0.10)  # chose to give up 10c
        assert row.market_slippage_per_share == pytest.approx(0.05)  # market took 5c more
        assert row.total_slippage_per_share == pytest.approx(0.15)
        assert row.commissions == pytest.approx(2.2)
        # 15c/share × $100 = $15/contract — worse than the $5 haircut assumption.
        assert report.avg_slippage_per_contract == pytest.approx(15.0)
        assert report.haircut_per_contract == 5.0
        assert report.orders_analyzed == 1 and report.orders_awaiting_fills == 0

    @pytest.mark.asyncio
    async def test_debit_close_and_tp_labeling(self, session_maker):
        # A profit-taker buys back a credit spread, so the close PAYS:
        # mid 0.60 debit, filled at 0.63 → 3c worse.
        async with session_maker() as session:
            session.add(_book())
            session.add(_order("o2", action="CLOSE", ref_suffix="o2:tp", limit_price=0.62, decision_midpoint=0.60))
            session.add(_fill("e3", "o2", side="BOT", price=1.55))
            session.add(_fill("e4", "o2", side="SLD", price=0.92))
            await session.commit()
        async with session_maker() as session:
            report = await fill_quality_report(session)
        (row,) = report.rows
        assert row.action == "TP"
        assert row.net_fill_per_share == pytest.approx(0.63)
        assert row.total_slippage_per_share == pytest.approx(0.03)
        (agg,) = report.by_action
        assert agg.label == "TP" and agg.orders == 1

    @pytest.mark.asyncio
    async def test_filled_order_without_fills_counts_as_awaiting(self, session_maker):
        async with session_maker() as session:
            session.add(_book())
            session.add(_order("o3"))
            await session.commit()
        async with session_maker() as session:
            report = await fill_quality_report(session)
        assert report.orders_analyzed == 0
        assert report.orders_awaiting_fills == 1
        assert report.avg_slippage_per_contract is None
        (row,) = report.rows
        assert row.net_fill_per_share is None and row.market_slippage_per_share is None
        # The concession we chose is known even before fills arrive.
        assert row.ladder_concession_per_share == pytest.approx(0.10)

    @pytest.mark.asyncio
    async def test_aggregates_weight_by_contracts_and_split_by_book(self, session_maker):
        async with session_maker() as session:
            session.add(_book("B01"))
            session.add(_book("B02"))
            # B01: 2 contracts at +0.10 slip; B02: 1 contract at +0.40 slip.
            session.add(_order("o4", "B01"))
            session.add(_fill("e5", "o4", side="SLD", price=1.20))  # net -1.20 vs mid -1.30
            session.add(
                _order("o5", "B02", combo_legs={"quantity": 1, "underlying": "XSP"}, order_ref="basis:B02:o5:open")
            )
            session.add(_fill("e6", "o5", side="SLD", price=0.90, qty=1.0))  # net -0.90 vs -1.30
            await session.commit()
        async with session_maker() as session:
            report = await fill_quality_report(session)
        # Weighted: (0.10×2 + 0.40×1)/3 = 0.20/share → $20/contract
        assert report.avg_slippage_per_contract == pytest.approx(20.0)
        by_book = {a.label: a for a in report.by_book}
        assert by_book["B01"].avg_slippage_per_contract == pytest.approx(10.0)
        assert by_book["B02"].avg_slippage_per_contract == pytest.approx(40.0)
        # Rows sorted worst-first.
        assert report.rows[0].book_id == "B02"

    @pytest.mark.asyncio
    async def test_endpoint_serves_the_report(self, session_maker, client):
        resp = await client.get("/api/analysis/fill-quality")
        assert resp.status_code == 200
        body = resp.json()
        assert body["orders_analyzed"] == 0
        assert body["haircut_per_contract"] == 5.0
