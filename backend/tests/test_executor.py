"""Tests for the Executor (Paper) nightly pipeline (backend/executor.py, #70).

Broker I/O is a FakeBroker at the BrokerSession surface; market data is
patched at the operator/executor import sites. Everything else — gates,
control, reconciliation, order/position state — runs for real against a
temp-file database seeded the way init_db seeds production.
"""

import copy
import datetime
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import executor as executor_mod
from backend import operator as operator_mod
from backend.broker import ConnectionFailedError, FillInfo, LegPosition, PlacedOrder, ReconcileReport, RefState
from backend.database import LAB_BOOKS, SEED_PLAYBOOKS, SEED_PORTFOLIO_CONFIG
from backend.dates import market_today
from backend.executor import run_executor_evening
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    ClosurePostMortemModel,
    GateEventModel,
    MarketStateModel,
    OrderModel,
    PlaybookDefinitionModel,
    PortfolioConfigModel,
    PositionModel,
    ReconciliationRunModel,
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
        self.cancelled_refs: list[str] = []
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

    def cancel_by_ref(self, ref):
        self.cancelled_refs.append(ref)
        return self.ref_states.get(ref) is RefState.OPEN


@pytest_asyncio.fixture
async def session_maker(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECUTOR_HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
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


class TestRunLock:
    @pytest.mark.asyncio
    async def test_held_lock_aborts_without_trading(self, session_maker, tmp_path):
        # H5 (#275): a concurrent run would double-close and double-adjust cash.
        (tmp_path / "executor.lock").write_text('{"pid": 1}')
        broker = FakeBroker()
        summary = await _run(session_maker, broker)
        assert broker.opened is False
        assert broker.placed == []
        assert any("RUN LOCK HELD" in n for n in summary.notes)
        assert await _audits(session_maker, "RUN_LOCK_HELD")

    @pytest.mark.asyncio
    async def test_stale_lock_is_broken_and_the_run_proceeds(self, session_maker, tmp_path):
        import os

        lock = tmp_path / "executor.lock"
        lock.write_text('{"pid": 1}')
        ancient = 1_000_000_000  # 2001 — far past the 2h staleness bound
        os.utime(lock, (ancient, ancient))
        broker = FakeBroker()
        summary = await _run(session_maker, broker)
        # The run proceeded (reconciliation ran; broker.close() resets .opened)
        assert summary.reconciliation == "CLEAN"
        assert not any("RUN LOCK HELD" in n for n in summary.notes)
        assert not lock.exists()  # released after the run


class TestBrokerDown:
    @pytest.mark.asyncio
    async def test_failure_is_audited_and_heartbeat_still_written(self, session_maker, tmp_path):
        broker = FakeBroker()
        broker.fail_open = ConnectionFailedError("gateway down")
        summary = await _run(session_maker, broker)
        assert summary.broker_ok is False
        assert await _audits(session_maker, "EXECUTOR_BROKER_UNAVAILABLE")
        assert (tmp_path / "heartbeat.json").exists()


class TestCrashNight:
    @pytest.mark.asyncio
    async def test_unexpected_crash_withholds_heartbeat_and_releases_lock(self, session_maker, tmp_path):
        # Audit II (#341): the finally-block heartbeat used to stamp a crashed
        # night healthy — silencing the 22:00 watchdog on exactly the night it
        # exists for. A crash now leaves the heartbeat stale and propagates to
        # the entrypoint, which alerts.
        broker = FakeBroker()

        def boom():
            raise RuntimeError("boom mid-run")

        broker.positions = boom
        with pytest.raises(RuntimeError, match="boom mid-run"):
            await _run(session_maker, broker)
        assert not (tmp_path / "heartbeat.json").exists()
        assert not (tmp_path / "executor.lock").exists()  # lock still released
        assert broker.opened is False  # broker.close() still ran


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
        entry_orders = [o for o in orders if o.action == "OPEN"]
        tp_orders = {o.order_ref: o for o in orders if o.order_ref.endswith(":tp")}
        # The bull put (income, 50% take) profit-taker buys back at half the credit
        bull_put = next(o for o in entry_orders if o.combo_legs["strategy_type"] == "BULL_PUT_SPREAD")
        _, _, bp_tp = next(p for p in broker.placed if p[1] == bull_put.order_ref)
        assert bull_put.limit_price < 0  # credit
        assert bp_tp == round(bull_put.limit_price * 0.5, 2)
        # Every profit-taker child gets its own row (#258): a GTC order that
        # can fill weeks later must be visible to the fill sync.
        for entry in entry_orders:
            tp = tp_orders[f"{entry.order_ref}:tp"]
            assert tp.action == "CLOSE"
            assert tp.status == "SUBMITTED"
            assert tp.position_id is None  # linked at parent fill
            assert tp.encumbered_risk == 0.0
        assert all(o.status == "SUBMITTED" for o in orders)
        assert all(o.ib_perm_id for o in entry_orders)
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
    async def test_consensus_book_abstains_when_engines_disagree(self, session_maker):
        # B29 (#316): only V0 has a reading in the default rig (V1-V3 are
        # INSUFFICIENT_DATA), so consensus is 1/3 — the book must sit out.
        broker = FakeBroker()
        summary = await _run(session_maker, broker)
        assert not any(r.startswith("basis:B29:") for _, r, _ in broker.placed)
        assert any(b.book_id == "B29" and "consensus 1/3" in b.reason for b in summary.entries_blocked)
        (event,) = await _audits(session_maker, "ENTRIES_BLOCKED_NO_CONSENSUS")
        assert event.book_id == "B29"
        assert event.payload["votes"] == 1 and event.payload["required"] == 3

    @pytest.mark.asyncio
    async def test_consensus_book_trades_when_engines_agree(self, session_maker):
        agreeing = {"V0": "CALM_BULL", "V1": "CALM_BULL", "V2": "CALM_BULL", "V3": "TRENDING_BEAR"}
        broker = FakeBroker()
        p1, p2, p3, p4 = _patches()
        with (
            p1,
            p2,
            p3,
            p4,
            patch.object(executor_mod, "persist_regime_readings", return_value=agreeing),
        ):
            summary = await run_executor_evening(session_maker=session_maker, broker_factory=lambda: broker)
        assert not any(b.book_id == "B29" for b in summary.entries_blocked)
        assert not await _audits(session_maker, "ENTRIES_BLOCKED_NO_CONSENSUS")
        assert any(r.startswith("basis:B29:") for _, r, _ in broker.placed)

    @pytest.mark.asyncio
    async def test_absurd_quotes_are_skipped_not_traded(self, session_maker):
        # H8 (#282): a spread mid beyond its strike width is a stale close or
        # broken quote — never a price to trade.
        def _absurd(syms):
            ordered = sorted(syms, key=lambda s: int(s[-8:]))
            return {s: float(i * 25) for i, s in enumerate(ordered)}  # $25 apart per strike

        broker = FakeBroker()
        p1, p2, p3, p4 = _patches(entry_quotes=_absurd)
        with p1, p2, p3, p4:
            await run_executor_evening(session_maker=session_maker, broker_factory=lambda: broker)
        # Only B21's calendar (same strike, two expiries — span 0, no width
        # bound applies) may trade; every vertical is blocked as absurd.
        assert all(ref.startswith("basis:B21:") for _, ref, _ in broker.placed)
        events = await _audits(session_maker, "CANDIDATE_UNPRICEABLE")
        assert any("absurd quote" in (e.payload.get("reason") or "") for e in events)

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
    async def test_fractional_strikes_reach_the_broker_unrounded(self, session_maker):
        # Audit II (#343): `round(leg.strike)` moved B30's legs off AAPL's
        # $2.50 grid — 232.5 became 232 (a strike that doesn't exist) while
        # banker's rounding sent 237.5 to 238, silently reshaping the spread.
        from types import SimpleNamespace

        from backend.executor import ExecutorRunSummary, _try_place_entry

        def leg(action: str, strike: float) -> SimpleNamespace:
            return SimpleNamespace(
                action=action, option_type="PUT", strike=strike, expiration_date="2026-10-30", quantity=1
            )

        spec = SimpleNamespace(
            underlying="AAPL",
            strategy_type="BULL_PUT_SPREAD",
            expiration_date="2026-10-30",
            max_loss_dollars=250.0,
            legs=[leg("BUY", 232.5), leg("SELL", 237.5)],
        )
        broker = FakeBroker()
        summary = ExecutorRunSummary(run_started_at="2026-08-20T00:00:00+00:00", run_date="2026-08-20")
        async with session_maker() as session:
            book = await session.get(BookModel, "B30")
            playbook = (
                (await session.execute(select(PlaybookDefinitionModel).filter_by(id="aapl_earnings_condor_v1")))
                .scalars()
                .one()
                .to_schema()
            )
            with patch.object(executor_mod, "fetch_options_latest_quotes", side_effect=_priced):
                ok = await _try_place_entry(session, broker, book, spec, playbook, summary)
            await session.commit()
        assert ok and len(summary.entries_placed) == 1
        (spread, ref, _tp) = broker.placed[0]
        assert {occ for occ, _a, _r in spread.legs} == {"AAPL261030P00232500", "AAPL261030P00237500"}
        async with session_maker() as session:
            order = (await session.execute(select(OrderModel).filter_by(order_ref=ref))).scalar_one()
        assert sorted(entry["strike"] for entry in order.combo_legs["legs"]) == [232.5, 237.5]

    @pytest.mark.asyncio
    async def test_reconciliation_drift_halts_entries(self, session_maker):
        broker = FakeBroker()
        broker.position_rows = [
            LegPosition(con_id=9, symbol="SPY", sec_type="STK", position=100.0, avg_cost=650.0, occ_symbol=None)
        ]
        summary = await _run(session_maker, broker)
        assert summary.reconciliation == "DRIFT"
        assert broker.placed == []  # the latched halt blocked every entry


def _snapshot(mandatory_exit_dte: int = 21) -> dict:
    """A real seed playbook, frozen the way _try_place_entry freezes it —
    valid under PlaybookDefinitionSchema so to_schema() round-trips."""
    snap = copy.deepcopy(SEED_PLAYBOOKS[0])
    snap["exit_rules"]["mandatory_exit_dte"] = mandatory_exit_dte
    return snap


def _roll_pos(pos_id: str, expiry, *, current_value: float, rolls: int = 0) -> PositionModel:
    """A B31 credit position at 12 DTE — due for the time exit (#318)."""
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
                "expiration": expiry.isoformat(),
                "delta": -0.3,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            }
        ],
        entry_date="2026-08-01",
        expiration_date=expiry.isoformat(),
        entry_premium=2.0,
        premium_direction="CREDIT",
        current_value_per_share=current_value,
        contracts=1,
        max_profit=2.0,
        max_loss=2.4,  # $240/lot — inside the 2.5% envelope
        notes="",
        rolls=rolls,
        status="OPEN",
        journal={
            "core_thesis_rationale": "t",
            "structural_invalidation": "t",
            "expected_underlying_move_pct": 1.0,
            "pre_trade_emotional_state": "Calm",
            "pre_trade_confidence_rating": 3,
        },
        playbook_snapshot=_snapshot(),
        last_priced_at=datetime.datetime.now(datetime.UTC).isoformat(),
        book_id="B31",
    )


def _roll_leg_at_broker(expiry) -> "LegPosition":
    """The broker-side mirror of _roll_pos's short leg — without it,
    reconciliation drifts and halts entries before the roll can stage."""
    return LegPosition(
        con_id=1,
        symbol="XSP",
        sec_type="OPT",
        position=-1.0,
        avg_cost=0,
        occ_symbol=f"XSP{expiry:%y%m%d}P00610000",
    )


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
    "playbook_id": "spy_iron_condor_v1",
    "playbook_version": "1.0",
    "playbook_snapshot": _snapshot(),
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
            tp = _order("o_fill_tp", "SUBMITTED", f"{ref}:tp")
            tp.action = "CLOSE"
            tp.limit_price = -0.60
            tp.encumbered_risk = 0.0
            session.add(tp)
            await session.commit()
        broker = FakeBroker()
        broker.ref_states[ref] = RefState.FILLED
        broker.ref_states[f"{ref}:tp"] = RefState.OPEN  # GTC child rests on
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
        # The playbook contract rides the fill onto the position (#260, C5):
        # exits must run under the rules the trade was ENTERED under.
        assert pos.playbook_id == "spy_iron_condor_v1"
        assert pos.playbook_version == "1.0"
        assert pos.to_schema().playbook_snapshot.exit_rules.mandatory_exit_dte == 21
        assert pos.config_hash == "h"  # the fixture book's fingerprint rides along (#284)
        # The resting profit-taker is adopted by the new position (#258).
        async with session_maker() as session:
            tp = await session.get(OrderModel, "o_fill_tp")
        assert tp.position_id == "pos_o_fill"
        assert tp.status == "SUBMITTED"

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
        assert pos.current_value_per_share == 0.30  # exit price IS the final mark (#280)

    @pytest.mark.asyncio
    async def test_profit_taker_fill_settles_even_same_day_as_entry(self, session_maker):
        # C1 (#258): the GTC child fills at the broker with no human in the
        # loop. Hardest ordering: entry AND profit-taker fill the same day —
        # the sync must create the position from the entry first, then settle
        # the child's close against it. Cash: +credit at entry, -buyback at TP.
        ref = "basis:B01:o_ft:open"
        async with session_maker() as session:
            session.add(_order("o_ft", "SUBMITTED", ref))
            tp = _order("o_ft_tp", "SUBMITTED", f"{ref}:tp")
            tp.action = "CLOSE"
            tp.combo_legs = {**ORDER_META, "exit_trigger": "PROFIT_TARGET"}
            tp.limit_price = -0.60  # buy back at half the 1.20 credit
            tp.encumbered_risk = 0.0
            session.add(tp)
            await session.commit()
        broker = FakeBroker()
        broker.ref_states[ref] = RefState.FILLED
        broker.ref_states[f"{ref}:tp"] = RefState.FILLED
        summary = await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_o_ft")
            tp_row = await session.get(OrderModel, "o_ft_tp")
            book = await session.get(BookModel, "B01")
        assert pos.status == "CLOSED"
        assert tp_row.status == "FILLED"
        assert tp_row.position_id == "pos_o_ft"
        assert book.cash_balance == 10000.0 + 120.0 - 60.0
        assert summary.reconciliation == "CLEAN"  # opened and closed → flat at broker
        # The closure wrote its expectancy row (#261): 1.20 in, 0.60 out.
        async with session_maker() as session:
            pm = (
                (await session.execute(select(ClosurePostMortemModel).filter_by(position_id="pos_o_ft")))
                .scalars()
                .one()
            )
        assert pm.exit_trigger == "PROFIT_TARGET"
        assert pm.outcome == "WIN"
        assert pm.realized_pnl == 60.0

    @pytest.mark.asyncio
    async def test_partial_fill_latches_and_halts_the_book(self, session_maker):
        # M1 (#283): a cancelled order that executed SOMETHING first must not
        # book at full size or vanish quietly — latch PARTIAL, halt the book,
        # leave the correction to a human.
        ref = "basis:B01:o_part:open"
        async with session_maker() as session:
            session.add(_order("o_part", "SUBMITTED", ref))
            await session.commit()
        broker = FakeBroker()
        broker.ref_states[ref] = RefState.CANCELLED
        broker.execution_rows = [
            FillInfo(exec_id="e_part1", con_id=1, side="SLD", quantity=1.0, price=1.85, order_ref=ref, commission=1.0)
        ]
        await _run(session_maker, broker)
        async with session_maker() as session:
            order = await session.get(OrderModel, "o_part")
            control = await session.get(TradingControlModel, "B01")
        assert order.status == "PARTIAL"
        assert control.state == "HALT_ENTRIES"
        assert await _audits(session_maker, "PARTIAL_FILL")
        # The latch is one-shot: the next run must not re-alert.
        broker2 = FakeBroker()
        broker2.ref_states[ref] = RefState.CANCELLED
        await _run(session_maker, broker2)
        assert len(await _audits(session_maker, "PARTIAL_FILL")) == 1

    @pytest.mark.asyncio
    async def test_missed_night_gap_is_announced(self, session_maker):
        # M2 (#283): a skipped night's fills are unrecoverable by the nightly
        # APIs — the run must say so, not pretend continuity.
        async with session_maker() as session:
            session.add(
                ReconciliationRunModel(
                    run_at="2026-08-12T22:50:00+00:00",  # a week ago
                    broker_snapshot={},
                    books_expected={},
                    result="CLEAN",
                    drift_details=None,
                )
            )
            await session.commit()
        broker = FakeBroker()
        summary = await _run(session_maker, broker)
        assert any("MISSED NIGHT" in n for n in summary.notes)
        assert await _audits(session_maker, "MISSED_NIGHT_GAP")

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


def _expired_pos(pos_id: str, expiry_iso: str, value: float = 0.10) -> PositionModel:
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
                "expiration": expiry_iso,
                "delta": -0.3,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            }
        ],
        entry_date="2026-07-01",
        expiration_date=expiry_iso,
        entry_premium=1.20,
        premium_direction="CREDIT",
        current_value_per_share=value,
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


class TestExpirySettlement:
    @pytest.mark.asyncio
    async def test_expired_position_settles_at_last_mark(self, session_maker):
        # C4 (#261): expiry is a settlement event, not a drift. IB purged the
        # legs and the resting TP overnight; the run cash-settles at the last
        # mark, writes the EXPIRY post-mortem, and stays CLEAN.
        expiry = (market_today() - datetime.timedelta(days=1)).isoformat()
        async with session_maker() as session:
            session.add(_expired_pos("pos_exp", expiry))
            tp = _order("o_exp_tp", "SUBMITTED", "basis:B01:o_exp:open:tp")
            tp.action = "CLOSE"
            tp.position_id = "pos_exp"
            tp.limit_price = -0.60
            tp.encumbered_risk = 0.0
            session.add(tp)
            await session.commit()
        broker = FakeBroker()  # nothing at the broker: legs and TP purged
        summary = await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_exp")
            tp_row = await session.get(OrderModel, "o_exp_tp")
            book = await session.get(BookModel, "B01")
            pm = (
                (await session.execute(select(ClosurePostMortemModel).filter_by(position_id="pos_exp"))).scalars().one()
            )
        assert pos.status == "EXPIRED"
        assert book.cash_balance == 10000.0 - 10.0  # buy back the 0.10 mark
        assert pm.exit_trigger == "EXPIRY"
        assert pm.outcome == "WIN"
        assert pm.realized_pnl == 110.0  # 1.20 collected, 0.10 paid
        assert tp_row.status == "CANCELLED"
        assert summary.reconciliation == "CLEAN"
        # The vanished TP is expected at expiry — never an urgent LOST alert.
        assert not await _audits(session_maker, "ORDER_LOST_AT_BROKER")
        assert await _audits(session_maker, "ORDER_EXPIRED_AT_BROKER")
        assert await _audits(session_maker, "POSITION_EXPIRED")
        assert any("cash-settled at expiry" in n for n in summary.notes)

    @pytest.mark.asyncio
    async def test_unpurged_expired_legs_do_not_drift(self, session_maker):
        # IB's purge timing is its own: legs still visible the evening AFTER
        # expiry must not read as an orphan (#261).
        expiry = market_today() - datetime.timedelta(days=1)
        async with session_maker() as session:
            session.add(_expired_pos("pos_exp2", expiry.isoformat()))
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{expiry:%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ),
        ]
        summary = await _run(session_maker, broker)
        assert summary.reconciliation == "CLEAN"
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_exp2")
        assert pos.status == "EXPIRED"


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
                last_priced_at=datetime.datetime.now(datetime.UTC).isoformat(),  # fresh mark (#280)
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
    async def test_roll_arm_stages_close_and_roll_entry_for_losers(self, session_maker):
        # B31 (#318): a LOSING position leaving on the time exit gets a
        # roll-out entry staged alongside its close — same strikes, next
        # cycle, lineage in the order meta.
        expiry = market_today() + datetime.timedelta(days=12)
        pos = _roll_pos("pos_roll", expiry, current_value=2.6)  # entry 2.0 credit → losing
        async with session_maker() as session:
            session.add(pos)
            await session.commit()
        broker = FakeBroker()
        broker.position_rows = [_roll_leg_at_broker(expiry)]
        summary = await _run(session_maker, broker)
        assert any("B31" in ref and ref.endswith(":close") for ref in summary.closes_placed)
        assert any(r.startswith("basis:B31:") and r.endswith(":open") for _, r, _ in broker.placed)
        async with session_maker() as session:
            b31_entries = (
                (await session.execute(select(OrderModel).filter_by(book_id="B31", action="OPEN"))).scalars().all()
            )
        # Layer C may also stage B31's ordinary nightly entry — the roll is
        # the one carrying lineage.
        (roll_entry,) = [o for o in b31_entries if o.combo_legs.get("rolled_from")]
        meta = roll_entry.combo_legs
        assert meta["rolled_from"] == "pos_roll"
        assert meta["rolls"] == 1
        assert meta["expiration_date"] > expiry.isoformat()  # next cycle, not the dying one
        assert {(leg["option_type"], leg["direction"], leg["strike"]) for leg in meta["legs"]} == {
            ("PUT", "SHORT", 610.0)
        }
        assert (await _audits(session_maker, "ROLL_STAGED"))[0].payload["roll_number"] == 1
        # The roll entry gets its own GTC profit-taker child like any entry.
        async with session_maker() as session:
            tp = (
                (await session.execute(select(OrderModel).filter_by(order_ref=f"{roll_entry.order_ref}:tp")))
                .scalars()
                .one_or_none()
            )
        assert tp is not None

    @pytest.mark.asyncio
    async def test_roll_arm_lets_winners_leave_and_respects_the_cap(self, session_maker):
        expiry = market_today() + datetime.timedelta(days=12)
        winner = _roll_pos("pos_win", expiry, current_value=1.0)  # entry 2.0 credit → winning
        capped = _roll_pos("pos_cap", expiry, current_value=2.6, rolls=2)
        async with session_maker() as session:
            session.add(winner)
            session.add(capped)
            await session.commit()
        broker = FakeBroker()
        leg = _roll_leg_at_broker(expiry)
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-2.0, avg_cost=0, occ_symbol=leg.occ_symbol)
        ]
        summary = await _run(session_maker, broker)
        # Both still close on the time exit…
        assert sum(1 for ref in summary.closes_placed if "B31" in ref) == 2
        # …but neither rolls: winners have nothing to repair, the cap is the
        # cap. (Layer C's ordinary nightly entry for B31 may still exist —
        # rolls are the orders carrying lineage.)
        async with session_maker() as session:
            b31_entries = (
                (await session.execute(select(OrderModel).filter_by(book_id="B31", action="OPEN"))).scalars().all()
            )
        assert not [o for o in b31_entries if o.combo_legs.get("rolled_from")]
        assert not await _audits(session_maker, "ROLL_STAGED")

    @pytest.mark.asyncio
    async def test_rolled_fill_creates_position_with_lineage(self, session_maker):
        # The roll entry's fill carries rolls and rolled_from into the new
        # position via the normal order→position path.
        order = _order("o_rolled", "SUBMITTED", "basis:B31:o_rolled:open")
        order.book_id = "B31"
        order.combo_legs = {**ORDER_META, "rolls": 1, "rolled_from": "pos_old"}
        async with session_maker() as session:
            session.add(order)
            await session.commit()
        broker = FakeBroker()
        broker.ref_states["basis:B31:o_rolled:open"] = RefState.FILLED
        await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_o_rolled")
        assert pos is not None
        assert pos.rolls == 1
        assert pos.journal["rolled_from"] == "pos_old"

    @pytest.mark.asyncio
    async def test_time_exit_honors_the_positions_own_snapshot(self, session_maker):
        # C3 (#260): the DTE rule is "mandatory" — the executor CLOSES, it
        # doesn't warn. And the threshold is each position's frozen playbook
        # snapshot: at 12 DTE, a default (21) position closes while a
        # 7-DTE-override position from the same book rides.
        expiry = market_today() + datetime.timedelta(days=12)
        occ = f"XSP{expiry:%y%m%d}P00610000"

        def _pos(pos_id, snapshot):
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
                        "expiration": expiry.isoformat(),
                        "delta": -0.3,
                        "theta": 0.02,
                        "vega": 0.1,
                        "gamma": 0.01,
                    }
                ],
                entry_date="2026-08-01",
                expiration_date=expiry.isoformat(),
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
                },
                playbook_snapshot=snapshot,
                last_priced_at=datetime.datetime.now(datetime.UTC).isoformat(),  # fresh mark (#280)
                book_id="B01",
            )

        async with session_maker() as session:
            session.add(_pos("pos_due", _snapshot()))
            session.add(_pos("pos_tight", _snapshot(mandatory_exit_dte=7)))
            # pos_due's resting GTC profit-taker: must come down before the
            # manual close goes up, and must NOT count as an escalation rung.
            tp = _order("o_due_tp", "SUBMITTED", "basis:B01:o_due:open:tp")
            tp.action = "CLOSE"
            tp.position_id = "pos_due"
            tp.limit_price = -1.00
            tp.encumbered_risk = 0.0
            session.add(tp)
            await session.commit()
        broker = FakeBroker()
        broker.ref_states["basis:B01:o_due:open:tp"] = RefState.OPEN
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-2.0, avg_cost=0, occ_symbol=occ),
        ]
        summary = await _run(session_maker, broker)
        async with session_maker() as session:
            due_closes = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_due", action="CLOSE")))
                .scalars()
                .all()
            )
            tight_closes = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_tight", action="CLOSE")))
                .scalars()
                .all()
            )
        due_manual = [o for o in due_closes if not o.order_ref.endswith(":tp")]
        due_tp = [o for o in due_closes if o.order_ref.endswith(":tp")]
        assert len(due_manual) == 1
        assert tight_closes == []
        assert any(":close" in ref for ref in summary.closes_placed)
        # The profit-taker came down first and didn't inflate the ladder rung:
        # rung 0 closes at the marked value, no concession.
        assert "basis:B01:o_due:open:tp" in broker.cancelled_refs
        assert due_tp[0].status == "CANCELLED"
        assert due_manual[0].limit_price == -1.90
        assert due_manual[0].combo_legs["exit_trigger"] == "TIME_RULE"  # rides to the post-mortem (#261)

    @pytest.mark.asyncio
    async def test_flatten_requested_closes_every_position_in_scope(self, session_maker):
        # ADR-0011 (#281): the kill switch's third state closes everything in
        # the flattened scope tonight; other books' healthy positions ride.
        far = (market_today() + datetime.timedelta(days=90)).isoformat()
        flat_pos = _expired_pos("pos_flat", far, value=1.15)  # ~4% profit — no P1 of its own
        flat_pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        ride_pos = _expired_pos("pos_ride2", far, value=1.15)
        ride_pos.book_id = "B04"
        ride_pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        async with session_maker() as session:
            session.add(flat_pos)
            session.add(ride_pos)
            row = await session.get(TradingControlModel, "B01")
            row.state = "FLATTEN_REQUESTED"
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-2.0, avg_cost=0, occ_symbol=occ)
        ]
        summary = await _run(session_maker, broker)
        async with session_maker() as session:
            flat_closes = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_flat", action="CLOSE")))
                .scalars()
                .all()
            )
            ride_closes = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_ride2", action="CLOSE")))
                .scalars()
                .all()
            )
        assert len(flat_closes) == 1
        assert flat_closes[0].combo_legs["exit_trigger"] == "MANUAL"
        assert ride_closes == []
        assert any("B01" in ref for ref in summary.closes_placed)

    @pytest.mark.asyncio
    async def test_stale_mark_skips_the_close_and_alerts(self, session_maker):
        # M3 (#280): a close limit derived from a mark of unknown age chases
        # the market with garbage — skip, alert, retry once repricing works.
        stale = _expired_pos("pos_stale", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        stale.last_priced_at = None  # never live-priced (and tonight's reprice is patched to fail)
        stale.current_value_per_share = 0.30  # would be a P1 profit target
        async with session_maker() as session:
            session.add(stale)
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        await _run(session_maker, broker)
        async with session_maker() as session:
            closes = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_stale", action="CLOSE")))
                .scalars()
                .all()
            )
            pos = await session.get(PositionModel, "pos_stale")
        assert closes == []
        assert pos.status == "OPEN"
        assert await _audits(session_maker, "STALE_MARK_CLOSE_SKIPPED")

    @pytest.mark.asyncio
    async def test_exhausted_ladder_stops_conceding_and_escalates(self, session_maker):
        # M3 (#280): five unfilled evenings means the concession isn't the
        # problem — stop chasing, tell the human.
        pos = _expired_pos("pos_ladder", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        pos.current_value_per_share = 0.30  # P1 profit target every night
        async with session_maker() as session:
            session.add(pos)
            for i in range(5):
                prior = _order(f"o_lad{i}", "CANCELLED", f"basis:B01:o_lad{i}:close")
                prior.action = "CLOSE"
                prior.position_id = "pos_ladder"
                prior.encumbered_risk = 0.0
                session.add(prior)
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        await _run(session_maker, broker)
        async with session_maker() as session:
            closes = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_ladder", action="CLOSE")))
                .scalars()
                .all()
            )
        assert len(closes) == 5  # the priors only — no sixth attempt
        assert await _audits(session_maker, "CLOSE_LADDER_EXHAUSTED")

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
                    last_priced_at=datetime.datetime.now(datetime.UTC).isoformat(),  # fresh mark (#280)
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
        # Entries placed tonight each carry a :tp child row (#258) — the
        # manual Layer A close is the only non-TP close.
        close_orders = [o for o in close_orders if not o.order_ref.endswith(":tp")]
        assert len(close_orders) == 1
        assert close_orders[0].status == "SUBMITTED"
        assert close_orders[0].encumbered_risk == 0.0
