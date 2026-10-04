"""#1093: the operator-triggered paper dress rehearsal of the share path.

Against a database seeded by the real init_db (so R01 arrives exactly as
production seeds it) and a fake broker: no network, no Gateway. Refusals
first: paper mode, the windows, the lock, another tenant. Then each phase,
a whole place → status → unwind → status cycle through B36's own code,
and the evidence isolation: R01's fills never reach B36, book_summaries,
the null drill or distribution attribution, and reconciliation reads clean
with R01's holdings present."""

import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import share_rehearsal
from backend.broker import FillInfo, LegPosition, PlacedOrder, ReconcileReport, RefState
from backend.dates import MARKET_TZ
from backend.etf_trend import buy_limit, sell_limit
from backend.models import (
    AuditEventModel,
    BookModel,
    IndexHistoryModel,
    ShareHoldingModel,
    ShareOrderModel,
    TradingControlModel,
)
from backend.seeds import LAB_BOOKS, OPS_BOOKS
from backend.states import BOOK_OPS_STATUS, SHARE_ORDER_PURPOSE_FLATTEN, SHARE_ORDER_PURPOSE_REBALANCE

# Tuesday 2026-10-06, 10:00 ET: a trading day outside every window; the
# nightly stored Monday's closes.
NOW = datetime.datetime(2026, 10, 6, 10, 0, tzinfo=MARKET_TZ)
CLOSE_DAY = "2026-10-05"
CLOSES = {"IAUM": 41.33, "SCHF": 27.58, "SCHH": 21.95, "SCHB": 29.61, "UTEN": 40.51, "DBMF": 32.91, "TBIL": 49.88}


class FakeBroker:
    """The BrokerSession surface the rehearsal touches. Verdicts, executions
    and positions are set by the test between phases, as the paper account
    would move them."""

    def __init__(self) -> None:
        self.placed: list[tuple[str, str, int, float, str]] = []
        self.states: dict[str, RefState] = {}
        self.execs: list[FillInfo] = []
        self.stk: dict[str, float] = {}
        self.reconciled_with: list[list[str]] = []
        self.closed = False

    def reconcile(self, refs: list[str]) -> ReconcileReport:
        self.reconciled_with.append(list(refs))
        return ReconcileReport(states={r: self.states.get(r, RefState.UNKNOWN) for r in refs})

    def executions(self) -> list[FillInfo]:
        return list(self.execs)

    def positions(self) -> list[LegPosition]:
        return [
            LegPosition(con_id=i, symbol=s, sec_type="STK", position=q, avg_cost=1.0)
            for i, (s, q) in enumerate(sorted(self.stk.items()))
            if q
        ]

    def open_orders(self) -> list:
        return []

    def place_share_order(self, symbol, side, quantity, limit_price, ref) -> PlacedOrder:
        self.placed.append((symbol, side, quantity, limit_price, ref))
        self.states[ref] = RefState.OPEN
        return PlacedOrder(order_id=len(self.placed), perm_id=500 + len(self.placed), ref=ref, status="Submitted")

    def close(self) -> None:
        self.closed = True

    def fill_everything(self) -> None:
        """Every placed order fills in full at its decision close, and the
        broker's share count moves with it."""
        for n, (symbol, side, quantity, _limit, ref) in enumerate(self.placed):
            if self.states.get(ref) is RefState.FILLED:
                continue
            self.states[ref] = RefState.FILLED
            self.execs.append(
                FillInfo(
                    exec_id=f"x{n}",
                    con_id=n,
                    side="BOT" if side == "BUY" else "SLD",
                    quantity=float(quantity),
                    price=CLOSES[symbol],
                    order_ref=ref,
                    commission=1.0,
                    exec_time="2026-10-06T14:00:00+00:00",
                )
            )
            self.stk[symbol] = self.stk.get(symbol, 0.0) + (quantity if side == "BUY" else -quantity)


@pytest.fixture
def maker(tmp_path, monkeypatch):
    import backend.database as db_mod

    url = f"sqlite+aiosqlite:///{(tmp_path / 'rehearsal.db').as_posix()}"
    monkeypatch.setattr(db_mod, "DATABASE_URL", url)
    engine = create_async_engine(url)
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(db_mod, "async_session_maker", m)
    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(share_rehearsal, "TRADING_MODE", "paper")
    monkeypatch.setattr(share_rehearsal, "other_gateway_tenant_active", lambda caller: False)
    return db_mod, m


async def _seed(maker_pair, closes_on: str = CLOSE_DAY) -> async_sessionmaker:
    db_mod, m = maker_pair
    await db_mod.init_db()
    async with m() as session:
        for symbol, close in CLOSES.items():
            session.add(IndexHistoryModel(date=closes_on, symbol=symbol, close=close))
        await session.commit()
    return m


async def _run(m, broker: FakeBroker | None, phase: str, now: datetime.datetime = NOW, **kw):
    def _open():
        if broker is None:
            return None, None, None, "IB Gateway API port never opened"
        return broker, None, None, None

    return await share_rehearsal.run_rehearsal(phase, session_maker=m, open_session=_open, now_et=lambda: now, **kw)


async def _rows(m, model, **filters) -> list:
    async with m() as session:
        return list((await session.execute(select(model).filter_by(**filters))).scalars().all())


async def _holdings(m, book_id: str) -> dict[str, float]:
    return {h.symbol: h.quantity for h in await _rows(m, ShareHoldingModel, book_id=book_id) if h.quantity}


async def _cash(m, book_id: str) -> float:
    async with m() as session:
        return (await session.get(BookModel, book_id)).cash_balance


async def _control(m, scope: str) -> str:
    async with m() as session:
        return (await session.get(TradingControlModel, scope)).state


async def _set_control(m, scope: str, state: str) -> None:
    async with m() as session:
        row = await session.get(TradingControlModel, scope)
        row.state = state
        await session.commit()


# ---------------------------------------------------------------------------
# Seeding: R01 is an ops book, designated for exactly B36's symbols
# ---------------------------------------------------------------------------


class TestSeeding:
    @pytest.mark.asyncio
    async def test_init_db_seeds_r01_as_an_ops_book_with_b36s_symbols(self, maker):
        m = await _seed(maker)
        async with m() as session:
            r01 = await session.get(BookModel, "R01")
            control = await session.get(TradingControlModel, "R01")
        b36_symbols = next(b for b in LAB_BOOKS if b["id"] == "B36")["config"]["share_symbols"]
        assert r01.status == BOOK_OPS_STATUS
        assert r01.config == {"share_symbols": b36_symbols}
        assert "etf_trend" not in r01.config  # never rebalances
        assert control.state == "ACTIVE"
        assert all(spec["id"] not in {b["id"] for b in LAB_BOOKS} for spec in OPS_BOOKS)

    @pytest.mark.asyncio
    async def test_restart_does_not_resync_r01(self, maker):
        m = await _seed(maker)
        db_mod, _ = maker
        await db_mod.init_db()
        assert await _rows(m, AuditEventModel, event_type="BOOK_CONFIG_SYNCED", book_id="R01") == []


# ---------------------------------------------------------------------------
# Refusals: nothing is placed, no Gateway is launched
# ---------------------------------------------------------------------------


class TestRefusals:
    @pytest.mark.asyncio
    async def test_live_mode_raises_before_anything(self, maker, monkeypatch):
        m = await _seed(maker)
        monkeypatch.setattr(share_rehearsal, "TRADING_MODE", "live")
        broker = FakeBroker()
        with pytest.raises(RuntimeError, match="PAPER"):
            await _run(m, broker, "place")
        assert broker.placed == [] and broker.reconciled_with == []

    @pytest.mark.asyncio
    async def test_a_non_paper_account_opens_no_session_and_places_nothing(self, maker):
        # BrokerSession.open refuses any non-D-prefixed account
        # (PaperAccountRequiredError); the launcher surfaces it as a failure.
        m = await _seed(maker)

        def _open():
            return None, None, None, "broker session failed to open: Managed accounts ['U123'] are not all paper"

        report = await share_rehearsal.run_rehearsal("place", session_maker=m, open_session=_open, now_et=lambda: NOW)
        assert report.exit_code == share_rehearsal.EXIT_BROKER_UNAVAILABLE
        assert await _rows(m, ShareOrderModel) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("hh", "mm"),
        [(12, 15), (12, 30), (12, 45), (13, 45), (14, 0), (14, 30), (18, 30), (18, 45), (19, 30)],
    )
    async def test_window_refusals(self, maker, hh, mm):
        m = await _seed(maker)
        broker = FakeBroker()
        report = await _run(m, broker, "status", now=NOW.replace(hour=hh, minute=mm))
        assert report.exit_code == share_rehearsal.EXIT_REFUSED
        assert "window" in report.refused
        assert broker.reconciled_with == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("hh", "mm"), [(12, 14), (12, 46), (13, 44), (14, 31), (18, 29), (19, 31), (21, 0)])
    async def test_outside_the_windows_runs(self, maker, hh, mm):
        m = await _seed(maker)
        report = await _run(m, FakeBroker(), "status", now=NOW.replace(hour=hh, minute=mm))
        assert report.refused is None

    @pytest.mark.asyncio
    async def test_a_held_executor_lock_refuses(self, maker):
        from backend.run_lock import acquire_run_lock, release_run_lock

        m = await _seed(maker)
        held = acquire_run_lock("executor")
        try:
            broker = FakeBroker()
            report = await _run(m, broker, "place")
        finally:
            release_run_lock(held)
        assert report.exit_code == share_rehearsal.EXIT_REFUSED and "lock" in report.refused
        assert broker.reconciled_with == []

    @pytest.mark.asyncio
    async def test_the_rehearsal_holds_the_executor_lock_while_it_runs(self, maker):
        from backend.run_lock import lock_is_held

        m = await _seed(maker)
        seen: list[bool] = []

        def _open():
            seen.append(lock_is_held("executor"))
            return FakeBroker(), None, None, None

        await share_rehearsal.run_rehearsal("status", session_maker=m, open_session=_open, now_et=lambda: NOW)
        assert seen == [True]
        assert not lock_is_held("executor")  # released afterwards

    @pytest.mark.asyncio
    async def test_another_gateway_tenant_refuses(self, maker, monkeypatch):
        m = await _seed(maker)
        monkeypatch.setattr(share_rehearsal, "other_gateway_tenant_active", lambda caller: True)
        broker = FakeBroker()
        report = await _run(m, broker, "place")
        assert report.exit_code == share_rehearsal.EXIT_REFUSED and "tenant" in report.refused
        assert broker.reconciled_with == []

    @pytest.mark.asyncio
    async def test_unknown_phase_and_bad_symbol_lists_refuse(self, maker):
        m = await _seed(maker)
        assert (await _run(m, FakeBroker(), "rebalance")).exit_code == share_rehearsal.EXIT_REFUSED
        for symbols in ((), ("SCHH", "SCHH"), ("IAUM", "SCHF", "SCHH", "SCHB")):
            assert (await _run(m, FakeBroker(), "place", symbols=symbols)).exit_code == share_rehearsal.EXIT_REFUSED

    @pytest.mark.asyncio
    async def test_an_undesignated_symbol_refuses(self, maker):
        m = await _seed(maker)
        broker = FakeBroker()
        report = await _run(m, broker, "place", symbols=("SPY",))
        assert "not designated" in report.refused and broker.placed == []

    @pytest.mark.asyncio
    async def test_missing_r01_refuses(self, maker):
        m = await _seed(maker)
        async with m() as session:
            (await session.get(BookModel, "R01")).status = "ACTIVE"
            await session.commit()
        report = await _run(m, FakeBroker(), "status")
        assert "not an ops book" in report.refused

    @pytest.mark.asyncio
    async def test_no_stored_close_refuses_and_never_fetches(self, maker):
        db_mod, m = maker
        await db_mod.init_db()
        broker = FakeBroker()
        report = await _run(m, broker, "place")
        assert "no stored close" in report.refused
        assert broker.reconciled_with == []
        assert await _rows(m, IndexHistoryModel) == []  # nothing persisted

    @pytest.mark.asyncio
    async def test_a_stale_close_refuses(self, maker):
        m = await _seed(maker, closes_on="2026-10-01")  # Thursday; Monday's close is missing
        report = await _run(m, FakeBroker(), "place")
        assert "older than the previous trading day" in report.refused

    @pytest.mark.asyncio
    async def test_place_refuses_while_r01_still_holds_or_has_pending(self, maker):
        m = await _seed(maker)
        async with m() as session:
            session.add(ShareHoldingModel(book_id="R01", symbol="SCHH", quantity=1.0, updated_at="t0"))
            await session.commit()
        report = await _run(m, FakeBroker(), "place")
        assert "still holds" in report.refused

    @pytest.mark.asyncio
    async def test_place_refuses_on_unexplained_drift(self, maker):
        m = await _seed(maker)
        broker = FakeBroker()
        broker.stk = {"SPY": 100.0}  # an assignment nobody owns
        report = await _run(m, broker, "place")
        assert "unexplained drift" in report.refused and broker.placed == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scope", ["R01", "GLOBAL"])
    async def test_place_refuses_before_gateway_when_r01_or_global_is_not_active(self, maker, scope):
        m = await _seed(maker)
        await _set_control(m, scope, "HALT_ENTRIES")
        broker = FakeBroker()
        report = await _run(m, broker, "place")
        assert f"{scope} is HALT_ENTRIES" in report.refused
        assert broker.reconciled_with == [] and await _rows(m, ShareOrderModel) == []

    @pytest.mark.asyncio
    async def test_a_halt_landing_after_the_precheck_is_caught_by_the_real_choke_point(self, maker, monkeypatch):
        m = await _seed(maker)
        await _set_control(m, "R01", "HALT_ENTRIES")

        async def _stale_precheck(session, book_id):
            return ("GLOBAL", "ACTIVE")  # what the early look saw, a moment before the halt

        monkeypatch.setattr(share_rehearsal, "check_trading_control", _stale_precheck)
        broker = FakeBroker()
        report = await _run(m, broker, "place")
        assert broker.placed == []
        assert any("halted mid-rebalance" in line for line in report.lines)
        orders = await _rows(m, ShareOrderModel, book_id="R01")
        assert [o.status for o in orders] == ["CANCELLED"]  # staged first, then refused at the choke point

    @pytest.mark.asyncio
    async def test_unwind_refuses_when_another_scope_is_flattening(self, maker):
        m = await _seed(maker)
        await _set_control(m, "GLOBAL", "FLATTEN_REQUESTED")
        broker = FakeBroker()
        report = await _run(m, broker, "unwind")
        assert "GLOBAL" in report.refused and broker.reconciled_with == []

    @pytest.mark.asyncio
    async def test_unwind_with_nothing_held_refuses_and_leaves_control_alone(self, maker):
        m = await _seed(maker)
        report = await _run(m, FakeBroker(), "unwind")
        assert "holds nothing" in report.refused
        assert await _control(m, "R01") == "ACTIVE"

    @pytest.mark.asyncio
    async def test_broker_unavailable_places_nothing(self, maker):
        m = await _seed(maker)
        report = await _run(m, None, "place")
        assert report.exit_code == share_rehearsal.EXIT_BROKER_UNAVAILABLE
        assert await _rows(m, ShareOrderModel) == []


# ---------------------------------------------------------------------------
# The phases, end to end through B36's own code
# ---------------------------------------------------------------------------


class TestFullCycle:
    @pytest.mark.asyncio
    async def test_place_status_unwind_status(self, maker):
        m = await _seed(maker)
        broker = FakeBroker()
        cash0 = await _cash(m, "R01")
        b36_cash0 = await _cash(m, "B36")

        # place: three 1-share DAY limit buys, 2% through Monday's close.
        report = await _run(m, broker, "place")
        assert report.exit_code == 0, report.lines
        assert sorted((s, side, q, lim) for s, side, q, lim, _ in broker.placed) == [
            ("IAUM", "BUY", 1, buy_limit(CLOSES["IAUM"])),
            ("SCHF", "BUY", 1, buy_limit(CLOSES["SCHF"])),
            ("SCHH", "BUY", 1, buy_limit(CLOSES["SCHH"])),
        ]
        orders = await _rows(m, ShareOrderModel, book_id="R01")
        assert {o.status for o in orders} == {"SUBMITTED"}
        assert {o.purpose for o in orders} == {SHARE_ORDER_PURPOSE_REBALANCE}
        assert all(o.order_ref.startswith("basis:R01:") and o.order_ref.endswith(":share") for o in orders)
        assert broker.reconciled_with  # reconcile ran before placement

        # status before the fills: in flight, the broker unmoved, clean.
        report = await _run(m, broker, "status")
        assert report.exit_code == 0 and any("in flight" in line for line in report.lines)

        # The buys fill. Before the sync, the broker holds shares the books
        # do not — explained by R01's pending orders, never drift.
        broker.fill_everything()
        report = await _run(m, broker, "status")
        assert report.exit_code == 0, report.lines
        assert await _holdings(m, "R01") == {"IAUM": 1.0, "SCHF": 1.0, "SCHH": 1.0}
        spent = sum(CLOSES[s] for s in ("IAUM", "SCHF", "SCHH")) + 3 * 1.0
        assert await _cash(m, "R01") == pytest.approx(cash0 - spent)
        assert "reconciliation: CLEAN" in report.lines
        assert any("SCHH: broker 1, books expect 1 (R01 1" in line for line in report.lines)
        assert any("next: `unwind`" in line for line in report.lines)

        # B36 saw none of it.
        assert await _holdings(m, "B36") == {}
        assert await _cash(m, "B36") == b36_cash0
        assert await _rows(m, ShareOrderModel, book_id="B36") == []

        # unwind: the flatten path sells every share.
        report = await _run(m, broker, "unwind")
        assert report.exit_code == 0, report.lines
        assert await _control(m, "R01") == "FLATTEN_REQUESTED"
        sells = [(s, side, q, lim) for s, side, q, lim, _ in broker.placed if side == "SELL"]
        assert sorted(sells) == [
            ("IAUM", "SELL", 1, sell_limit(CLOSES["IAUM"])),
            ("SCHF", "SELL", 1, sell_limit(CLOSES["SCHF"])),
            ("SCHH", "SELL", 1, sell_limit(CLOSES["SCHH"])),
        ]
        flatten_orders = await _rows(m, ShareOrderModel, book_id="R01", purpose=SHARE_ORDER_PURPOSE_FLATTEN)
        assert len(flatten_orders) == 3 and {o.signal_date for o in flatten_orders} == {CLOSE_DAY}
        assert {e.payload["scope"] for e in await _rows(m, AuditEventModel, event_type="SHARE_FLATTEN_SUBMITTED")} == {
            "R01"
        }

        # The sells fill; status books them, R01 is flat, clean, complete.
        broker.fill_everything()
        report = await _run(m, broker, "status")
        assert report.exit_code == 0, report.lines
        assert await _holdings(m, "R01") == {}
        assert await _cash(m, "R01") == pytest.approx(cash0 - 6 * 1.0)  # round trip at the same price: commissions
        assert "reconciliation: CLEAN" in report.lines
        assert any("REHEARSAL COMPLETE" in line for line in report.lines)
        # Never resumed by the command (ADR-0008): the console does that.
        assert await _control(m, "R01") == "FLATTEN_REQUESTED"
        assert broker.closed

        runs = await _rows(m, AuditEventModel, event_type=share_rehearsal.SHARE_REHEARSAL_RUN)
        assert [r.payload["phase"] for r in runs] == ["place", "status", "status", "unwind", "status"]
        assert {r.payload["outcome"] for r in runs} == {"OK"}

    @pytest.mark.asyncio
    async def test_status_reports_real_drift_and_exits_3(self, maker):
        m = await _seed(maker)
        async with m() as session:
            session.add(ShareHoldingModel(book_id="R01", symbol="SCHH", quantity=1.0, updated_at="t0"))
            await session.commit()
        broker = FakeBroker()  # the broker holds no SCHH: a hand sale, say
        report = await _run(m, broker, "status")
        assert report.exit_code == share_rehearsal.EXIT_DRIFT
        assert any(line.startswith("  DRIFT: SHARE_DRIFT SCHH") for line in report.lines)
        # Read-only: no reconciliation row, no halt latched.
        from backend.models import ReconciliationRunModel

        assert await _rows(m, ReconciliationRunModel) == []
        assert await _control(m, "GLOBAL") == "ACTIVE"

    @pytest.mark.asyncio
    async def test_unwind_skips_a_drifted_symbol_as_the_evening_run_would(self, maker):
        m = await _seed(maker)
        async with m() as session:
            for symbol in ("SCHH", "SCHF"):
                session.add(ShareHoldingModel(book_id="R01", symbol=symbol, quantity=1.0, updated_at="t0"))
            await session.commit()
        broker = FakeBroker()
        broker.stk = {"SCHF": 1.0}  # SCHH already sold by hand
        report = await _run(m, broker, "unwind")
        assert [(s, side) for s, side, *_ in broker.placed] == [("SCHF", "SELL")]
        assert any("FLATTEN R01 SCHH: NOT sold tonight" in line for line in report.lines)

    @pytest.mark.asyncio
    async def test_an_unfilled_buy_expires_and_place_can_run_again(self, maker):
        m = await _seed(maker)
        broker = FakeBroker()
        await _run(m, broker, "place", symbols=("SCHH",))
        (ref,) = [r for *_, r in broker.placed]
        broker.states[ref] = RefState.CANCELLED  # the DAY limit expired at the close
        await _run(m, broker, "status")
        (order,) = await _rows(m, ShareOrderModel, book_id="R01")
        assert order.status == "CANCELLED" and await _holdings(m, "R01") == {}
        report = await _run(m, broker, "place", symbols=("SCHH",))
        assert report.exit_code == 0 and len(broker.placed) == 2


# ---------------------------------------------------------------------------
# Evidence isolation
# ---------------------------------------------------------------------------


async def _rehearse_to_holdings(maker_pair) -> async_sessionmaker:
    m = await _seed(maker_pair)
    broker = FakeBroker()
    await _run(m, broker, "place")
    broker.fill_everything()
    await _run(m, broker, "status")
    assert await _holdings(m, "R01") == {"IAUM": 1.0, "SCHF": 1.0, "SCHH": 1.0}
    return m


class TestEvidenceIsolation:
    @pytest.mark.asyncio
    async def test_book_summaries_never_list_r01_and_b36_has_no_fill(self, maker):
        from backend.console import book_summaries

        m = await _rehearse_to_holdings(maker)
        async with m() as session:
            summaries = await book_summaries(session)
        ids = {s.id for s in summaries}
        assert "R01" not in ids and "B36" in ids
        b36 = next(s for s in summaries if s.id == "B36")
        assert b36.share_holdings == []
        assert b36.trend_yardstick is not None
        # The yardstick clock opens at B36's own first fill; R01's do not start it.
        assert b36.trend_yardstick.first_fill_date is None
        assert b36.trend_yardstick.ok is False

    @pytest.mark.asyncio
    async def test_the_null_drill_never_pools_r01(self, maker):
        from backend.empirical_null_drill import load_haircut_pnls_by_book

        m = await _rehearse_to_holdings(maker)
        async with m() as session:
            assert "R01" not in await load_haircut_pnls_by_book(session)

    @pytest.mark.asyncio
    async def test_evidence_counts_r01_as_no_raced_book(self, maker):
        from backend import evidence

        m = await _rehearse_to_holdings(maker)
        async with m() as session:
            books = (await session.execute(select(BookModel))).scalars().all()
        raced = [b for b in books if b.status in ("ACTIVE", "RETIRED")]
        assert "R01" not in {b.id for b in raced}
        assert "R01" not in evidence._EXCLUDED_BOOK_IDS  # excluded by status, not by a hand-kept list

    @pytest.mark.asyncio
    async def test_distribution_attribution_never_names_r01(self, maker):
        from backend.share_distributions import _owners

        m = await _rehearse_to_holdings(maker)
        async with m() as session:
            owners = await _owners(session)
        assert all("R01" not in books for books in owners.values())

    @pytest.mark.asyncio
    async def test_reconciliation_counts_r01_holdings_clean(self, maker):
        from backend.reconciliation import BrokerSnapshot, compare_books

        m = await _rehearse_to_holdings(maker)
        positions = tuple(
            LegPosition(con_id=i, symbol=s, sec_type="STK", position=1.0, avg_cost=1.0)
            for i, s in enumerate(("IAUM", "SCHF", "SCHH"))
        )
        async with m() as session:
            comparison = await compare_books(session, BrokerSnapshot(positions=positions), today="2026-10-06")
        assert comparison.drifts == ()
        assert comparison.expected_shares == {"IAUM": 1.0, "SCHF": 1.0, "SCHH": 1.0}

    @pytest.mark.asyncio
    async def test_no_automated_path_acts_on_r01(self, maker):
        from backend import share_book

        m = await _rehearse_to_holdings(maker)
        async with m() as session:
            # Month-end: only ACTIVE share books rebalance; R01 is neither.
            result = await share_book.run_etf_trend_rebalances(session, FakeBroker(), datetime.date(2026, 10, 30))
            assert not any("R01" in note for note in result.notes)
            assert not any("R01" in note for note in await share_book.rebalance_watch_notes(session, NOW.date()))
        orders = await _rows(m, ShareOrderModel, book_id="R01")
        assert len(orders) == 3  # only the rehearsal's own buys


def test_main_parses_symbols_and_phase(monkeypatch):
    seen: dict = {}

    async def _fake(phase, symbols):
        seen.update(phase=phase, symbols=symbols)
        return share_rehearsal.RehearsalReport(phase=phase)

    async def _no_init():
        return None

    monkeypatch.setattr(share_rehearsal, "run_rehearsal", _fake)
    monkeypatch.setattr("backend.database.init_db", _no_init)
    monkeypatch.setattr("backend.run_logging.setup_run_logging", lambda name: None)
    assert share_rehearsal.main(["place", "--symbols", "schh, schf"]) == 0
    assert seen == {"phase": "place", "symbols": ("SCHH", "SCHF")}
    assert share_rehearsal._parse_symbols(None) == share_rehearsal.DEFAULT_SYMBOLS
