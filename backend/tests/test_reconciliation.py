"""Tests for the reconciliation engine (backend/reconciliation.py, #66)."""

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.broker import FillInfo, LegPosition, OpenOrderInfo
from backend.models import (
    Base,
    BookModel,
    FillModel,
    OrderModel,
    PositionModel,
    ReconciliationRunModel,
    TradingControlModel,
)
from backend.reconciliation import (
    EXTERNAL_CLOSE,
    GHOST_ORDER,
    ORPHAN,
    PARTIAL_DRIFT,
    BrokerSnapshot,
    ReconciliationResult,
    resolve_reconciliation,
    run_reconciliation,
)

# The stored SPY bull put spread used across tests: short 610 put, long 605 put,
# December 18 2026, 1 contract. OCC keys the comparison on both sides.
SHORT_OCC = "SPY261218P00610000"
LONG_OCC = "SPY261218P00605000"


def _leg_position(occ: str, qty: float, con_id: int = 1) -> LegPosition:
    return LegPosition(con_id=con_id, symbol="SPY", sec_type="OPT", position=qty, avg_cost=100.0, occ_symbol=occ)


def _stock_position(symbol: str = "SPY", qty: float = 100.0) -> LegPosition:
    return LegPosition(con_id=99, symbol=symbol, sec_type="STK", position=qty, avg_cost=650.0, occ_symbol=None)


def _spread_position(pos_id: str = "p1", book_id: str = "B01", contracts: int = 1) -> PositionModel:
    return PositionModel(
        id=pos_id,
        underlying="SPY",
        strategy_type="BULL_PUT_SPREAD",
        execution_mode="PAPER",
        legs=[
            {
                "option_type": "PUT",
                "direction": "SHORT",
                "strike": 610.0,
                "expiration": "2026-12-18",
                "delta": -0.3,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            },
            {
                "option_type": "PUT",
                "direction": "LONG",
                "strike": 605.0,
                "expiration": "2026-12-18",
                "delta": -0.15,
                "theta": 0.01,
                "vega": 0.05,
                "gamma": 0.01,
            },
        ],
        entry_date="2026-08-10",
        expiration_date="2026-12-18",
        entry_premium=1.25,
        premium_direction="CREDIT",
        current_value_per_share=1.10,
        contracts=contracts,
        max_profit=1.25,
        max_loss=3.75,
        notes="",
        rolls=0,
        status="OPEN",
        journal={},
        book_id=book_id,
    )


# The broker state that exactly matches one open spread position.
MATCHING_BROKER = (_leg_position(SHORT_OCC, -1.0, con_id=1), _leg_position(LONG_OCC, 1.0, con_id=2))


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        for book_id in ("B01", "B02"):
            session.add(
                BookModel(
                    id=book_id,
                    name=book_id,
                    config={},
                    config_version=1,
                    config_hash="",
                    starting_capital=10000.0,
                    cash_balance=10000.0,
                    status="ACTIVE",
                    created_at="t0",
                )
            )
        session.add(TradingControlModel(scope="GLOBAL", state="ACTIVE", reason="", actor="test", changed_at="t0"))
        await session.commit()
    yield maker
    await engine.dispose()


async def _run(maker, snapshot: BrokerSnapshot) -> ReconciliationResult:
    async with maker() as session:
        return await run_reconciliation(session, snapshot)


async def _global_state(maker) -> str:
    async with maker() as session:
        row = await session.get(TradingControlModel, "GLOBAL")
        return row.state


class TestCleanRun:
    @pytest.mark.asyncio
    async def test_exact_bijection_is_clean(self, session_maker):
        async with session_maker() as session:
            session.add(_spread_position())
            await session.commit()
        result = await _run(session_maker, BrokerSnapshot(positions=MATCHING_BROKER))
        assert result.clean
        assert result.drifts == ()
        assert await _global_state(session_maker) == "ACTIVE"

    @pytest.mark.asyncio
    async def test_empty_books_and_flat_broker_is_clean(self, session_maker):
        result = await _run(session_maker, BrokerSnapshot(positions=()))
        assert result.clean

    @pytest.mark.asyncio
    async def test_same_direction_sharing_across_books_sums(self, session_maker):
        async with session_maker() as session:
            session.add(_spread_position("p1", "B01"))
            session.add(_spread_position("p2", "B02"))
            await session.commit()
        broker = (_leg_position(SHORT_OCC, -2.0, con_id=1), _leg_position(LONG_OCC, 2.0, con_id=2))
        result = await _run(session_maker, BrokerSnapshot(positions=broker))
        assert result.clean

    @pytest.mark.asyncio
    async def test_run_row_persisted(self, session_maker):
        result = await _run(session_maker, BrokerSnapshot(positions=()))
        async with session_maker() as session:
            run = await session.get(ReconciliationRunModel, result.run_id)
        assert run.result == "CLEAN"
        assert run.drift_details is None


class TestDrift:
    @pytest.mark.asyncio
    async def test_orphan_option_halts_globally(self, session_maker):
        result = await _run(session_maker, BrokerSnapshot(positions=(_leg_position(SHORT_OCC, -1.0),)))
        assert not result.clean
        (drift,) = result.drifts
        assert drift.kind == ORPHAN
        assert drift.key == SHORT_OCC
        assert not drift.unexpected_instrument
        assert await _global_state(session_maker) == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_stock_orphan_is_no_stock_p1(self, session_maker):
        result = await _run(session_maker, BrokerSnapshot(positions=(_stock_position(),)))
        (drift,) = result.drifts
        assert drift.kind == ORPHAN
        assert drift.sec_type == "STK"
        assert drift.unexpected_instrument
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
        assert "UNEXPECTED_INSTRUMENT" in row.reason
        assert "No-Stock" in row.reason

    @pytest.mark.asyncio
    async def test_external_close_detected(self, session_maker):
        async with session_maker() as session:
            session.add(_spread_position())
            await session.commit()
        result = await _run(session_maker, BrokerSnapshot(positions=()))  # broker flat
        kinds = {d.kind for d in result.drifts}
        assert kinds == {EXTERNAL_CLOSE}
        assert {d.key for d in result.drifts} == {SHORT_OCC, LONG_OCC}
        assert await _global_state(session_maker) == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_partial_quantity_drift(self, session_maker):
        async with session_maker() as session:
            session.add(_spread_position(contracts=2))
            await session.commit()
        broker = (_leg_position(SHORT_OCC, -2.0, con_id=1), _leg_position(LONG_OCC, 1.0, con_id=2))
        result = await _run(session_maker, BrokerSnapshot(positions=broker))
        (drift,) = [d for d in result.drifts if d.kind == PARTIAL_DRIFT]
        assert drift.key == LONG_OCC
        assert drift.broker_qty == 1.0
        assert drift.expected_qty == 2.0

    @pytest.mark.asyncio
    async def test_ghost_basis_order_is_drift(self, session_maker):
        # Audit II R2 (#408): after a DB restore, a prior DB generation's
        # `basis:` orders rest at IBKR with no row to receive their fill —
        # the sync only queries refs the DB already knows, so nothing else
        # ever looks at them.
        snapshot = BrokerSnapshot(
            positions=(),
            open_orders=(
                OpenOrderInfo(order_ref="basis:B01:o_ghost:close", order_id=7, perm_id=90007, status="Submitted"),
                OpenOrderInfo(order_ref="manual-human-order", order_id=8, perm_id=90008, status="Submitted"),
            ),
        )
        result = await _run(session_maker, snapshot)
        (drift,) = result.drifts  # the human's own order is not flagged
        assert drift.kind == GHOST_ORDER
        assert drift.key == "basis:B01:o_ghost:close"
        assert await _global_state(session_maker) == "HALT_ENTRIES"
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
        assert "GHOST_ORDER" in row.reason

    @pytest.mark.asyncio
    async def test_known_resting_orders_are_not_ghosts(self, session_maker):
        # A SUBMITTED row and its GTC profit-taker (each with its OWN row
        # since #409) are the legitimate steady state — never drift.
        async with session_maker() as session:
            session.add(
                OrderModel(
                    id="o_live",
                    book_id="B01",
                    position_id=None,
                    order_ref="basis:B01:o_live:open",
                    ib_order_id=9,
                    ib_perm_id=90009,
                    action="OPEN",
                    combo_legs={},
                    order_type="LIMIT",
                    limit_price=-1.25,
                    decision_midpoint=-1.25,
                    status="SUBMITTED",
                    submitted_at="t0",
                    completed_at=None,
                    encumbered_risk=375.0,
                )
            )
            session.add(
                OrderModel(
                    id="o_live_tp",
                    book_id="B01",
                    position_id=None,
                    order_ref="basis:B01:o_live:open:tp",
                    ib_order_id=10,
                    ib_perm_id=90010,
                    action="CLOSE",
                    combo_legs={},
                    order_type="LIMIT",
                    limit_price=1.90,
                    decision_midpoint=1.90,
                    status="SUBMITTED",
                    submitted_at="t0",
                    completed_at=None,
                    encumbered_risk=0.0,
                )
            )
            await session.commit()
        snapshot = BrokerSnapshot(
            positions=(),
            open_orders=(
                OpenOrderInfo(order_ref="basis:B01:o_live:open", order_id=9, perm_id=90009, status="Submitted"),
                OpenOrderInfo(order_ref="basis:B01:o_live:open:tp", order_id=10, perm_id=90010, status="Submitted"),
            ),
        )
        result = await _run(session_maker, snapshot)
        assert result.clean

    @pytest.mark.asyncio
    async def test_tp_with_no_own_row_is_a_ghost_even_though_its_parent_is_live(self, session_maker):
        # #473: a `:tp` ref rides its OWN row since #409 — a resting TP whose
        # own row is terminal (e.g. the parent latched PARTIAL and cancelled
        # it, but the cancel hasn't reached the broker yet) IS the ghost the
        # old parent-ref fallback used to mask, even while the parent's row
        # is still legitimately live.
        async with session_maker() as session:
            session.add(
                OrderModel(
                    id="o_parent",
                    book_id="B01",
                    position_id=None,
                    order_ref="basis:B01:o_parent:open",
                    ib_order_id=11,
                    ib_perm_id=90011,
                    action="OPEN",
                    combo_legs={},
                    order_type="LIMIT",
                    limit_price=-1.25,
                    decision_midpoint=-1.25,
                    status="PARTIAL",
                    submitted_at="t0",
                    completed_at=None,
                    encumbered_risk=375.0,
                )
            )
            session.add(
                OrderModel(
                    id="o_parent_tp",
                    book_id="B01",
                    position_id=None,
                    order_ref="basis:B01:o_parent:open:tp",
                    ib_order_id=12,
                    ib_perm_id=90012,
                    action="CLOSE",
                    combo_legs={},
                    order_type="LIMIT",
                    limit_price=1.90,
                    decision_midpoint=1.90,
                    status="CANCELLED",  # the sync latched PARTIAL and cancelled the TP
                    submitted_at="t0",
                    completed_at="t1",
                    encumbered_risk=0.0,
                )
            )
            await session.commit()
        snapshot = BrokerSnapshot(
            positions=(),
            open_orders=(
                # Still resting at IBKR — the cancel hasn't reached the broker yet.
                OpenOrderInfo(order_ref="basis:B01:o_parent:open:tp", order_id=12, perm_id=90012, status="Submitted"),
            ),
        )
        result = await _run(session_maker, snapshot)
        (drift,) = result.drifts
        assert drift.kind == GHOST_ORDER
        assert drift.key == "basis:B01:o_parent:open:tp"

    @pytest.mark.asyncio
    async def test_cancel_in_flight_broker_status_is_not_a_ghost(self, session_maker):
        # #473: an order the operator (or the sync) already cancelled sits
        # briefly in PendingCancel/ApiCancelled at IBKR before it clears the
        # open-orders feed — flagging it halts the book telling the operator
        # to do what they already did.
        snapshot = BrokerSnapshot(
            positions=(),
            open_orders=(
                OpenOrderInfo(
                    order_ref="basis:B01:o_cancelling:close", order_id=13, perm_id=90013, status="PendingCancel"
                ),
                OpenOrderInfo(
                    order_ref="basis:B01:o_cancelled_api:close", order_id=14, perm_id=90014, status="ApiCancelled"
                ),
            ),
        )
        result = await _run(session_maker, snapshot)
        assert result.clean

    @pytest.mark.asyncio
    async def test_drift_never_mutates_positions(self, session_maker):
        async with session_maker() as session:
            session.add(_spread_position())
            await session.commit()
        await _run(session_maker, BrokerSnapshot(positions=()))
        async with session_maker() as session:
            pos = await session.get(PositionModel, "p1")
        assert pos.status == "OPEN"  # flagged and halted, never auto-adjusted
        assert pos.contracts == 1


class TestFillBackfill:
    async def _seed_order(self, maker, ref: str = "basis:B01:o1:open") -> None:
        async with maker() as session:
            session.add(
                OrderModel(
                    id="o1",
                    book_id="B01",
                    position_id=None,
                    order_ref=ref,
                    ib_order_id=100,
                    ib_perm_id=90100,
                    action="OPEN",
                    combo_legs=[],
                    order_type="LIMIT",
                    limit_price=-1.25,
                    decision_midpoint=-1.30,
                    status="SUBMITTED",
                    submitted_at="t0",
                    completed_at=None,
                )
            )
            await session.commit()

    def _execution(self, exec_id: str, ref: str, exec_time: str = "2024-01-01T00:00:00+00:00") -> FillInfo:
        return FillInfo(
            exec_id=exec_id,
            con_id=1,
            side="SLD",
            quantity=1.0,
            price=6.10,
            order_ref=ref,
            commission=1.05,
            exec_time=exec_time,
        )

    @pytest.mark.asyncio
    async def test_missed_fill_ingested_with_book_attribution(self, session_maker):
        await self._seed_order(session_maker)
        snapshot = BrokerSnapshot(positions=(), executions=(self._execution("e1", "basis:B01:o1:open"),))
        result = await _run(session_maker, snapshot)
        assert result.fills_backfilled == 1
        async with session_maker() as session:
            fill = (await session.execute(select(FillModel))).scalar_one()
        assert fill.book_id == "B01"
        assert fill.order_id == "o1"
        assert fill.raw["source"] == "reconciliation_backfill"
        # The commission is real money (#276): debited from book cash at
        # ingestion, exactly once (exec-id dedupe).
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
        assert book.cash_balance == 10000.0 - 1.05

    @pytest.mark.asyncio
    async def test_tp_child_ref_maps_to_parent_order(self, session_maker):
        await self._seed_order(session_maker)
        snapshot = BrokerSnapshot(positions=(), executions=(self._execution("e2", "basis:B01:o1:open:tp"),))
        result = await _run(session_maker, snapshot)
        assert result.fills_backfilled == 1

    @pytest.mark.asyncio
    async def test_duplicate_exec_id_skipped(self, session_maker):
        await self._seed_order(session_maker)
        ex = self._execution("e1", "basis:B01:o1:open")
        await _run(session_maker, BrokerSnapshot(positions=(), executions=(ex,)))
        result = await _run(session_maker, BrokerSnapshot(positions=(), executions=(ex,)))
        assert result.fills_backfilled == 0
        async with session_maker() as session:
            fills = (await session.execute(select(FillModel))).scalars().all()
            book = await session.get(BookModel, "B01")
        assert len(fills) == 1
        assert book.cash_balance == 10000.0 - 1.05  # commission debited ONCE

    @pytest.mark.asyncio
    async def test_unknown_ref_reported_not_guessed(self, session_maker):
        snapshot = BrokerSnapshot(positions=(), executions=(self._execution("e9", "manual-order-nobody-knows"),))
        result = await _run(session_maker, snapshot)
        assert result.fills_backfilled == 0
        assert result.unknown_ref_exec_ids == ("e9",)
        async with session_maker() as session:
            fills = (await session.execute(select(FillModel))).scalars().all()
        assert fills == []


class TestResolution:
    @pytest.mark.asyncio
    async def test_resolution_recorded_but_never_auto_resumes(self, session_maker):
        result = await _run(session_maker, BrokerSnapshot(positions=(_stock_position(),)))
        async with session_maker() as session:
            await resolve_reconciliation(session, result.run_id, "closed assigned stock manually, verified flat")
        async with session_maker() as session:
            run = await session.get(ReconciliationRunModel, result.run_id)
        assert run.resolution.startswith("closed assigned stock")
        assert run.resolved_at is not None
        assert await _global_state(session_maker) == "HALT_ENTRIES"  # resume stays console-only

    @pytest.mark.asyncio
    async def test_unknown_run_id_raises(self, session_maker):
        async with session_maker() as session:
            with pytest.raises(ValueError, match="No reconciliation run"):
                await resolve_reconciliation(session, 424242, "n/a")
