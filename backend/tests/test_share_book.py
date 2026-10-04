"""Share-book plumbing (#1054): the month-end rebalance, the share-fill sync,
and the reconciliation, MTM and sync-pending seams around them.

Broker and data are fakes — no network. Fail-closed paths first: a halted
book, a pending order, an unpriced holding, a FILLED verdict without its
executions, an assignment hiding behind a pending order."""

import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import share_book
from backend.anomaly import check_pnl_shock
from backend.broker import BrokerError, FillInfo, LegPosition, OpenOrderInfo, PlacedOrder, ReconcileReport, RefState
from backend.etf_trend import month_end_dates
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    BookMtmHistoryModel,
    FillModel,
    IndexHistoryModel,
    OrderModel,
    ShareHoldingModel,
    ShareOrderModel,
    TradingControlModel,
)
from backend.reconciliation import (
    GHOST_ORDER,
    ORPHAN,
    SHARE_DRIFT,
    BrokerSnapshot,
    DriftItem,
    _backfill_missed_fills,
    drift_is_sync_pending,
    run_reconciliation,
)
from backend.seeds import LAB_BOOKS

SIGNAL_DAY = datetime.date(2026, 10, 30)
NEXT_DAY = datetime.date(2026, 11, 2)
B36_CONFIG = next(b for b in LAB_BOOKS if b["id"] == "B36")["config"]
MENU = ("VTI", "VEA", "IEF", "GLD", "VNQ", "DBMF")
TODAY_CLOSES = {"VTI": 300.0, "VEA": 55.0, "IEF": 95.0, "GLD": 330.0, "VNQ": 90.0, "DBMF": 28.0, "SGOV": 100.5}


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with m() as session:
        for book_id, config in (
            ("B01", {"engine_variant": "V0", "underlying": "XSP", "envelope": {}}),
            ("B36", B36_CONFIG),
        ):
            session.add(
                BookModel(
                    id=book_id,
                    name=book_id,
                    config=config,
                    config_version=1,
                    config_hash=f"hash-{book_id}",
                    starting_capital=10000.0,
                    cash_balance=10000.0,
                    status="ACTIVE",
                    created_at="2026-10-01T00:00:00+00:00",
                )
            )
            session.add(TradingControlModel(scope=book_id, state="ACTIVE", reason="", actor="t", changed_at="t0"))
        session.add(TradingControlModel(scope="GLOBAL", state="ACTIVE", reason="", actor="t", changed_at="t0"))
        await session.commit()
    yield m
    await engine.dispose()


async def _seed_history(m, trending: set[str], *, skip: dict[str, str] | None = None) -> None:
    """Ten month-ends per menu asset ending on SIGNAL_DAY at TODAY_CLOSES:
    a rising series for trending assets, a falling one otherwise. SGOV gets
    today's close only. *skip* drops one {symbol: date} row."""
    dates = month_end_dates(SIGNAL_DAY, 10)
    async with m() as session:
        for symbol in MENU:
            close = TODAY_CLOSES[symbol]
            step = close * 0.01 * (1 if symbol in trending else -1)
            for i, d in enumerate(dates):
                iso = d.isoformat()
                if skip and skip.get(symbol) == iso:
                    continue
                session.add(IndexHistoryModel(date=iso, symbol=symbol, close=close - step * (len(dates) - 1 - i)))
        session.add(IndexHistoryModel(date=SIGNAL_DAY.isoformat(), symbol="SGOV", close=TODAY_CLOSES["SGOV"]))
        await session.commit()


class FakeShareBroker:
    def __init__(self, fail_on: str | None = None):
        self.placed: list[tuple[str, str, int, float, str]] = []
        self.fail_on = fail_on

    def place_share_order(self, symbol, side, quantity, limit_price, ref):
        if symbol == self.fail_on:
            raise BrokerError(f"refused {symbol}")
        self.placed.append((symbol, side, quantity, limit_price, ref))
        return PlacedOrder(order_id=len(self.placed), perm_id=1000 + len(self.placed), ref=ref, status="PreSubmitted")


async def _orders(m) -> list[ShareOrderModel]:
    async with m() as session:
        return list((await session.execute(select(ShareOrderModel))).scalars().all())


async def _events(m, event_type: str) -> list[AuditEventModel]:
    async with m() as session:
        return list((await session.execute(select(AuditEventModel).filter_by(event_type=event_type))).scalars().all())


async def _rebalance(m, broker, day=SIGNAL_DAY) -> share_book.RebalanceResult:
    async with m() as session:
        return await share_book.run_etf_trend_rebalances(session, broker, day)


async def _add_order(m, *, symbol="VTI", side="BUY", quantity=3, status="SUBMITTED", order_id="o1") -> ShareOrderModel:
    order = ShareOrderModel(
        id=order_id,
        book_id="B36",
        order_ref=share_book.share_order_ref("B36", order_id),
        symbol=symbol,
        side=side,
        quantity=quantity,
        limit_price=306.0,
        decision_close=300.0,
        signal_date=SIGNAL_DAY.isoformat(),
        status=status,
        config_hash="hash-B36",
        created_at="t0",
        fills=[],
    )
    async with m() as session:
        session.add(order)
        await session.commit()
    return order


def _exec(ref: str, qty: float, price: float, exec_id: str = "e1", commission: float | None = 1.0) -> FillInfo:
    return FillInfo(
        exec_id=exec_id,
        con_id=7,
        side="BOT",
        quantity=qty,
        price=price,
        order_ref=ref,
        commission=commission,
        exec_time="2026-11-02T14:31:00+00:00",
    )


async def _sync(m, report: ReconcileReport, executions=(), gap: int | None = 1) -> list[str]:
    async with m() as session:
        pending = await share_book.pending_share_orders(session)
        notes = await share_book.sync_share_orders(session, pending, report, tuple(executions), gap)
        await session.commit()
    return notes


def _report(ref: str, state: RefState, rejection: str | None = None) -> ReconcileReport:
    return ReconcileReport(states={ref: state}, rejections={ref: rejection} if rejection else {})


async def _holding(m, symbol: str) -> float | None:
    async with m() as session:
        row = await session.get(ShareHoldingModel, ("B36", symbol))
        return row.quantity if row else None


async def _cash(m, book_id: str = "B36") -> float:
    async with m() as session:
        return (await session.get(BookModel, book_id)).cash_balance


def _stk(symbol: str, qty: float) -> LegPosition:
    return LegPosition(con_id=7, symbol=symbol, sec_type="STK", position=qty, avg_cost=1.0)


# ---------------------------------------------------------------------------
# The month-end rebalance
# ---------------------------------------------------------------------------


class TestRebalanceRefusals:
    @pytest.mark.asyncio
    async def test_non_signal_day_places_nothing(self, maker):
        await _seed_history(maker, set(MENU))
        broker = FakeShareBroker()
        result = await _rebalance(maker, broker, datetime.date(2026, 10, 29))
        assert broker.placed == [] and result.notes == [] and await _orders(maker) == []

    @pytest.mark.asyncio
    async def test_halted_book_skips_the_month(self, maker):
        await _seed_history(maker, set(MENU))
        async with maker() as session:
            (await session.get(TradingControlModel, "B36")).state = "HALT_ENTRIES"
            await session.commit()
        broker = FakeShareBroker()
        result = await _rebalance(maker, broker)
        assert broker.placed == [] and await _orders(maker) == []
        assert "SKIPPED" in result.notes[0] and "no other day trades" in result.notes[0]
        assert len(await _events(maker, share_book.ETF_TREND_SKIPPED)) == 1

    @pytest.mark.asyncio
    async def test_global_halt_skips_the_month(self, maker):
        await _seed_history(maker, set(MENU))
        async with maker() as session:
            (await session.get(TradingControlModel, "GLOBAL")).state = "HALT_ENTRIES"
            await session.commit()
        broker = FakeShareBroker()
        result = await _rebalance(maker, broker)
        assert broker.placed == []
        assert "GLOBAL=HALT_ENTRIES" in result.notes[0]

    @pytest.mark.asyncio
    async def test_a_still_pending_order_skips_the_month(self, maker):
        await _seed_history(maker, set(MENU))
        await _add_order(maker)
        broker = FakeShareBroker()
        result = await _rebalance(maker, broker)
        assert broker.placed == []
        assert "still pending" in result.notes[0]

    @pytest.mark.asyncio
    async def test_held_symbol_without_todays_close_skips_the_month(self, maker):
        await _seed_history(maker, set(MENU), skip={"VNQ": SIGNAL_DAY.isoformat()})
        async with maker() as session:
            session.add(ShareHoldingModel(book_id="B36", symbol="VNQ", quantity=10.0, updated_at="t0"))
            await session.commit()
        broker = FakeShareBroker()
        result = await _rebalance(maker, broker)
        assert broker.placed == []
        assert "no close today for held symbol(s) VNQ" in result.notes[0]

    @pytest.mark.asyncio
    async def test_fractional_holding_skips_the_month(self, maker):
        await _seed_history(maker, set(MENU))
        async with maker() as session:
            session.add(ShareHoldingModel(book_id="B36", symbol="VTI", quantity=2.5, updated_at="t0"))
            await session.commit()
        result = await _rebalance(maker, FakeShareBroker())
        assert "not whole shares: VTI" in result.notes[0]

    @pytest.mark.asyncio
    async def test_missing_cash_leg_close_skips_the_month(self, maker):
        await _seed_history(maker, set(MENU))
        async with maker() as session:
            row = await session.get(IndexHistoryModel, (SIGNAL_DAY.isoformat(), "SGOV"))
            await session.delete(row)
            await session.commit()
        broker = FakeShareBroker()
        result = await _rebalance(maker, broker)
        assert broker.placed == []
        assert "cash leg SGOV" in result.notes[0]

    @pytest.mark.asyncio
    async def test_options_books_are_never_rebalanced(self, maker):
        await _seed_history(maker, set(MENU))
        async with maker() as session:
            (await session.get(BookModel, "B36")).status = "RETIRED"
            await session.commit()
        broker = FakeShareBroker()
        result = await _rebalance(maker, broker)
        assert broker.placed == [] and result.notes == []


class TestRebalancePlacement:
    @pytest.mark.asyncio
    async def test_first_month_buys_trending_slots_and_cash_leg(self, maker):
        await _seed_history(maker, {"VTI", "GLD"})
        broker = FakeShareBroker()
        result = await _rebalance(maker, broker)
        placed = {(s, side): q for s, side, q, _, _ in broker.placed}
        # slot = 10000/6 = 1666.67: VTI 5, GLD 5; the rest to SGOV.
        assert placed[("VTI", "BUY")] == 5
        assert placed[("GLD", "BUY")] == 5
        assert ("SGOV", "BUY") in placed
        assert all(side == "BUY" for _, side in placed)
        orders = await _orders(maker)
        assert {o.status for o in orders} == {"SUBMITTED"}
        assert all(o.order_ref.startswith("basis:B36:") and o.order_ref.endswith(":share") for o in orders)
        assert all(o.config_hash == "hash-B36" and o.signal_date == SIGNAL_DAY.isoformat() for o in orders)
        assert result.placed == [o for _, _, _, _, o in broker.placed]
        assert "trending GLD, VTI" in result.notes[0] or "trending VTI, GLD" in result.notes[0]
        (signal,) = await _events(maker, share_book.ETF_TREND_SIGNAL)
        assert signal.payload["readings"]["VTI"]["status"] == "TRENDING"
        # Funding: every buy at its limit fits the book's cash.
        assert sum(q * limit for _, _, q, limit, _ in broker.placed) <= 10_000.0
        # The choke point was read before every submission (one pre-check + one per order).
        assert len(await _events(maker, "CONTROL_CHECK")) == 1 + len(broker.placed)

    @pytest.mark.asyncio
    async def test_missing_history_sends_that_slot_to_cash_and_says_so(self, maker):
        dropped = month_end_dates(SIGNAL_DAY, 10)[2].isoformat()
        await _seed_history(maker, set(MENU), skip={"DBMF": dropped})
        broker = FakeShareBroker()
        result = await _rebalance(maker, broker)
        assert "DBMF" not in {s for s, *_ in broker.placed}
        assert "MISSING HISTORY" in result.notes[0] and "DBMF" in result.notes[0]

    @pytest.mark.asyncio
    async def test_rebalance_orders_only_the_deltas(self, maker):
        await _seed_history(maker, {"VTI"})
        async with maker() as session:
            session.add(ShareHoldingModel(book_id="B36", symbol="VTI", quantity=5.0, updated_at="t0"))
            session.add(ShareHoldingModel(book_id="B36", symbol="IEF", quantity=17.0, updated_at="t0"))
            (await session.get(BookModel, "B36")).cash_balance = 10_000.0 - 5 * 300.0 - 17 * 95.0
            await session.commit()
        broker = FakeShareBroker()
        await _rebalance(maker, broker)
        sides = {s: (side, q) for s, side, q, _, _ in broker.placed}
        assert "VTI" not in sides  # already on target
        assert sides["IEF"] == ("SELL", 17)  # no longer trending
        assert sides["SGOV"][0] == "BUY"
        assert broker.placed[0][0] == "IEF"  # sells first

    @pytest.mark.asyncio
    async def test_on_target_places_nothing(self, maker):
        await _seed_history(maker, set())
        async with maker() as session:
            session.add(ShareHoldingModel(book_id="B36", symbol="SGOV", quantity=99.0, updated_at="t0"))
            (await session.get(BookModel, "B36")).cash_balance = 10_000.0 - 99 * 100.5
            await session.commit()
        broker = FakeShareBroker()
        result = await _rebalance(maker, broker)
        assert broker.placed == []
        assert "already on target" in result.notes[-1]

    @pytest.mark.asyncio
    async def test_broker_error_rejects_that_order_and_stops(self, maker):
        await _seed_history(maker, {"VTI", "GLD"})
        broker = FakeShareBroker(fail_on="GLD")
        result = await _rebalance(maker, broker)
        statuses = {o.symbol: o.status for o in await _orders(maker)}
        assert statuses["GLD"] == "REJECTED"
        assert "SGOV" not in statuses and "VTI" not in statuses  # GLD sorts first among buys; nothing after it
        assert any("refused by the broker" in n for n in result.notes)
        assert len(await _events(maker, share_book.SHARE_ORDER_REJECTED)) == 1

    @pytest.mark.asyncio
    async def test_halt_landing_mid_rebalance_cancels_and_stops(self, maker, monkeypatch):
        await _seed_history(maker, {"VTI", "GLD"})
        calls = {"n": 0}
        real = share_book.assert_entries_allowed

        async def _flaky(session, book_id=None, actor="executor"):
            calls["n"] += 1
            if calls["n"] == 3:  # pre-check, first order, then the halt lands
                from backend.trading_control import TradingHaltedError

                raise TradingHaltedError("B36", "HALT_ENTRIES")
            await real(session, book_id, actor)

        monkeypatch.setattr(share_book, "assert_entries_allowed", _flaky)
        broker = FakeShareBroker()
        result = await _rebalance(maker, broker)
        assert len(broker.placed) == 1
        statuses = sorted(o.status for o in await _orders(maker))
        assert statuses == ["CANCELLED", "SUBMITTED"]
        assert any("halted mid-rebalance" in n for n in result.notes)

    @pytest.mark.asyncio
    async def test_losing_book_never_sizes_past_its_equity(self, maker):
        await _seed_history(maker, set())
        async with maker() as session:
            (await session.get(BookModel, "B36")).cash_balance = 4000.0
            await session.commit()
        broker = FakeShareBroker()
        await _rebalance(maker, broker)
        assert sum(q * limit for _, _, q, limit, _ in broker.placed) <= 4000.0


# ---------------------------------------------------------------------------
# The share-fill sync — the only writer of share_holdings
# ---------------------------------------------------------------------------


class TestShareSync:
    @pytest.mark.asyncio
    async def test_full_fill_books_holding_cash_and_commission(self, maker):
        order = await _add_order(maker, quantity=3)
        notes = await _sync(maker, _report(order.order_ref, RefState.FILLED), [_exec(order.order_ref, 3, 301.0)])
        assert await _holding(maker, "VTI") == 3.0
        assert await _cash(maker) == pytest.approx(10_000.0 - 903.0 - 1.0)
        (row,) = await _orders(maker)
        assert (row.status, row.filled_quantity, row.avg_fill_price, row.commission) == ("FILLED", 3.0, 301.0, 1.0)
        assert row.fills[0]["exec_id"] == "e1"
        assert "booked" in notes[0]
        assert len(await _events(maker, share_book.SHARE_FILL_BOOKED)) == 1

    @pytest.mark.asyncio
    async def test_booked_fill_reconciles_clean(self, maker):
        order = await _add_order(maker, quantity=3)
        await _sync(maker, _report(order.order_ref, RefState.FILLED), [_exec(order.order_ref, 3, 301.0)])
        async with maker() as session:
            result = await run_reconciliation(session, BrokerSnapshot(positions=(_stk("VTI", 3.0),)))
        assert result.clean

    @pytest.mark.asyncio
    async def test_sell_fill_reduces_holding_and_credits_cash(self, maker):
        async with maker() as session:
            session.add(ShareHoldingModel(book_id="B36", symbol="IEF", quantity=17.0, updated_at="t0"))
            await session.commit()
        order = await _add_order(maker, symbol="IEF", side="SELL", quantity=17)
        await _sync(maker, _report(order.order_ref, RefState.FILLED), [_exec(order.order_ref, 17, 94.0)])
        assert await _holding(maker, "IEF") == 0.0
        assert await _cash(maker) == pytest.approx(10_000.0 + 17 * 94.0 - 1.0)

    @pytest.mark.asyncio
    async def test_partial_fill_then_expiry_books_exactly_what_filled(self, maker):
        order = await _add_order(maker, quantity=5)
        notes = await _sync(
            maker,
            _report(order.order_ref, RefState.CANCELLED),
            [_exec(order.order_ref, 2, 300.0, "e1", 0.5), _exec(order.order_ref, 1, 302.0, "e2", 0.5)],
        )
        assert await _holding(maker, "VTI") == 3.0
        (row,) = await _orders(maker)
        assert row.status == "CANCELLED" and row.filled_quantity == 3.0
        assert row.avg_fill_price == pytest.approx(902.0 / 3)
        assert await _cash(maker) == pytest.approx(10_000.0 - 902.0 - 1.0)
        assert "3 of 5 filled" in notes[0]

    @pytest.mark.asyncio
    async def test_unfilled_expiry_changes_nothing(self, maker):
        order = await _add_order(maker)
        notes = await _sync(maker, _report(order.order_ref, RefState.CANCELLED))
        assert await _holding(maker, "VTI") is None
        assert await _cash(maker) == 10_000.0
        assert (await _orders(maker))[0].status == "CANCELLED"
        assert "did not fill" in notes[0]

    @pytest.mark.asyncio
    async def test_broker_rejection_is_recorded(self, maker):
        order = await _add_order(maker)
        await _sync(maker, _report(order.order_ref, RefState.CANCELLED, rejection="Rejected by System: no permission"))
        assert (await _orders(maker))[0].status == "REJECTED"
        (event,) = await _events(maker, share_book.SHARE_ORDER_REJECTED)
        assert "no permission" in event.payload["reason"]

    @pytest.mark.asyncio
    async def test_filled_without_its_executions_is_held_never_guessed(self, maker):
        order = await _add_order(maker, quantity=3)
        notes = await _sync(maker, _report(order.order_ref, RefState.FILLED))
        assert await _holding(maker, "VTI") is None
        assert await _cash(maker) == 10_000.0
        assert (await _orders(maker))[0].status == "SUBMITTED"
        assert "NOT booked" in notes[0]
        # ... and reconciliation halts loudly on the unbooked shares.
        async with maker() as session:
            result = await run_reconciliation(session, BrokerSnapshot(positions=(_stk("VTI", 3.0),)))
        assert not result.clean
        assert result.drifts[0].kind == ORPHAN and result.drifts[0].unexpected_instrument

    @pytest.mark.asyncio
    async def test_executions_are_deduped_across_nights(self, maker):
        order = await _add_order(maker, quantity=3)
        ex = _exec(order.order_ref, 3, 300.0)
        await _sync(maker, _report(order.order_ref, RefState.OPEN), [ex])
        await _sync(maker, _report(order.order_ref, RefState.FILLED), [ex])
        assert await _holding(maker, "VTI") == 3.0
        assert len((await _orders(maker))[0].fills) == 1

    @pytest.mark.asyncio
    async def test_staged_found_resting_is_promoted(self, maker):
        order = await _add_order(maker, status="STAGED")
        await _sync(maker, _report(order.order_ref, RefState.OPEN))
        assert (await _orders(maker))[0].status == "SUBMITTED"

    @pytest.mark.asyncio
    async def test_unknown_staged_intent_expires(self, maker):
        order = await _add_order(maker, status="STAGED")
        await _sync(maker, _report(order.order_ref, RefState.UNKNOWN), gap=1)
        assert (await _orders(maker))[0].status == "CANCELLED"
        assert len(await _events(maker, share_book.SHARE_ORDER_EXPIRED)) == 1

    @pytest.mark.parametrize("gap", [None, 3])
    @pytest.mark.asyncio
    async def test_unknown_after_a_restore_gap_is_held(self, maker, gap):
        order = await _add_order(maker)
        notes = await _sync(maker, _report(order.order_ref, RefState.UNKNOWN), gap=gap)
        assert (await _orders(maker))[0].status == "SUBMITTED"
        assert "held" in notes[0]

    @pytest.mark.asyncio
    async def test_buy_without_its_funding_sell_flags_negative_cash(self, maker):
        async with maker() as session:
            (await session.get(BookModel, "B36")).cash_balance = 100.0
            await session.commit()
        order = await _add_order(maker, quantity=3)
        notes = await _sync(maker, _report(order.order_ref, RefState.FILLED), [_exec(order.order_ref, 3, 300.0)])
        assert "cash is now" in notes[0]


# ---------------------------------------------------------------------------
# Reconciliation seams
# ---------------------------------------------------------------------------


class TestReconciliationSeams:
    @pytest.mark.asyncio
    async def test_pending_share_order_resting_at_broker_is_not_a_ghost(self, maker):
        order = await _add_order(maker)
        snap = BrokerSnapshot(positions=(), open_orders=(OpenOrderInfo(order.order_ref, 1, 2, "Submitted"),))
        async with maker() as session:
            result = await run_reconciliation(session, snap)
        assert result.clean

    @pytest.mark.asyncio
    async def test_terminal_share_order_resting_at_broker_is_a_ghost(self, maker):
        order = await _add_order(maker, status="CANCELLED")
        snap = BrokerSnapshot(positions=(), open_orders=(OpenOrderInfo(order.order_ref, 1, 2, "Submitted"),))
        async with maker() as session:
            result = await run_reconciliation(session, snap)
        assert [d.kind for d in result.drifts] == [GHOST_ORDER]

    @pytest.mark.asyncio
    async def test_share_executions_are_ours_and_not_ledgered_twice(self, maker):
        order = await _add_order(maker)
        async with maker() as session:
            backfilled, unknown = await _backfill_missed_fills(session, (_exec(order.order_ref, 3, 300.0),))
            await session.commit()
            fills = (await session.execute(select(FillModel))).scalars().all()
        assert (backfilled, unknown, fills) == (0, [], [])
        assert await _cash(maker) == 10_000.0  # commission is the share sync's to debit

    @pytest.mark.asyncio
    async def test_unrelated_unknown_ref_still_surfaces(self, maker):
        async with maker() as session:
            _, unknown = await _backfill_missed_fills(session, (_exec("basis:B99:zz:share", 1, 1.0),))
        assert unknown == ["e1"]


def _share_drift(kind: str, broker: float, expected: float, *, mixed: bool = False, symbol: str = "VTI") -> DriftItem:
    return DriftItem(
        kind=kind,
        key=symbol,
        sec_type="STK",
        broker_qty=broker,
        expected_qty=expected,
        unexpected_instrument=True,
        mixed_sign=mixed,
    )


class TestShareSyncPendingCarveOut:
    def test_first_buy_filled_this_morning_is_explained(self):
        assert drift_is_sync_pending(_share_drift(ORPHAN, 10, 0), set(), {"VTI": 10})

    def test_partial_fill_inside_the_order_is_explained(self):
        assert drift_is_sync_pending(_share_drift(ORPHAN, 4, 0), set(), {"VTI": 10})

    def test_top_up_onto_an_existing_holding_is_explained(self):
        assert drift_is_sync_pending(_share_drift(SHARE_DRIFT, 25, 20), set(), {"VTI": 5})

    def test_sell_filled_this_morning_is_explained(self):
        assert drift_is_sync_pending(_share_drift(SHARE_DRIFT, 15, 20), set(), {"VTI": -5})

    def test_assignment_on_top_of_a_pending_buy_still_halts(self):
        # GLD is also an options underlying: 4 pending + 100 assigned.
        assert not drift_is_sync_pending(_share_drift(ORPHAN, 104, 0, symbol="GLD"), set(), {"GLD": 4})

    def test_move_against_the_pending_direction_halts(self):
        assert not drift_is_sync_pending(_share_drift(SHARE_DRIFT, 15, 20), set(), {"VTI": 5})

    def test_no_pending_order_halts(self):
        assert not drift_is_sync_pending(_share_drift(ORPHAN, 10, 0), set(), {"GLD": 10})

    def test_callers_without_share_deltas_keep_the_old_answer(self):
        assert not drift_is_sync_pending(_share_drift(ORPHAN, 10, 0), set())

    def test_mixed_sign_rows_never_explained(self):
        assert not drift_is_sync_pending(_share_drift(SHARE_DRIFT, 10, 0, mixed=True), set(), {"VTI": 10})

    def test_zero_move_is_not_explained(self):
        assert not drift_is_sync_pending(_share_drift(SHARE_DRIFT, 20, 20, mixed=True), set(), {"VTI": 5})

    @pytest.mark.asyncio
    async def test_pending_deltas_net_per_symbol(self, maker):
        await _add_order(maker, symbol="VTI", side="BUY", quantity=5, order_id="a")
        await _add_order(maker, symbol="VTI", side="SELL", quantity=2, order_id="b")
        await _add_order(maker, symbol="IEF", side="SELL", quantity=3, order_id="c")
        await _add_order(maker, symbol="GLD", side="BUY", quantity=9, order_id="d", status="FILLED")
        async with maker() as session:
            assert await share_book.pending_share_deltas(session) == {"VTI": 3.0, "IEF": -3.0}


# ---------------------------------------------------------------------------
# Book equity: holdings at that day's close
# ---------------------------------------------------------------------------


class TestShareMarks:
    @pytest.mark.asyncio
    async def test_mtm_includes_holdings_so_the_first_fill_is_no_pnl_shock(self, maker):
        async with maker() as session:
            book = await session.get(BookModel, "B36")
            book.cash_balance = 1000.0
            book.last_mtm = 10_000.0
            book.last_mtm_at = "2026-11-02T22:00:00+00:00"
            session.add(ShareHoldingModel(book_id="B36", symbol="SGOV", quantity=90.0, updated_at="t0"))
            session.add(IndexHistoryModel(date=NEXT_DAY.isoformat(), symbol="SGOV", close=100.0))
            await session.commit()
            finding = await check_pnl_shock(session, book, [], today=NEXT_DAY.isoformat())
            await session.commit()
            mark = await session.get(BookMtmHistoryModel, ("B36", NEXT_DAY.isoformat()))
        assert finding is None
        assert mark.mtm == 10_000.0

    @pytest.mark.asyncio
    async def test_unpriced_holding_takes_no_mark(self, maker):
        async with maker() as session:
            book = await session.get(BookModel, "B36")
            session.add(ShareHoldingModel(book_id="B36", symbol="SGOV", quantity=90.0, updated_at="t0"))
            await session.commit()
            finding = await check_pnl_shock(session, book, [], today=NEXT_DAY.isoformat())
            await session.commit()
            mark = await session.get(BookMtmHistoryModel, ("B36", NEXT_DAY.isoformat()))
        assert finding is None and mark is None and book.last_mtm is None
        assert len(await _events(maker, "MTM_SKIPPED_NO_SHARE_MARK")) == 1

    @pytest.mark.asyncio
    async def test_book_without_holdings_is_valued_at_zero(self, maker):
        async with maker() as session:
            assert await share_book.book_share_value(session, "B01", NEXT_DAY.isoformat()) == 0.0

    @pytest.mark.asyncio
    async def test_holdings_view_marks_at_latest_close(self, maker):
        async with maker() as session:
            session.add(ShareHoldingModel(book_id="B36", symbol="SGOV", quantity=90.0, updated_at="t0"))
            session.add(ShareHoldingModel(book_id="B36", symbol="VTI", quantity=3.0, updated_at="t0"))
            session.add(IndexHistoryModel(date="2026-10-30", symbol="SGOV", close=100.0))
            session.add(IndexHistoryModel(date="2026-11-02", symbol="SGOV", close=100.2))
            await session.commit()
            view = await share_book.share_holdings_view(session, "B36", "2026-11-02")
        by_symbol = {v.symbol: v for v in view}
        assert by_symbol["SGOV"].mark == 100.2 and by_symbol["SGOV"].value == pytest.approx(9018.0)
        assert by_symbol["VTI"].mark is None and by_symbol["VTI"].value is None


class TestBacktestReplay:
    def test_replay_from_seeds_skips_the_share_book(self):
        from backend.backtest.driver import replay_config_from_seeds

        config = replay_config_from_seeds(datetime.date(2020, 1, 1), datetime.date(2020, 2, 1))
        ids = {b.book_id for b in config.books}
        assert "B36" not in ids and "B01" in ids


class TestOptionsLedgerUntouched:
    @pytest.mark.asyncio
    async def test_share_orders_never_reach_the_options_orders_table(self, maker):
        await _seed_history(maker, {"VTI"})
        await _rebalance(maker, FakeShareBroker())
        async with maker() as session:
            assert (await session.execute(select(OrderModel))).scalars().all() == []
