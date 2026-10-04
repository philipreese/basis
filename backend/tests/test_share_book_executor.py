"""The share book through the real evening pipeline (#1054).

run_executor_evening end to end against a temp database, a fake broker and
patched market data: the month-end night places the rebalance, the next
night's sync books its fills BEFORE reconciliation (so the broker's new
shares reconcile clean), and the options Layer C never touches the book."""

import contextlib
import datetime
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import executor as executor_mod
from backend import operator as operator_mod
from backend.broker import FillInfo, LegPosition, PlacedOrder, ReconcileReport, RefState
from backend.etf_trend import month_end_dates
from backend.executor import ExecutorRunSummary, run_executor_evening
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    IndexHistoryModel,
    MarketStateModel,
    PlaybookDefinitionModel,
    PortfolioConfigModel,
    ShareHoldingModel,
    ShareOrderModel,
    TradingControlModel,
)
from backend.seeds import LAB_BOOKS, SEED_PLAYBOOKS, SEED_PORTFOLIO_CONFIG

SIGNAL_DAY = datetime.date(2026, 10, 30)
NEXT_DAY = datetime.date(2026, 11, 2)
TELEMETRY = {"spy_price": 760.0, "spy_sma20": 750.0, "vix_close": 14.5, "spy_daily_return": 0.004}
CLOSES = {"VTI": 300.0, "VEA": 55.0, "IEF": 95.0, "GLD": 330.0, "VNQ": 90.0, "DBMF": 28.0, "SGOV": 100.5}


class ShareFakeBroker:
    """The BrokerSession surface the evening run touches, plus place_share_order."""

    def __init__(self):
        self.ref_states: dict[str, RefState] = {}
        self.execution_rows: list[FillInfo] = []
        self.position_rows: list[LegPosition] = []
        self.share_placed: list[tuple] = []
        self.option_placed: list = []
        self._next = 100

    def open(self):
        pass

    def close(self):
        pass

    def account_net_liquidation(self):
        return None

    def reconcile(self, refs, since=None):
        return ReconcileReport(states={r: self.ref_states.get(r, RefState.UNKNOWN) for r in refs})

    def executions(self, since=None):
        return list(self.execution_rows)

    def positions(self):
        return list(self.position_rows)

    def open_orders(self):
        return []

    def preview_spread(self, spread):  # pragma: no cover - options books are retired in this rig
        raise AssertionError("no option entry may be previewed")

    def place_spread(self, spread, ref, profit_target_price=None):  # pragma: no cover
        self.option_placed.append(ref)
        raise AssertionError("no option entry may be placed")

    def place_share_order(self, symbol, side, quantity, limit_price, ref):
        self._next += 1
        self.share_placed.append((symbol, side, quantity, limit_price, ref))
        return PlacedOrder(order_id=self._next, perm_id=None, ref=ref, status="PreSubmitted")


@pytest_asyncio.fixture
async def maker(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECUTOR_HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'share.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with m() as session:
        session.add(
            PortfolioConfigModel(
                id=1,
                account=SEED_PORTFOLIO_CONFIG["account"],
                risk_profile=SEED_PORTFOLIO_CONFIG["risk_profile"],
                portfolio_greek_limits=SEED_PORTFOLIO_CONFIG["portfolio_greek_limits"],
            )
        )
        session.add(
            MarketStateModel(
                id=1,
                current_regime="CALM_BULL",
                spy_price=760.0,
                spy_sma20=750.0,
                vix_close=14.5,
                spy_rv20=8.0,
                underlying_ivrs={"SPY": 25.0},
                spy_daily_return=0.004,
                catalyst_dates=[],
                regime_scores={"CALM_BULL": 6.0},
            )
        )
        for pb in SEED_PLAYBOOKS:
            session.add(
                PlaybookDefinitionModel(
                    id=pb["id"],
                    version=pb["version"],
                    name=pb["name"],
                    underlying_ticker=pb["underlying_ticker"],
                    strategy_type=pb["strategy_type"],
                    enabled=pb.get("enabled", True),
                    entry_filters=pb["entry_filters"],
                    execution_specs=pb["execution_specs"],
                    exit_rules=pb["exit_rules"],
                )
            )
        for spec in LAB_BOOKS:
            # Only the share book races here: every options book is retired,
            # so any option order placed would have to come from the share book.
            session.add(
                BookModel(
                    id=spec["id"],
                    name=spec["name"],
                    config=spec["config"],
                    config_version=1,
                    config_hash=f"hash-{spec['id']}",
                    starting_capital=10000.0,
                    cash_balance=10000.0,
                    status="ACTIVE" if spec["id"] == "B36" else "RETIRED",
                    created_at="2026-10-01T00:00:00+00:00",
                )
            )
            session.add(TradingControlModel(scope=spec["id"], state="ACTIVE", reason="", actor="t", changed_at="t0"))
        session.add(TradingControlModel(scope="GLOBAL", state="ACTIVE", reason="", actor="t", changed_at="t0"))
        for symbol, close in CLOSES.items():
            dates = month_end_dates(SIGNAL_DAY, 10) if symbol != "SGOV" else [SIGNAL_DAY]
            for i, d in enumerate(dates):
                # VTI and GLD rise into the signal; the rest fall.
                step = close * 0.01 * (1 if symbol in ("VTI", "GLD") else -1)
                session.add(
                    IndexHistoryModel(date=d.isoformat(), symbol=symbol, close=close - step * (len(dates) - 1 - i))
                )
        for symbol, close in CLOSES.items():
            session.add(IndexHistoryModel(date=NEXT_DAY.isoformat(), symbol=symbol, close=close))
        await session.commit()
    yield m
    await engine.dispose()


async def _night(m, broker, day) -> ExecutorRunSummary:
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(operator_mod, "fetch_market_telemetry", return_value=TELEMETRY))
        stack.enter_context(patch.object(operator_mod, "fetch_options_latest_quotes", return_value={}))
        stack.enter_context(patch.object(operator_mod, "fetch_index_daily_closes", return_value=None))
        stack.enter_context(patch.object(operator_mod, "spy_rv20_value", new=AsyncMock(return_value=8.0)))
        return await run_executor_evening(session_maker=m, broker_factory=lambda: broker, today=day)


@pytest.mark.asyncio
async def test_month_end_rebalance_then_next_night_books_and_reconciles_clean(maker):
    broker = ShareFakeBroker()
    night1 = await _night(maker, broker, SIGNAL_DAY)
    assert broker.option_placed == []
    placed = {(s, side): q for s, side, q, _, _ in broker.share_placed}
    assert set(placed) == {("VTI", "BUY"), ("GLD", "BUY"), ("SGOV", "BUY")}
    assert night1.share_orders_placed == [ref for *_, ref in broker.share_placed]
    assert any("ETF trend signal" in n for n in night1.notes)

    # Overnight: every order fills at the open; the broker now holds the shares.
    broker.ref_states = dict.fromkeys(night1.share_orders_placed, RefState.FILLED)
    broker.execution_rows = [
        FillInfo(f"x{i}", 7, "BOT", float(q), CLOSES[s], ref, 1.0, "2026-11-02T13:31:00+00:00")
        for i, (s, _, q, _, ref) in enumerate(broker.share_placed)
    ]
    broker.position_rows = [LegPosition(7, s, "STK", float(q), CLOSES[s]) for s, _, q, _, _ in broker.share_placed]
    broker.share_placed = []
    night2 = await _night(maker, broker, NEXT_DAY)

    assert night2.reconciliation == "CLEAN"
    assert broker.share_placed == []  # not a signal day: no trades
    async with maker() as session:
        holdings = {h.symbol: h.quantity for h in (await session.execute(select(ShareHoldingModel))).scalars().all()}
        statuses = {o.status for o in (await session.execute(select(ShareOrderModel))).scalars().all()}
        global_state = (await session.get(TradingControlModel, "GLOBAL")).state
        book = await session.get(BookModel, "B36")
    assert holdings == {s: float(q) for (s, _), q in placed.items()}
    assert statuses == {"FILLED"}
    assert global_state == "ACTIVE"
    spent = sum(q * CLOSES[s] for (s, _), q in placed.items()) + len(placed) * 1.0
    assert book.cash_balance == pytest.approx(10_000.0 - spent)
    # Equity marked with the holdings: no false PNL_SHOCK on the first fill.
    assert book.last_mtm == pytest.approx(10_000.0 - len(placed) * 1.0, abs=0.01)


async def _hold_and_flatten(m, broker, holdings: dict[str, float], scope: str = "B36") -> None:
    async with m() as session:
        for symbol, qty in holdings.items():
            session.add(ShareHoldingModel(book_id="B36", symbol=symbol, quantity=qty, updated_at="t0"))
        (await session.get(TradingControlModel, scope)).state = "FLATTEN_REQUESTED"
        await session.commit()
    broker.position_rows = [LegPosition(7, s, "STK", q, CLOSES[s]) for s, q in holdings.items()]


@pytest.mark.asyncio
async def test_flatten_sells_share_holdings_and_retries_an_unfilled_night(maker):
    # #1074 / ADR-0011 amendment: a flatten in scope of the share book sells
    # its holdings at the next evening run.
    broker = ShareFakeBroker()
    await _hold_and_flatten(maker, broker, {"VTI": 5.0, "GLD": 3.0})
    night1 = await _night(maker, broker, NEXT_DAY)
    assert night1.reconciliation == "CLEAN"
    assert sorted((s, side, q) for s, side, q, _, _ in broker.share_placed) == [("GLD", "SELL", 3), ("VTI", "SELL", 5)]
    assert night1.share_orders_placed == [ref for *_, ref in broker.share_placed]

    # Nothing filled: the DAY orders expired. The next run sells again.
    later = datetime.date(2026, 11, 3)
    async with maker() as session:
        for symbol, close in CLOSES.items():
            session.add(IndexHistoryModel(date=later.isoformat(), symbol=symbol, close=close))
        await session.commit()
    broker.ref_states = dict.fromkeys(night1.share_orders_placed, RefState.CANCELLED)
    broker.share_placed = []
    night2 = await _night(maker, broker, later)
    assert sorted((s, side, q) for s, side, q, _, _ in broker.share_placed) == [("GLD", "SELL", 3), ("VTI", "SELL", 5)]
    assert any("the flatten retries next run" in n for n in night2.notes)
    async with maker() as session:
        assert (await session.get(TradingControlModel, "B36")).state == "FLATTEN_REQUESTED"


@pytest.mark.asyncio
async def test_month_end_under_flatten_sells_instead_of_rebalancing_and_says_so(maker):
    broker = ShareFakeBroker()
    await _hold_and_flatten(maker, broker, {"VTI": 5.0}, scope="GLOBAL")
    night = await _night(maker, broker, SIGNAL_DAY)
    assert [(s, side, q) for s, side, q, _, _ in broker.share_placed] == [("VTI", "SELL", 5)]
    assert any(
        "B36 missed its month-end rebalance (2026-10-30): skipped — entries halted (GLOBAL=FLATTEN_REQUESTED)" in n
        for n in night.notes
    )


@pytest.mark.asyncio
async def test_layer_c_never_scans_a_share_book(maker):
    async with maker() as session:
        (await session.get(BookModel, "B01")).status = "ACTIVE"
        await session.commit()
        state = await session.get(MarketStateModel, 1)
        summary = ExecutorRunSummary(run_started_at=executor_mod._now(), run_date=SIGNAL_DAY.isoformat())
        # Stale telemetry: every scanned book records ENTRY_NOT_TAKEN without
        # reaching the broker — so the audit rows name exactly who was scanned.
        await executor_mod._layer_c_entries(
            session, ShareFakeBroker(), state, {"V0": "CALM_BULL"}, False, summary, SIGNAL_DAY
        )
        events = (await session.execute(select(AuditEventModel))).scalars().all()
    shuffled = [e for e in events if e.event_type == "BOOK_ORDER_SHUFFLED"]
    assert shuffled[0].payload["order"] == ["B01"]
    assert {e.book_id for e in events if e.event_type == "ENTRY_NOT_TAKEN"} == {"B01"}
