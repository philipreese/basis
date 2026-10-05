"""#1074: FLATTEN_REQUESTED sells share holdings; a missed or unfilled
month-end is loud; the share book compounds.

Unit level against the share-book rig in test_share_book (fake broker, no
network). The evening-run integration lives in test_share_book_executor.
Fail-closed paths first: drift, a pending order, an undesignated row, no
close, less than a share, a flatten lifted mid-run."""

import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend import share_book
from backend.broker import ReconcileReport, RefState
from backend.digest import urgent_event_lines
from backend.etf_trend import buy_limit, last_signal_day_on_or_before, next_signal_day_after, sell_limit
from backend.models import (
    AuditEventModel,
    BookModel,
    BookMtmHistoryModel,
    IndexHistoryModel,
    ShareHoldingModel,
    ShareOrderModel,
    TradingControlModel,
)
from backend.states import SHARE_ORDER_PURPOSE_FLATTEN, SHARE_ORDER_PURPOSE_REBALANCE
from backend.tests.test_share_book import (
    MENU,
    NEXT_DAY,
    SIGNAL_DAY,
    FakeShareBroker,
    _add_order,
    _cash,
    _events,
    _exec,
    _holding,
    _orders,
    _rebalance,
    _report,
    _seed_history,
    _sync,
    share_rig,
)


@pytest_asyncio.fixture
async def maker():
    async with share_rig() as m:
        yield m


async def _hold(m, symbol: str, qty: float, book_id: str = "B36") -> None:
    async with m() as session:
        session.add(ShareHoldingModel(book_id=book_id, symbol=symbol, quantity=qty, updated_at="t0"))
        await session.commit()


async def _closes(m, day: datetime.date, **closes: float) -> None:
    async with m() as session:
        for symbol, close in closes.items():
            session.add(IndexHistoryModel(date=day.isoformat(), symbol=symbol, close=close))
        await session.commit()


async def _control(m, scope: str, state: str) -> None:
    async with m() as session:
        row = await session.get(TradingControlModel, scope)
        row.state = state
        await session.commit()


async def _flatten(m, broker, day=NEXT_DAY, drifted: frozenset[str] = frozenset()) -> share_book.RebalanceResult:
    async with m() as session:
        return await share_book.run_share_flatten(session, broker, day, drifted)


async def _watch(m, day: datetime.date) -> list[str]:
    async with m() as session:
        return await share_book.rebalance_watch_notes(session, day)


# ---------------------------------------------------------------------------
# Part 1 — the flatten
# ---------------------------------------------------------------------------


class TestFlattenSells:
    @pytest.mark.asyncio
    async def test_book_scoped_flatten_sells_every_holding(self, maker):
        await _hold(maker, "SCHB", 5.0)
        await _hold(maker, "TBIL", 80.0)
        await _closes(maker, NEXT_DAY, SCHB=300.0, TBIL=100.5)
        await _control(maker, "B36", "FLATTEN_REQUESTED")
        broker = FakeShareBroker()
        result = await _flatten(maker, broker)
        assert sorted((s, side, q, lim) for s, side, q, lim, _ in broker.placed) == [
            ("SCHB", "SELL", 5, sell_limit(300.0)),
            ("TBIL", "SELL", 80, sell_limit(100.5)),
        ]
        orders = await _orders(maker)
        assert {o.purpose for o in orders} == {SHARE_ORDER_PURPOSE_FLATTEN}
        assert {o.status for o in orders} == {"SUBMITTED"}
        assert all(o.order_ref.endswith(":share") and o.signal_date == NEXT_DAY.isoformat() for o in orders)
        assert result.placed == [ref for *_, ref in broker.placed]
        events = await _events(maker, share_book.SHARE_FLATTEN_SUBMITTED)
        assert {e.payload["scope"] for e in events} == {"B36"}
        assert {e.payload["trigger"] for e in events} == {"MANUAL"}
        # Placing never touches the control state: the flatten stays latched.
        async with maker() as session:
            assert (await session.get(TradingControlModel, "B36")).state == "FLATTEN_REQUESTED"

    @pytest.mark.asyncio
    async def test_global_flatten_includes_share_books(self, maker):
        await _hold(maker, "IAUM", 3.0)
        await _closes(maker, NEXT_DAY, IAUM=330.0)
        await _control(maker, "GLOBAL", "FLATTEN_REQUESTED")
        broker = FakeShareBroker()
        await _flatten(maker, broker)
        assert [(s, side, q) for s, side, q, _, _ in broker.placed] == [("IAUM", "SELL", 3)]
        (event,) = await _events(maker, share_book.SHARE_FLATTEN_SUBMITTED)
        assert event.payload["scope"] == "GLOBAL"

    @pytest.mark.asyncio
    async def test_no_flatten_anywhere_sells_nothing(self, maker):
        await _hold(maker, "SCHB", 5.0)
        await _closes(maker, NEXT_DAY, SCHB=300.0)
        await _control(maker, "B36", "HALT_ENTRIES")  # a halt is not a flatten
        broker = FakeShareBroker()
        result = await _flatten(maker, broker)
        assert broker.placed == [] and result.notes == [] and await _orders(maker) == []

    @pytest.mark.asyncio
    async def test_flatten_with_no_holdings_is_a_no_op(self, maker):
        await _hold(maker, "SCHB", 0.0)  # a fully sold holding leaves a zero row
        await _control(maker, "B36", "FLATTEN_REQUESTED")
        broker = FakeShareBroker()
        result = await _flatten(maker, broker)
        assert broker.placed == [] and result.notes == [] and await _orders(maker) == []

    @pytest.mark.asyncio
    async def test_flatten_of_another_book_leaves_the_share_book_alone(self, maker):
        await _hold(maker, "SCHB", 5.0)
        await _closes(maker, NEXT_DAY, SCHB=300.0)
        await _control(maker, "B01", "FLATTEN_REQUESTED")
        broker = FakeShareBroker()
        await _flatten(maker, broker)
        assert broker.placed == []

    @pytest.mark.asyncio
    async def test_a_short_holding_is_bought_back(self, maker):
        await _hold(maker, "SCHB", -2.0)
        await _closes(maker, NEXT_DAY, SCHB=300.0)
        await _control(maker, "B36", "FLATTEN_REQUESTED")
        broker = FakeShareBroker()
        await _flatten(maker, broker)
        assert [(s, side, q, lim) for s, side, q, lim, _ in broker.placed] == [("SCHB", "BUY", 2, buy_limit(300.0))]

    @pytest.mark.asyncio
    async def test_fractional_remainder_is_named_for_a_hand_sale(self, maker):
        await _hold(maker, "TBIL", 80.4)
        await _closes(maker, NEXT_DAY, TBIL=100.5)
        await _control(maker, "B36", "FLATTEN_REQUESTED")
        broker = FakeShareBroker()
        result = await _flatten(maker, broker)
        assert [(s, q) for s, _, q, _, _ in broker.placed] == [("TBIL", 80)]
        assert any("0.4 fractional share(s) left" in n for n in result.notes)


class TestFlattenFailsClosed:
    @pytest.mark.asyncio
    async def test_drifted_symbol_is_never_sold(self, maker):
        # The operator already sold at the broker: the books say 5, the broker 0.
        await _hold(maker, "SCHB", 5.0)
        await _hold(maker, "IAUM", 3.0)
        await _closes(maker, NEXT_DAY, SCHB=300.0, IAUM=330.0)
        await _control(maker, "B36", "FLATTEN_REQUESTED")
        broker = FakeShareBroker()
        result = await _flatten(maker, broker, drifted=frozenset({"SCHB"}))
        assert [s for s, *_ in broker.placed] == ["IAUM"]
        assert any("FLATTEN B36 SCHB: NOT sold" in n and "share drift" in n for n in result.notes)
        (skip,) = await _events(maker, share_book.SHARE_FLATTEN_SKIPPED)
        assert skip.payload["symbol"] == "SCHB"
        # A needed close that did not happen interrupts a human (the urgent push).
        async with maker() as session:
            urgent = await urgent_event_lines(session, since="")
        assert any(line.text.startswith("SHARE_FLATTEN_SKIPPED") and "share drift" in line.text for line in urgent)

    @pytest.mark.asyncio
    async def test_pending_order_on_the_symbol_blocks_a_second_sell(self, maker):
        await _hold(maker, "SCHB", 5.0)
        await _closes(maker, NEXT_DAY, SCHB=300.0)
        await _add_order(maker, symbol="SCHB", side="SELL", quantity=5)
        await _control(maker, "B36", "FLATTEN_REQUESTED")
        broker = FakeShareBroker()
        result = await _flatten(maker, broker)
        assert broker.placed == []
        assert any("already pending" in n for n in result.notes)

    @pytest.mark.asyncio
    async def test_undesignated_symbol_row_is_not_sold(self, maker):
        await _hold(maker, "SPY", 10.0)  # B36 is not designated for SPY
        await _closes(maker, NEXT_DAY, SPY=700.0)
        await _control(maker, "B36", "FLATTEN_REQUESTED")
        broker = FakeShareBroker()
        result = await _flatten(maker, broker)
        assert broker.placed == []
        assert any("not designated" in n for n in result.notes)

    @pytest.mark.asyncio
    async def test_no_close_today_skips_the_symbol(self, maker):
        await _hold(maker, "SCHB", 5.0)
        await _control(maker, "B36", "FLATTEN_REQUESTED")
        broker = FakeShareBroker()
        result = await _flatten(maker, broker)
        assert broker.placed == []
        assert any("no close today" in n for n in result.notes)

    @pytest.mark.asyncio
    async def test_under_one_share_is_left_for_a_hand_sale(self, maker):
        await _hold(maker, "TBIL", 0.4)
        await _closes(maker, NEXT_DAY, TBIL=100.5)
        await _control(maker, "B36", "FLATTEN_REQUESTED")
        broker = FakeShareBroker()
        result = await _flatten(maker, broker)
        assert broker.placed == []
        assert any("under one whole share" in n for n in result.notes)

    @pytest.mark.asyncio
    async def test_flatten_lifted_mid_run_places_nothing(self, maker, monkeypatch):
        await _hold(maker, "SCHB", 5.0)
        await _closes(maker, NEXT_DAY, SCHB=300.0)
        await _control(maker, "B36", "FLATTEN_REQUESTED")

        async def _resumed(session, book_id):
            return False

        monkeypatch.setattr(share_book, "_still_flattening", _resumed)
        broker = FakeShareBroker()
        result = await _flatten(maker, broker)
        assert broker.placed == []
        (order,) = await _orders(maker)
        assert order.status == "CANCELLED"
        assert any("lifted mid-run" in n for n in result.notes)

    @pytest.mark.asyncio
    async def test_broker_refusal_rejects_that_order_and_moves_on(self, maker):
        await _hold(maker, "SCHB", 5.0)
        await _hold(maker, "IAUM", 3.0)
        await _closes(maker, NEXT_DAY, SCHB=300.0, IAUM=330.0)
        await _control(maker, "B36", "FLATTEN_REQUESTED")
        broker = FakeShareBroker(fail_on="IAUM")
        result = await _flatten(maker, broker)
        assert [s for s, *_ in broker.placed] == ["SCHB"]
        statuses = {o.symbol: o.status for o in await _orders(maker)}
        assert statuses == {"IAUM": "REJECTED", "SCHB": "SUBMITTED"}
        assert any("refused by the broker" in n for n in result.notes)
        assert len(await _events(maker, share_book.SHARE_FLATTEN_REJECTED)) == 1

    @pytest.mark.asyncio
    async def test_still_flattening_reads_book_and_global_scopes(self, maker):
        async with maker() as session:
            assert not await share_book._still_flattening(session, "B36")
        await _control(maker, "B36", "FLATTEN_REQUESTED")
        async with maker() as session:
            assert await share_book._still_flattening(session, "B36")
        await _control(maker, "B36", "ACTIVE")
        await _control(maker, "GLOBAL", "FLATTEN_REQUESTED")
        async with maker() as session:
            assert await share_book._still_flattening(session, "B36")


class TestFlattenNonFills:
    @pytest.mark.asyncio
    async def test_an_expired_flatten_sell_says_it_retries(self, maker):
        order = await _add_order(maker, symbol="SCHB", side="SELL", quantity=5)
        async with maker() as session:
            row = await session.get(ShareOrderModel, order.id)
            row.purpose = SHARE_ORDER_PURPOSE_FLATTEN
            await session.commit()
        notes = await _sync(maker, _report(order.order_ref, RefState.CANCELLED))
        assert any("the flatten retries next run" in n for n in notes)

    @pytest.mark.asyncio
    async def test_partial_flatten_fill_books_what_filled_and_the_rest_sells_next_run(self, maker):
        await _hold(maker, "SCHB", 5.0)
        await _closes(maker, NEXT_DAY, SCHB=300.0)
        await _control(maker, "B36", "FLATTEN_REQUESTED")
        broker = FakeShareBroker()
        await _flatten(maker, broker)
        ref = broker.placed[0][4]
        # The DAY order sold 2 of 5 and expired.
        await _sync(maker, _report(ref, RefState.CANCELLED), [_exec(ref, -2, 294.0)])
        assert await _holding(maker, "SCHB") == pytest.approx(3.0)
        assert await _cash(maker) == pytest.approx(10_000.0 + 2 * 294.0 - 1.0)
        # Next night: the remainder goes out; the flatten is still latched.
        later = NEXT_DAY + datetime.timedelta(days=1)
        await _closes(maker, later, SCHB=301.0)
        broker2 = FakeShareBroker()
        await _flatten(maker, broker2, day=later)
        assert [(s, side, q) for s, side, q, _, _ in broker2.placed] == [("SCHB", "SELL", 3)]
        async with maker() as session:
            assert (await session.get(TradingControlModel, "B36")).state == "FLATTEN_REQUESTED"

    @pytest.mark.asyncio
    async def test_month_end_rebalance_does_not_run_under_flatten(self, maker):
        await _seed_history(maker, set(MENU))
        await _control(maker, "B36", "FLATTEN_REQUESTED")
        broker = FakeShareBroker()
        result = await _rebalance(maker, broker)
        assert broker.placed == []
        assert await _events(maker, share_book.ETF_TREND_SIGNAL) == []
        (skip,) = await _events(maker, share_book.ETF_TREND_SKIPPED)
        assert "B36=FLATTEN_REQUESTED" in skip.payload["reason"]
        assert skip.payload["signal_date"] == SIGNAL_DAY.isoformat()
        assert any("SKIPPED" in n for n in result.notes)

    @pytest.mark.asyncio
    async def test_a_halt_is_the_skip_reason_even_with_orders_pending(self, maker):
        # Under a flatten, tonight's own flatten sells are pending: the skip
        # the missed-rebalance line repeats must name the halt.
        await _seed_history(maker, set(MENU))
        await _add_order(maker, symbol="SCHB", side="SELL", quantity=5)
        await _control(maker, "GLOBAL", "FLATTEN_REQUESTED")
        await _rebalance(maker, FakeShareBroker())
        (skip,) = await _events(maker, share_book.ETF_TREND_SKIPPED)
        assert "GLOBAL=FLATTEN_REQUESTED" in skip.payload["reason"]


# ---------------------------------------------------------------------------
# A missed or unfilled month-end is loud
# ---------------------------------------------------------------------------


class TestSignalDayHelpers:
    def test_last_and_next_signal_days(self):
        assert last_signal_day_on_or_before(datetime.date(2026, 10, 30)) == datetime.date(2026, 10, 30)
        assert last_signal_day_on_or_before(datetime.date(2026, 11, 2)) == datetime.date(2026, 10, 30)
        assert last_signal_day_on_or_before(datetime.date(2027, 1, 4)) == datetime.date(2026, 12, 31)
        assert next_signal_day_after(datetime.date(2026, 10, 30)) == datetime.date(2026, 11, 30)
        assert next_signal_day_after(datetime.date(2026, 10, 5)) == datetime.date(2026, 10, 30)
        assert next_signal_day_after(datetime.date(2026, 12, 31)) == datetime.date(2027, 1, 29)


class TestRebalanceWatch:
    @pytest.mark.asyncio
    async def test_missed_with_no_record_says_it_may_have_been_missed(self, maker):
        notes = await _watch(maker, NEXT_DAY)
        assert notes == [
            (
                "⚠ B36 missed its month-end rebalance (2026-10-30): no rebalance record for that day (no run, or the "
                "run stopped before the rebalance) — it may have been missed; holding last month's positions until "
                "2026-11-30"
            )
        ]

    @pytest.mark.asyncio
    async def test_repeats_every_night_until_the_next_month_end(self, maker):
        for day in (NEXT_DAY, datetime.date(2026, 11, 17), datetime.date(2026, 11, 27)):
            assert len(await _watch(maker, day)) == 1

    @pytest.mark.asyncio
    async def test_halted_by_the_operator_names_the_halt(self, maker):
        await _seed_history(maker, set(MENU))
        await _control(maker, "B36", "HALT_ENTRIES")
        await _rebalance(maker, FakeShareBroker())
        (note,) = await _watch(maker, SIGNAL_DAY)
        assert "missed its month-end rebalance (2026-10-30): skipped — entries halted (B36=HALT_ENTRIES)" in note
        assert "until 2026-11-30" in note

    @pytest.mark.asyncio
    async def test_a_rebalance_that_ran_and_filled_says_nothing(self, maker):
        await _seed_history(maker, {"SCHB"})
        broker = FakeShareBroker()
        await _rebalance(maker, broker)
        assert await _watch(maker, SIGNAL_DAY) == []  # orders still working: nothing to say yet
        report = ReconcileReport(states={ref: RefState.FILLED for *_, ref in broker.placed})
        await _sync(maker, report, [_exec(ref, q, 100.0, exec_id=ref) for _, _, q, _, ref in broker.placed])
        assert await _watch(maker, NEXT_DAY) == []

    @pytest.mark.asyncio
    async def test_an_unfilled_or_partial_month_end_order_names_its_slot(self, maker):
        await _seed_history(maker, {"SCHB", "IAUM"})
        broker = FakeShareBroker()
        await _rebalance(maker, broker)
        by_symbol = {s: (q, ref) for s, _, q, _, ref in broker.placed}
        vti_q, vti_ref = by_symbol["SCHB"]
        gld_q, gld_ref = by_symbol["IAUM"]
        sgov_q, sgov_ref = by_symbol["TBIL"]
        # SCHB: the open gapped past the limit. IAUM: 2 filled, then expired. TBIL: filled.
        report = ReconcileReport(
            states={vti_ref: RefState.CANCELLED, gld_ref: RefState.CANCELLED, sgov_ref: RefState.FILLED}
        )
        await _sync(
            maker, report, [_exec(gld_ref, 2, 330.0, exec_id="g1"), _exec(sgov_ref, sgov_q, 100.5, exec_id="s1")]
        )
        notes = await _watch(maker, NEXT_DAY)
        assert len(notes) == 2
        assert notes[0].startswith(f"⚠ B36 BUY {gld_q} IAUM from the 2026-10-30 rebalance filled only 2 of {gld_q}")
        assert notes[1].startswith(f"⚠ B36 BUY {vti_q} SCHB from the 2026-10-30 rebalance did not fill")
        assert all("slot holds last month's position until 2026-11-30" in n for n in notes)

    @pytest.mark.asyncio
    async def test_an_intent_the_rebalance_never_placed_is_named(self, maker):
        await _seed_history(maker, {"SCHB", "IAUM"})
        broker = FakeShareBroker(fail_on="IAUM")  # the broker refuses IAUM: the rest is never staged
        await _rebalance(maker, broker)
        notes = await _watch(maker, SIGNAL_DAY)
        assert any("IAUM from the 2026-10-30 rebalance did not fill (REJECTED)" in n for n in notes)
        assert any("TBIL from the 2026-10-30 rebalance was never placed" in n for n in notes)

    @pytest.mark.asyncio
    async def test_a_flatten_non_fill_is_not_a_missed_rotation(self, maker):
        await _seed_history(maker, {"SCHB"})
        await _rebalance(maker, FakeShareBroker())
        async with maker() as session:
            for order in (await session.execute(select(ShareOrderModel))).scalars().all():
                order.status = "FILLED"
                order.filled_quantity = float(order.quantity)
            session.add(
                ShareOrderModel(
                    id="fl1",
                    book_id="B36",
                    order_ref="basis:B36:fl1:share",
                    symbol="SCHB",
                    side="SELL",
                    quantity=5,
                    limit_price=294.0,
                    decision_close=300.0,
                    signal_date=SIGNAL_DAY.isoformat(),
                    status="CANCELLED",
                    config_hash="hash-B36",
                    created_at="t0",
                    fills=[],
                    purpose=SHARE_ORDER_PURPOSE_FLATTEN,
                )
            )
            await session.commit()
        assert await _watch(maker, NEXT_DAY) == []

    @pytest.mark.asyncio
    async def test_signal_days_before_the_book_existed_are_never_reported(self, maker):
        async with maker() as session:
            (await session.get(BookModel, "B36")).created_at = "2026-10-31T00:00:00+00:00"
            await session.commit()
        assert await _watch(maker, NEXT_DAY) == []

    @pytest.mark.asyncio
    async def test_retired_or_options_books_are_never_watched(self, maker):
        async with maker() as session:
            (await session.get(BookModel, "B36")).status = "RETIRED"
            await session.commit()
        assert await _watch(maker, NEXT_DAY) == []


# ---------------------------------------------------------------------------
# The book compounds (operator ruling 2026-10-03)
# ---------------------------------------------------------------------------


class TestCompounding:
    @pytest.mark.asyncio
    async def test_equity_above_the_basis_is_invested_in_full(self, maker):
        await _seed_history(maker, {"SCHB"})
        async with maker() as session:
            (await session.get(BookModel, "B36")).cash_balance = 15_000.0
            await session.commit()
        broker = FakeShareBroker()
        await _rebalance(maker, broker)
        placed = {s: q for s, _, q, _, _ in broker.placed}
        # slot = 15000/7 = 2142.86 -> 7 SCHB at 300 (the old basis cap gave 4).
        assert placed["SCHB"] == 7
        (signal,) = await _events(maker, share_book.ETF_TREND_SIGNAL)
        assert signal.payload["investable"] == pytest.approx(15_000.0)

    @pytest.mark.asyncio
    async def test_equity_below_the_basis_invests_only_the_equity(self, maker):
        await _seed_history(maker, {"SCHB"})
        async with maker() as session:
            (await session.get(BookModel, "B36")).cash_balance = 6_000.0
            await session.commit()
        broker = FakeShareBroker()
        await _rebalance(maker, broker)
        placed = {s: q for s, _, q, _, _ in broker.placed}
        assert placed["SCHB"] == 2  # slot = 6000/7 = 857
        (signal,) = await _events(maker, share_book.ETF_TREND_SIGNAL)
        assert signal.payload["investable"] == pytest.approx(6_000.0)


async def _stake(m, stake: float, cash: float, *, synced_at: str | None = None, marks=()) -> None:
    """Give B36 a stage-1 stake. *synced_at* writes a BOOK_CONFIG_SYNCED (the
    era, so the stake window, opens then and starting_capital is no longer a
    fallback baseline); *marks* are (date, mtm) book_mtm_history rows."""
    async with m() as session:
        book = await session.get(BookModel, "B36")
        book.config = {**book.config, "stage1_stake": stake}
        book.cash_balance = cash
        if synced_at:
            session.add(
                AuditEventModel(run_at=synced_at, book_id="B36", event_type="BOOK_CONFIG_SYNCED", actor="t", payload={})
            )
        for d, mtm in marks:
            session.add(BookMtmHistoryModel(book_id="B36", date=d, mtm=mtm))
        await session.commit()


async def _investable(m) -> float:
    (signal,) = await _events(m, share_book.ETF_TREND_SIGNAL)
    return signal.payload["investable"]


class TestStakedSizing:
    """A staked share book sizes from stake + P&L since its stake window
    opened, by the drawdown halt's own window and baseline (stage1)."""

    @pytest.mark.asyncio
    async def test_a_gain_compounds_past_the_stake(self, maker):
        await _seed_history(maker, {"SCHB"})
        # Window is the book's whole life: baseline = starting_capital 10,000.
        await _stake(maker, 5_000.0, cash=12_000.0)
        broker = FakeShareBroker()
        await _rebalance(maker, broker)
        assert await _investable(maker) == pytest.approx(7_000.0)  # stake + 2,000 gain
        assert {s: q for s, _, q, _, _ in broker.placed}["SCHB"] == 3  # 7000/7 = 1000 -> 3 at 300

    @pytest.mark.asyncio
    async def test_a_loss_shrinks_the_stake(self, maker):
        await _seed_history(maker, {"SCHB"})
        await _stake(maker, 5_000.0, cash=9_000.0)
        broker = FakeShareBroker()
        await _rebalance(maker, broker)
        assert await _investable(maker) == pytest.approx(4_000.0)  # stake - 1,000 loss
        assert {s: q for s, _, q, _, _ in broker.placed}["SCHB"] == 1  # 4000/7 = 571 -> 1 at 300

    @pytest.mark.asyncio
    async def test_the_baseline_is_the_last_mark_before_the_window(self, maker):
        # Same definition as the drawdown halt: the era synced 2026-10-10, so
        # the baseline is the 10-09 mark, not starting_capital.
        await _seed_history(maker, {"SCHB"})
        await _stake(
            maker,
            5_000.0,
            cash=12_000.0,
            synced_at="2026-10-10T14:00:00+00:00",
            marks=[("2026-10-08", 10_500.0), ("2026-10-09", 11_000.0), ("2026-10-20", 11_500.0)],
        )
        await _rebalance(maker, FakeShareBroker())
        assert await _investable(maker) == pytest.approx(6_000.0)  # 5000 + (12000 - 11000)

    @pytest.mark.asyncio
    async def test_never_more_than_current_equity(self, maker):
        await _seed_history(maker, {"SCHB"})
        await _stake(maker, 20_000.0, cash=12_000.0)
        await _rebalance(maker, FakeShareBroker())
        assert await _investable(maker) == pytest.approx(12_000.0)

    @pytest.mark.asyncio
    async def test_no_baseline_means_no_orders_and_an_urgent_reason(self, maker):
        # Era synced, no mark before it, and starting_capital is no fallback.
        await _seed_history(maker, {"SCHB"})
        await _stake(maker, 5_000.0, cash=12_000.0, synced_at="2026-10-10T14:00:00+00:00")
        broker = FakeShareBroker()
        result = await _rebalance(maker, broker)
        assert broker.placed == [] and await _orders(maker) == []
        (unsized,) = await _events(maker, share_book.ETF_TREND_STAKE_UNSIZED)
        assert (
            "baseline equity at the stake window start (2026-10-10) cannot be determined" in unsized.payload["reason"]
        )
        (skip,) = await _events(maker, share_book.ETF_TREND_SKIPPED)
        assert skip.payload["signal_date"] == SIGNAL_DAY.isoformat()
        assert any("SKIPPED" in n and "cannot be determined" in n for n in result.notes)
        async with maker() as session:
            urgent = await urgent_event_lines(session, since="")
        assert any(line.text.startswith("ETF_TREND_STAKE_UNSIZED") for line in urgent)

    @pytest.mark.asyncio
    async def test_an_exhausted_stake_places_nothing(self, maker):
        await _seed_history(maker, {"SCHB"})
        await _stake(maker, 1_000.0, cash=8_500.0)  # stake 1000, P&L -1500
        broker = FakeShareBroker()
        await _rebalance(maker, broker)
        assert broker.placed == []
        (unsized,) = await _events(maker, share_book.ETF_TREND_STAKE_UNSIZED)
        assert "stake exhausted" in unsized.payload["reason"]


@pytest.mark.asyncio
async def test_rebalance_orders_are_marked_as_rebalance(maker):
    await _seed_history(maker, {"SCHB"})
    await _rebalance(maker, FakeShareBroker())
    assert {o.purpose for o in await _orders(maker)} == {SHARE_ORDER_PURPOSE_REBALANCE}
