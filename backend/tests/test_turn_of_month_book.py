"""The turn-of-month book's executor plumbing (#1092): run_turn_of_month_rebalances
and turn_of_month_watch_notes, against a real DB session and a fake broker —
no network. Mirrors test_share_book.py's rig for B36, scoped to B38.

Fail-closed paths first: orders only on the exact scheduled entry/exit
evening, never mid-window or mid-month, and never at all when the calendar
can't say — the book stays in TBIL and the digest says why."""

import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import share_book
from backend.broker import BrokerError, FillInfo, PlacedOrder, ReconcileReport, RefState
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    IndexHistoryModel,
    ShareHoldingModel,
    ShareOrderModel,
    TradingControlModel,
)
from backend.reconciliation import BrokerSnapshot, run_reconciliation
from backend.seeds import LAB_BOOKS

B38_CONFIG = next(b for b in LAB_BOOKS if b["id"] == "B38")["config"]
CLOSES = {"SCHB": 300.0, "TBIL": 100.5}

# Oct 30 2026 (Fri) is October's last trading day; Nov 2/3/4 are the first
# three trading days of November (Nov 1 is a Sunday) — the window this
# module's fixtures exercise.
ENTRY_EVENING = datetime.date(2026, 10, 29)  # evening before the window opens
WINDOW_FIRST_DAY = datetime.date(2026, 10, 30)
WINDOW_MID_DAY = datetime.date(2026, 11, 3)
EXIT_EVENING = datetime.date(2026, 11, 4)  # the window's own last day
AFTER_EXIT = datetime.date(2026, 11, 5)
MID_MONTH = datetime.date(2026, 10, 15)


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with m() as session:
        session.add(
            BookModel(
                id="B38",
                name="B38",
                config=B38_CONFIG,
                config_version=1,
                config_hash="hash-B38",
                starting_capital=10000.0,
                cash_balance=10000.0,
                status="ACTIVE",
                created_at="2026-09-01T00:00:00+00:00",
            )
        )
        session.add(TradingControlModel(scope="B38", state="ACTIVE", reason="", actor="t", changed_at="t0"))
        session.add(TradingControlModel(scope="GLOBAL", state="ACTIVE", reason="", actor="t", changed_at="t0"))
        for day in (ENTRY_EVENING, WINDOW_FIRST_DAY, WINDOW_MID_DAY, EXIT_EVENING, AFTER_EXIT, MID_MONTH):
            for symbol, close in CLOSES.items():
                session.add(IndexHistoryModel(date=day.isoformat(), symbol=symbol, close=close))
        await session.commit()
    yield m
    await engine.dispose()


class FakeBroker:
    def __init__(self, fail_on: str | None = None):
        self.placed: list[tuple[str, str, int, float, str]] = []
        self.fail_on = fail_on

    def place_share_order(self, symbol, side, quantity, limit_price, ref):
        if symbol == self.fail_on:
            raise BrokerError(f"refused {symbol}")
        self.placed.append((symbol, side, quantity, limit_price, ref))
        return PlacedOrder(order_id=len(self.placed), perm_id=2000 + len(self.placed), ref=ref, status="PreSubmitted")


async def _rebalance(m, broker, day) -> share_book.RebalanceResult:
    async with m() as session:
        return await share_book.run_turn_of_month_rebalances(session, broker, day)


async def _holding(m, symbol: str) -> float | None:
    async with m() as session:
        row = await session.get(ShareHoldingModel, ("B38", symbol))
        return row.quantity if row else None


async def _set_holding(m, symbol: str, quantity: float) -> None:
    async with m() as session:
        session.add(ShareHoldingModel(book_id="B38", symbol=symbol, quantity=quantity, updated_at="t0"))
        if symbol != "TBIL":
            # Starting from SCHB implies the book sold its TBIL to buy it;
            # keep cash internally consistent for the tests that check it.
            book = await session.get(BookModel, "B38")
            book.cash_balance = 10_000.0 - quantity * CLOSES[symbol] - 1.0
        await session.commit()


async def _orders(m) -> list[ShareOrderModel]:
    async with m() as session:
        return list((await session.execute(select(ShareOrderModel))).scalars().all())


async def _events(m, event_type: str) -> list[AuditEventModel]:
    async with m() as session:
        return list((await session.execute(select(AuditEventModel).filter_by(event_type=event_type))).scalars().all())


async def _watch_notes(m, day) -> list[str]:
    async with m() as session:
        return await share_book.turn_of_month_watch_notes(session, day)


class TestEntryAndExitEvenings:
    @pytest.mark.asyncio
    async def test_entry_evening_buys_the_risk_symbol(self, maker):
        broker = FakeBroker()
        result = await _rebalance(maker, broker, ENTRY_EVENING)
        # $10,000 / $300 floors to 33 shares, but the funding check (buy
        # limit 2% through the close, less a $1 commission reserve) shrinks
        # it to 32 so the book never borrows — etf_trend.rebalance_orders,
        # reused unchanged.
        assert [(s, side, q) for s, side, q, *_ in broker.placed] == [("SCHB", "BUY", 32)]
        assert result.placed == [ref for *_, ref in broker.placed]
        assert any("turn of month" in n for n in result.notes)
        assert len(await _events(maker, share_book.TURN_OF_MONTH_SIGNAL)) == 1

    @pytest.mark.asyncio
    async def test_mid_window_evening_places_nothing(self, maker):
        await _set_holding(maker, "SCHB", 33.0)
        broker = FakeBroker()
        result = await _rebalance(maker, broker, WINDOW_MID_DAY)
        assert broker.placed == [] and result.notes == [] and await _orders(maker) == []

    @pytest.mark.asyncio
    async def test_mid_month_evening_places_nothing(self, maker):
        broker = FakeBroker()
        result = await _rebalance(maker, broker, MID_MONTH)
        assert broker.placed == [] and result.notes == [] and await _orders(maker) == []

    @pytest.mark.asyncio
    async def test_exit_evening_sells_the_risk_symbol(self, maker):
        await _set_holding(maker, "SCHB", 33.0)
        broker = FakeBroker()
        result = await _rebalance(maker, broker, EXIT_EVENING)
        # Sell SCHB first, then sweep the proceeds into TBIL (etf_trend's
        # sells-first-then-buys order, reused unchanged).
        assert [(s, side) for s, side, *_ in broker.placed] == [("SCHB", "SELL"), ("TBIL", "BUY")]
        assert result.placed == [ref for *_, ref in broker.placed]

    @pytest.mark.asyncio
    async def test_not_holding_the_risk_symbol_on_exit_evening_places_nothing(self, maker):
        # Already in TBIL on the scheduled exit evening: nothing to sell.
        broker = FakeBroker()
        result = await _rebalance(maker, broker, EXIT_EVENING)
        assert broker.placed == [] and result.notes == []

    @pytest.mark.asyncio
    async def test_a_held_order_from_a_failed_exit_retries_the_next_evening(self, maker):
        # #1092: unlike B36's "never catch up," an unfilled exit retries
        # every evening until the book is actually back in TBIL.
        await _set_holding(maker, "SCHB", 33.0)
        broker = FakeBroker()
        await _rebalance(maker, broker, EXIT_EVENING)
        assert len(broker.placed) == 2  # SELL SCHB, BUY TBIL
        # Neither order has filled yet (still STAGED/SUBMITTED) — the
        # catch-up does not re-place while the first attempt is still
        # pending; it waits for that night's sync to resolve them first.
        broker.placed = []
        again = await _rebalance(maker, broker, AFTER_EXIT)
        assert broker.placed == []
        assert "pending" in again.notes[0]

    @pytest.mark.asyncio
    async def test_halted_book_skips_entry(self, maker):
        async with maker() as session:
            (await session.get(TradingControlModel, "B38")).state = "HALT_ENTRIES"
            await session.commit()
        broker = FakeBroker()
        result = await _rebalance(maker, broker, ENTRY_EVENING)
        assert broker.placed == [] and await _orders(maker) == []
        assert "SKIPPED" in result.notes[0]
        assert len(await _events(maker, share_book.TURN_OF_MONTH_SKIPPED)) == 1

    @pytest.mark.asyncio
    async def test_broker_refusal_stops_further_orders_this_evening(self, maker):
        broker = FakeBroker(fail_on="SCHB")
        result = await _rebalance(maker, broker, ENTRY_EVENING)
        assert broker.placed == []
        assert "refused" in result.notes[-1]
        assert len(await _events(maker, share_book.SHARE_ORDER_REJECTED)) == 1


class TestFailClosedCalendar:
    @pytest.mark.asyncio
    async def test_an_unverified_year_never_enters(self, maker):
        async with maker() as session:
            book = await session.get(BookModel, "B38")
            book.created_at = "2028-01-01T00:00:00+00:00"
            session.add(IndexHistoryModel(date="2028-01-14", symbol="SCHB", close=300.0))
            session.add(IndexHistoryModel(date="2028-01-14", symbol="TBIL", close=100.5))
            await session.commit()
        broker = FakeBroker()
        result = await _rebalance(maker, broker, datetime.date(2028, 1, 14))
        assert broker.placed == [] and result.notes == []

    @pytest.mark.asyncio
    async def test_an_unverified_year_exits_a_held_position(self, maker):
        # Fail closed means "stay in TBIL": a book somehow holding the risk
        # symbol during an unreadable calendar sells to cash rather than
        # waiting for calendar certainty that may never come.
        await _set_holding(maker, "SCHB", 10.0)
        async with maker() as session:
            session.add(IndexHistoryModel(date="2028-01-14", symbol="SCHB", close=300.0))
            session.add(IndexHistoryModel(date="2028-01-14", symbol="TBIL", close=100.5))
            await session.commit()
        broker = FakeBroker()
        result = await _rebalance(maker, broker, datetime.date(2028, 1, 14))
        assert [(s, side) for s, side, *_ in broker.placed] == [("SCHB", "SELL"), ("TBIL", "BUY")]
        assert any("fail closed" in n or "TBIL" in n for n in result.notes)


class TestWatchNotes:
    @pytest.mark.asyncio
    async def test_holding_matches_the_calendar_is_silent(self, maker):
        await _set_holding(maker, "SCHB", 10.0)
        assert await _watch_notes(maker, WINDOW_MID_DAY) == []

    @pytest.mark.asyncio
    async def test_in_window_without_the_risk_symbol_is_loud(self, maker):
        notes = await _watch_notes(maker, WINDOW_MID_DAY)
        assert len(notes) == 1
        assert "not holding SCHB" in notes[0]

    @pytest.mark.asyncio
    async def test_out_of_window_still_holding_the_risk_symbol_is_loud(self, maker):
        await _set_holding(maker, "SCHB", 10.0)
        notes = await _watch_notes(maker, MID_MONTH)
        assert len(notes) == 1
        assert "has not filled yet" in notes[0]

    @pytest.mark.asyncio
    async def test_unreadable_calendar_is_loud(self, maker):
        notes = await _watch_notes(maker, datetime.date(2028, 1, 14))
        assert len(notes) == 1
        assert "cannot be determined" in notes[0]


class TestFillsHoldingsReconcile:
    @pytest.mark.asyncio
    async def test_entry_fill_books_into_holdings_and_reconciles_clean(self, maker):
        broker = FakeBroker()
        await _rebalance(maker, broker, ENTRY_EVENING)
        (symbol, _side, qty, _limit, ref) = broker.placed[0]
        pending = await _orders(maker)
        assert len(pending) == 1 and pending[0].order_ref == ref

        async with maker() as session:
            pending_orders = await share_book.pending_share_orders(session)
            report = ReconcileReport(states={ref: RefState.FILLED})
            executions = (
                FillInfo(
                    exec_id="e1",
                    con_id=7,
                    side="BOT",
                    quantity=float(qty),
                    price=CLOSES[symbol],
                    order_ref=ref,
                    commission=1.0,
                    exec_time="2026-10-30T13:31:00+00:00",
                ),
            )
            notes = await share_book.sync_share_orders(session, pending_orders, report, executions, 1)
            await session.commit()
        assert any("booked" in n for n in notes)
        assert await _holding(maker, "SCHB") == float(qty)

        async with maker() as session:
            from backend.broker import LegPosition

            snapshot = BrokerSnapshot(
                positions=(LegPosition(con_id=7, symbol="SCHB", sec_type="STK", position=float(qty), avg_cost=1.0),),
                executions=executions,
                open_orders=(),
            )
            recon = await run_reconciliation(session, snapshot, today=WINDOW_FIRST_DAY.isoformat())
            await session.commit()
        assert recon.clean, recon.drifts
