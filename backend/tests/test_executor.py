"""Tests for the Executor (Paper) nightly pipeline (backend/executor.py, #70).

Broker I/O is a FakeBroker at the BrokerSession surface; market data is
patched at the operator/executor import sites. Everything else — gates,
control, reconciliation, order/position state — runs for real against a
temp-file database seeded the way init_db seeds production.
"""

from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import executor as executor_mod
from backend import operator as operator_mod
from backend.broker import ConnectionFailedError, LegPosition, PlacedOrder, ReconcileReport, RefState
from backend.database import LAB_BOOKS, SEED_PLAYBOOKS, SEED_PORTFOLIO_CONFIG
from backend.executor import run_executor_evening
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    GateEventModel,
    MarketStateModel,
    OrderModel,
    PlaybookDefinitionModel,
    PortfolioConfigModel,
    PositionModel,
    TradingControlModel,
)

TELEMETRY = {"spy_price": 760.0, "spy_sma20": 750.0, "vix_close": 14.5, "spy_daily_return": 0.004}


def _priced(symbols: list[str]) -> dict[str, float]:
    """Deterministic quotes: price proportional to strike, so short (higher)
    strikes are worth more than long (lower) strikes — real credit spreads."""
    return {s: round(int(s[-8:]) / 1000.0 / 200.0, 2) for s in symbols}


class FakeBroker:
    def __init__(self):
        self.opened = False
        self.fail_open: Exception | None = None
        self.ref_states: dict[str, RefState] = {}
        self.execution_rows: list = []
        self.position_rows: list[LegPosition] = []
        self.open_order_rows: list = []
        self.placed: list[tuple] = []
        self.closed: list[tuple] = []
        self._next = 500

    def open(self):
        if self.fail_open:
            raise self.fail_open
        self.opened = True

    def close(self):
        self.opened = False

    def reconcile(self, refs, since=None):
        return ReconcileReport(
            states={r: self.ref_states.get(r, RefState.UNKNOWN) for r in refs},
            broker_refs=frozenset(self.ref_states),
        )

    def executions(self, since=None):
        return list(self.execution_rows)

    def positions(self):
        return list(self.position_rows)

    def open_orders(self):
        return list(self.open_order_rows)

    def _placed_order(self, ref):
        self._next += 1
        return PlacedOrder(order_id=self._next, perm_id=90000 + self._next, ref=ref, status="Submitted")

    def place_spread(self, spread, ref, profit_target_price=None):
        if getattr(self, "fail_place", None):
            raise self.fail_place
        self.placed.append((spread, ref, profit_target_price))
        return self._placed_order(ref)

    def close_spread(self, spread, ref):
        self.closed.append((spread, ref))
        return self._placed_order(ref)


@pytest_asyncio.fixture
async def session_maker(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECUTOR_HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    monkeypatch.delenv("NTFY_COMMAND_TOPIC", raising=False)
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'exec.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
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
                    execution_mode=pb["execution_mode"],
                    enabled=pb.get("enabled", True),
                    entry_filters=pb["entry_filters"],
                    execution_specs=pb["execution_specs"],
                    exit_rules=pb["exit_rules"],
                )
            )
        for spec in LAB_BOOKS:
            session.add(
                BookModel(
                    id=spec["id"],
                    name=spec["name"],
                    config=spec["config"],
                    config_version=1,
                    config_hash="h",
                    starting_capital=10000.0,
                    cash_balance=10000.0,
                    status="ACTIVE",
                    created_at="t0",
                )
            )
            session.add(TradingControlModel(scope=spec["id"], state="ACTIVE", reason="", actor="t", changed_at="t0"))
        session.add(TradingControlModel(scope="GLOBAL", state="ACTIVE", reason="", actor="t", changed_at="t0"))
        await session.commit()
    yield maker
    await engine.dispose()


def _patches(entry_quotes=None):
    """Patch every network touchpoint. entry_quotes=None means unpriceable."""
    quotes = (lambda syms: _priced(syms)) if entry_quotes is None else entry_quotes
    return (
        patch.object(operator_mod, "fetch_market_telemetry", return_value=TELEMETRY),
        patch.object(operator_mod, "fetch_options_latest_quotes", return_value={}),
        patch.object(operator_mod, "fetch_index_daily_closes", return_value=None),
        patch.object(executor_mod, "fetch_options_latest_quotes", side_effect=quotes),
    )


async def _run(maker, broker):
    p1, p2, p3, p4 = _patches()
    with p1, p2, p3, p4:
        return await run_executor_evening(session_maker=maker, broker_factory=lambda: broker)


async def _audits(maker, event_type):
    async with maker() as session:
        rows = (await session.execute(select(AuditEventModel).filter_by(event_type=event_type))).scalars().all()
    return rows


class TestBrokerDown:
    @pytest.mark.asyncio
    async def test_failure_is_audited_and_heartbeat_still_written(self, session_maker, tmp_path):
        broker = FakeBroker()
        broker.fail_open = ConnectionFailedError("gateway down")
        summary = await _run(session_maker, broker)
        assert summary.broker_ok is False
        assert await _audits(session_maker, "EXECUTOR_BROKER_UNAVAILABLE")
        assert (tmp_path / "heartbeat.json").exists()


class TestOrderPathAbort:
    @pytest.mark.asyncio
    async def test_broker_error_aborts_the_rest_of_the_submission_phase(self, session_maker):
        """Design §3.2 (#68): an order-path broker error (162-class) never
        fails soft — the first rejection ends the entire entry phase."""
        from backend.broker import BrokerError

        broker = FakeBroker()
        broker.fail_place = BrokerError("simulated competing-session 162")
        summary = await _run(session_maker, broker)
        assert summary.entries_placed == []
        rejected = await _audits(session_maker, "ORDER_REJECTED")
        assert len(rejected) == 1  # exactly one attempt, then the phase stops
        assert await _audits(session_maker, "ENTRY_PHASE_ABORTED")


class TestEntryPlacement:
    @pytest.mark.asyncio
    async def test_entries_placed_with_gtc_profit_taker(self, session_maker):
        broker = FakeBroker()
        summary = await _run(session_maker, broker)
        assert summary.reconciliation == "CLEAN"
        assert summary.entries_placed  # V0 books trade; V1/V2 blocked on INSUFFICIENT_DATA
        spread, ref, _tp = broker.placed[0]
        assert ref.startswith("basis:B0")
        assert ref.endswith(":open")
        assert spread.net_limit_price != 0
        async with session_maker() as session:
            orders = (await session.execute(select(OrderModel))).scalars().all()
            gate_events = (await session.execute(select(GateEventModel))).scalars().all()
        # The bull put (income, 50% take) profit-taker buys back at half the credit
        bull_put = next(o for o in orders if o.combo_legs["strategy_type"] == "BULL_PUT_SPREAD")
        _, _, bp_tp = next(p for p in broker.placed if p[1] == bull_put.order_ref)
        assert bull_put.limit_price < 0  # credit
        assert bp_tp == round(bull_put.limit_price * 0.5, 2)
        assert all(o.status == "SUBMITTED" for o in orders)
        assert all(o.ib_perm_id for o in orders)
        assert gate_events  # every placement went through logged gates

    @pytest.mark.asyncio
    async def test_xsp_books_trade_xsp_contracts(self, session_maker):
        broker = FakeBroker()
        await _run(session_maker, broker)
        xsp_refs = [(s, r) for s, r, _ in broker.placed if s.underlying == "XSP"]
        spy_refs = [(s, r) for s, r, _ in broker.placed if s.underlying == "SPY"]
        assert xsp_refs and spy_refs  # B01 (XSP) and B04 (SPY) both traded
        assert all(occ.startswith("XSP") for s, _ in xsp_refs for occ, _a, _r in s.legs)

    @pytest.mark.asyncio
    async def test_v1_v2_books_blocked_without_history(self, session_maker):
        broker = FakeBroker()
        summary = await _run(session_maker, broker)
        blocked_books = {b.book_id for b in summary.entries_blocked}
        assert {"B02", "B03", "B05", "B06"} <= blocked_books  # variant reading unavailable
        assert await _audits(session_maker, "ENTRIES_BLOCKED_STALE_DATA")

    @pytest.mark.asyncio
    async def test_unpriceable_candidates_do_not_trade(self, session_maker):
        broker = FakeBroker()
        p1, p2, p3, p4 = _patches(entry_quotes=lambda syms: {})
        with p1, p2, p3, p4:
            summary = await run_executor_evening(session_maker=session_maker, broker_factory=lambda: broker)
        assert broker.placed == []
        assert summary.entries_placed == []
        assert await _audits(session_maker, "CANDIDATE_UNPRICEABLE")


class TestHaltsAndStale:
    @pytest.mark.asyncio
    async def test_global_halt_blocks_placement_but_records_experiment(self, session_maker):
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
            row.state = "HALT_ENTRIES"
            await session.commit()
        broker = FakeBroker()
        summary = await _run(session_maker, broker)
        assert broker.placed == []
        assert summary.entries_placed == []
        assert await _audits(session_maker, "WOULD_HAVE_TRADED")  # experiment record survives the halt
        async with session_maker() as session:
            orders = (await session.execute(select(OrderModel))).scalars().all()
        assert all(o.status == "CANCELLED" for o in orders)  # encumbrance released

    @pytest.mark.asyncio
    async def test_stale_telemetry_blocks_all_entries(self, session_maker):
        broker = FakeBroker()
        with (
            patch.object(operator_mod, "fetch_market_telemetry", return_value=None),
            patch.object(operator_mod, "fetch_options_latest_quotes", return_value={}),
            patch.object(operator_mod, "fetch_index_daily_closes", return_value=None),
            patch.object(executor_mod, "fetch_options_latest_quotes", side_effect=_priced),
        ):
            summary = await run_executor_evening(session_maker=session_maker, broker_factory=lambda: broker)
        assert broker.placed == []
        assert any("STALE_DATA" in b.reason for b in summary.entries_blocked)

    @pytest.mark.asyncio
    async def test_reconciliation_drift_halts_entries(self, session_maker):
        broker = FakeBroker()
        broker.position_rows = [
            LegPosition(con_id=9, symbol="SPY", sec_type="STK", position=100.0, avg_cost=650.0, occ_symbol=None)
        ]
        summary = await _run(session_maker, broker)
        assert summary.reconciliation == "DRIFT"
        assert broker.placed == []  # the latched halt blocked every entry


ORDER_META = {
    "legs": [
        {
            "occ": "XSP261218P00610000",
            "option_type": "PUT",
            "direction": "SHORT",
            "strike": 610.0,
            "expiration": "2026-12-18",
        },
        {
            "occ": "XSP261218P00605000",
            "option_type": "PUT",
            "direction": "LONG",
            "strike": 605.0,
            "expiration": "2026-12-18",
        },
    ],
    "quantity": 1,
    "strategy_type": "BULL_PUT_SPREAD",
    "expiration_date": "2026-12-18",
    "underlying": "XSP",
}


def _order(order_id: str, status: str, ref: str) -> OrderModel:
    return OrderModel(
        id=order_id,
        book_id="B01",
        position_id=None,
        order_ref=ref,
        ib_order_id=100,
        ib_perm_id=90100,
        action="OPEN",
        combo_legs=ORDER_META,
        order_type="LIMIT",
        limit_price=-1.20,
        decision_midpoint=-1.25,
        status=status,
        submitted_at="t0",
        completed_at=None,
        encumbered_risk=380.0,
    )


class TestOrderStateSync:
    @pytest.mark.asyncio
    async def test_filled_entry_becomes_position_with_cash_credit(self, session_maker):
        ref = "basis:B01:o_fill:open"
        async with session_maker() as session:
            session.add(_order("o_fill", "SUBMITTED", ref))
            await session.commit()
        broker = FakeBroker()
        broker.ref_states[ref] = RefState.FILLED
        # Broker holds the resulting legs so reconciliation stays clean
        broker.position_rows = [
            LegPosition(
                con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol="XSP261218P00610000"
            ),
            LegPosition(
                con_id=2, symbol="XSP", sec_type="OPT", position=1.0, avg_cost=0, occ_symbol="XSP261218P00605000"
            ),
        ]
        summary = await _run(session_maker, broker)
        assert "pos_o_fill" in summary.positions_created
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_o_fill")
            order = await session.get(OrderModel, "o_fill")
            book = await session.get(BookModel, "B01")
        assert pos.status == "OPEN"
        assert pos.book_id == "B01"
        assert pos.premium_direction == "CREDIT"
        assert pos.entry_premium == 1.20
        assert pos.max_loss == 3.80  # encumbered_risk 380 / 100
        assert order.status == "FILLED"
        assert book.cash_balance == 10000.0 + 120.0  # credit received
        assert summary.reconciliation == "CLEAN"

    @pytest.mark.asyncio
    async def test_filled_close_debits_the_buyback_cost(self, session_maker):
        # Closing a credit spread PAYS limit_price (negative = cash out). The
        # inverted sign credited the buy-back instead — every close inflated
        # the book by 2× the exit value (#257, audit finding C2).
        ref = "basis:B01:o_cls:close"
        async with session_maker() as session:
            session.add(
                PositionModel(
                    id="pos_c",
                    underlying="XSP",
                    strategy_type="BULL_PUT_SPREAD",
                    execution_mode="PAPER",
                    legs=[],
                    entry_date="2026-08-01",
                    expiration_date="2026-12-18",
                    entry_premium=1.20,
                    premium_direction="CREDIT",
                    current_value_per_share=0.30,
                    contracts=1,
                    max_profit=1.20,
                    max_loss=3.80,
                    notes="",
                    rolls=0,
                    status="OPEN",
                    journal={
                        "core_thesis_rationale": "t",
                        "structural_invalidation": "t",
                        "expected_underlying_move_pct": 1.0,
                        "pre_trade_emotional_state": "Calm",
                        "pre_trade_confidence_rating": 3,
                    },
                    book_id="B01",
                )
            )
            close = _order("o_cls", "SUBMITTED", ref)
            close.action = "CLOSE"
            close.position_id = "pos_c"
            close.limit_price = -0.30  # pay 0.30/share to buy the spread back
            close.encumbered_risk = 0.0
            session.add(close)
            await session.commit()
        broker = FakeBroker()
        broker.ref_states[ref] = RefState.FILLED
        await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_c")
            book = await session.get(BookModel, "B01")
        assert pos.status == "CLOSED"
        assert book.cash_balance == 10000.0 - 30.0  # buy-back DEBITS cash

    @pytest.mark.asyncio
    async def test_stale_staged_intent_expires(self, session_maker):
        ref = "basis:B01:o_stale:open"
        async with session_maker() as session:
            session.add(_order("o_stale", "STAGED", ref))
            await session.commit()
        broker = FakeBroker()  # ref UNKNOWN at broker
        summary = await _run(session_maker, broker)
        assert ref in summary.intents_expired
        assert await _audits(session_maker, "INTENT_EXPIRED")
        async with session_maker() as session:
            order = await session.get(OrderModel, "o_stale")
        assert order.status == "CANCELLED"

    @pytest.mark.asyncio
    async def test_expired_day_order_releases_encumbrance(self, session_maker):
        ref = "basis:B01:o_exp:open"
        async with session_maker() as session:
            session.add(_order("o_exp", "SUBMITTED", ref))
            await session.commit()
        broker = FakeBroker()
        broker.ref_states[ref] = RefState.CANCELLED
        await _run(session_maker, broker)
        async with session_maker() as session:
            order = await session.get(OrderModel, "o_exp")
        assert order.status == "CANCELLED"
        assert await _audits(session_maker, "ORDER_EXPIRED_AT_BROKER")


class TestLayerACloses:
    @pytest.mark.asyncio
    async def test_regime_flip_closes_flagged_book_only(self, session_maker):
        # B28 (#254): entered under HIGH_VOL_NEUTRAL, tonight reads CALM_BULL
        # → close. The same situation on a non-flagged book (B01) rides.
        def _pos(pos_id, book_id):
            return PositionModel(
                id=pos_id,
                underlying="XSP",
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
                    }
                ],
                entry_date="2026-08-01",
                expiration_date="2026-12-18",
                entry_premium=2.0,
                premium_direction="CREDIT",
                current_value_per_share=1.9,  # 5% profit — no lifecycle P1
                contracts=1,
                max_profit=2.0,
                max_loss=3.0,
                notes="",
                rolls=0,
                status="OPEN",
                journal={
                    "core_thesis_rationale": "t",
                    "structural_invalidation": "t",
                    "expected_underlying_move_pct": 1.0,
                    "pre_trade_emotional_state": "Calm",
                    "pre_trade_confidence_rating": 3,
                    "entry_regime": "HIGH_VOL_NEUTRAL",
                },
                book_id=book_id,
            )

        async with session_maker() as session:
            session.add(_pos("pos_flip", "B28"))
            session.add(_pos("pos_ride", "B01"))
            await session.commit()
        broker = FakeBroker()
        broker.position_rows = [
            LegPosition(
                con_id=1, symbol="XSP", sec_type="OPT", position=-2.0, avg_cost=0, occ_symbol="XSP261218P00610000"
            ),
        ]
        summary = await _run(session_maker, broker)
        flip_closes = [ref for ref in summary.closes_placed if ":close" in ref and "B28" in ref]
        ride_closes = [ref for ref in summary.closes_placed if "B01" in ref]
        assert len(flip_closes) == 1
        assert ride_closes == []
        async with session_maker() as session:
            close_order = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_flip", action="CLOSE")))
                .scalars()
                .one()
            )
        assert close_order.status == "SUBMITTED"

    @pytest.mark.asyncio
    async def test_p1_profit_target_gets_closing_sell_combo(self, session_maker):
        async with session_maker() as session:
            session.add(
                PositionModel(
                    id="pos_p1",
                    underlying="XSP",
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
                    entry_date="2026-08-01",
                    expiration_date="2026-12-18",
                    entry_premium=2.0,
                    premium_direction="CREDIT",
                    current_value_per_share=1.0,  # 50% of max profit → P1 CLOSE NOW
                    contracts=1,
                    max_profit=2.0,
                    max_loss=3.0,
                    notes="",
                    rolls=0,
                    status="OPEN",
                    journal={
                        "core_thesis_rationale": "t",
                        "structural_invalidation": "t",
                        "expected_underlying_move_pct": 1.0,
                        "pre_trade_emotional_state": "Calm",
                        "pre_trade_confidence_rating": 3,
                    },
                    book_id="B01",
                )
            )
            await session.commit()
        broker = FakeBroker()
        broker.position_rows = [
            LegPosition(
                con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol="XSP261218P00610000"
            ),
            LegPosition(
                con_id=2, symbol="XSP", sec_type="OPT", position=1.0, avg_cost=0, occ_symbol="XSP261218P00605000"
            ),
        ]
        summary = await _run(session_maker, broker)
        assert summary.closes_placed
        (spread, ref) = broker.closed[0]
        assert ref.endswith(":close")
        assert spread.net_limit_price == -1.0  # pay the buy-back cost, rung 0 = mid
        # The closing bag mirrors the entry bag; the SELL order action reverses it
        actions = {occ: action for occ, action, _ in spread.legs}
        assert actions["XSP261218P00610000"] == "SELL"
        assert actions["XSP261218P00605000"] == "BUY"
        async with session_maker() as session:
            close_orders = (await session.execute(select(OrderModel).filter_by(action="CLOSE"))).scalars().all()
        assert len(close_orders) == 1
        assert close_orders[0].status == "SUBMITTED"
        assert close_orders[0].encumbered_risk == 0.0
