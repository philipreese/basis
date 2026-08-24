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
from backend.broker import (
    ConnectionFailedError,
    FillInfo,
    LegPosition,
    MarginPreview,
    OpenOrderInfo,
    PlacedOrder,
    ReconcileReport,
    RefState,
)
from backend.calendars import is_trading_day
from backend.database import LAB_BOOKS, SEED_PLAYBOOKS, SEED_PORTFOLIO_CONFIG
from backend.dates import market_today
from backend.executor import run_executor_evening
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    ClosurePostMortemModel,
    FillModel,
    GateEventModel,
    IndexHistoryModel,
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
        # #627: ref -> the broker's own rejection text; empty unless a test
        # opts in, mirroring ReconcileReport.rejections.
        self.rejection_reasons: dict[str, str] = {}
        self.execution_rows: list = []
        self.position_rows: list[LegPosition] = []
        # #684: run_executor_evening reads broker.positions() twice — once at
        # reconciliation, once more (fresh) right before Layer A. Unset,
        # every call just returns position_rows as-is; a test exercising the
        # SECOND read's own drift detection sets this to simulate the
        # broker-side change landing in the gap between the two reads.
        self.position_rows_after_reconciliation: list[LegPosition] | None = None
        self._positions_calls = 0
        self.open_order_rows: list = []
        self.previewed: list = []
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
            rejections=dict(self.rejection_reasons),
        )

    def executions(self, since=None):
        return list(self.execution_rows)

    def positions(self):
        self._positions_calls += 1
        if self._positions_calls > 1 and self.position_rows_after_reconciliation is not None:
            return list(self.position_rows_after_reconciliation)
        return list(self.position_rows)

    def open_orders(self):
        return list(self.open_order_rows)

    def _placed_order(self, ref):
        self._next += 1
        return PlacedOrder(order_id=self._next, perm_id=90000 + self._next, ref=ref, status="Submitted")

    def preview_spread(self, spread):
        # #626: the preview gate is now a hard precondition of every entry/
        # roll submission. Defaults to a clean approval so the existing
        # placement-focused tests don't all need to know about it; a test
        # exercising the gate itself sets fail_preview to the exception
        # preview_spread should raise (mirrors fail_open/fail_place).
        if getattr(self, "fail_preview", None):
            raise self.fail_preview
        self.previewed.append(spread)
        return MarginPreview(
            init_margin_change=100.0, maint_margin_change=100.0, commission_min=1.0, commission_max=2.0
        )

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
    # WAL + busy_timeout (#271, matches backend.database's production engine):
    # without these, two sessions writing to the same file (the race tests
    # below deliberately do this) hit SQLite's zero-timeout default and fail
    # immediately with "database is locked" instead of serializing.
    from backend.database import _install_sqlite_pragmas

    _install_sqlite_pragmas(engine.sync_engine)
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


def _patches(entry_quotes=None, index_closes=None):
    """Patch every network touchpoint. entry_quotes=None means unpriceable.
    index_closes=None means persist_index_history's fetch always misses
    (the pre-existing default — most tests pre-seed IndexHistoryModel
    directly and never rely on the pipeline's own fetch); pass a callable
    (symbol, days) -> list[(date, close)] | None to exercise the real
    persist-then-settle path (#692)."""
    quotes = (lambda syms: _priced(syms)) if entry_quotes is None else entry_quotes
    index_patch = (
        patch.object(operator_mod, "fetch_index_daily_closes", return_value=None)
        if index_closes is None
        else patch.object(operator_mod, "fetch_index_daily_closes", side_effect=index_closes)
    )
    return (
        patch.object(operator_mod, "fetch_market_telemetry", return_value=TELEMETRY),
        patch.object(operator_mod, "fetch_options_latest_quotes", return_value={}),
        index_patch,
        patch.object(executor_mod, "fetch_options_latest_quotes", side_effect=quotes),
    )


async def _run(maker, broker, index_closes=None):
    p1, p2, p3, p4 = _patches(index_closes=index_closes)
    with p1, p2, p3, p4:
        return await run_executor_evening(session_maker=maker, broker_factory=lambda: broker)


async def _audits(maker, event_type):
    async with maker() as session:
        rows = (await session.execute(select(AuditEventModel).filter_by(event_type=event_type))).scalars().all()
    return rows


def _nearest_trading_day_on_or_before(day: datetime.date) -> datetime.date:
    while not is_trading_day(day):
        day -= datetime.timedelta(days=1)
    return day


# #632: pinned to the most recent real trading day, not an arbitrary fixed
# date. Several tests build fixtures with genuinely real timestamps (e.g.
# `datetime.datetime.now(UTC)` for a "fresh mark") alongside market_today()
# -relative dates; a frozen constant far from the real calendar date reads
# as artificial drift/staleness to that logic. Rolling back from the real
# market_today() to the nearest trading day keeps this within a day or two
# of "now" (never more than a long weekend) while still guaranteeing the
# holiday guard never fires.
_FROZEN_TODAY = _nearest_trading_day_on_or_before(market_today())


@pytest.fixture(autouse=True)
def _pin_market_today(monkeypatch):
    """This whole file computes relative dates (expiry = market_today() + N
    days, staleness windows, etc.) and relies on run_executor_evening's own
    default `today` (also market_today()) landing on an actual trading day —
    the holiday guard (executor.py) correctly refuses to trade otherwise.
    Reading the real wall clock made large parts of this file fail
    deterministically on any actual Saturday/Sunday (confirmed: a completely
    unrelated flex_audit.py change surfaced dozens of failures here purely
    because it happened to be run on a real weekend). Freeze BOTH this
    module's own `market_today` (used directly in tests' date math) and
    backend.executor's copy (each did its own `from backend.dates import
    market_today` — separate name bindings) to the same fixed, known
    trading day, so the file runs deterministically regardless of the real
    calendar. A test that needs a different `today` (e.g. to exercise a
    date crossing) overrides it locally with its own monkeypatch — same
    escape hatch as before, just no longer needed to reach a baseline pass."""
    monkeypatch.setattr("backend.tests.test_executor.market_today", lambda: _FROZEN_TODAY)
    monkeypatch.setattr(executor_mod, "market_today", lambda: _FROZEN_TODAY)


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

    @pytest.mark.asyncio
    async def test_stolen_lock_mid_run_aborts_before_the_next_phase(self, session_maker, tmp_path, monkeypatch):
        # #536: a losing verify-restore race in _break_stale can strand this
        # run's lock in the graveyard while a third contender takes the
        # path — this run keeps going holding nothing. Simulate the theft
        # between _settle_expired and the first phase-boundary refresh
        # (right before reconciliation) and assert the abort lands there:
        # no reconciliation, no closes, no entries.
        import json

        original_settle = executor_mod._settle_expired

        async def stealing_settle(session, summary):
            await original_settle(session, summary)
            (tmp_path / "executor.lock").write_text(json.dumps({"pid": 999, "token": "thief"}), encoding="utf-8")

        monkeypatch.setattr(executor_mod, "_settle_expired", stealing_settle)
        broker = FakeBroker()
        summary = await _run(session_maker, broker)
        assert summary.reconciliation == "SKIPPED"  # never ran — aborted first
        assert broker.placed == []
        assert broker.closed == []
        assert any("RUN LOCK LOST" in n for n in summary.notes)
        assert await _audits(session_maker, "RUN_LOCK_LOST")
        # Not ours any more — must not release the thief's lock.
        on_disk = json.loads((tmp_path / "executor.lock").read_text(encoding="utf-8"))
        assert on_disk["token"] == "thief"


class TestBrokerDown:
    @pytest.mark.asyncio
    async def test_failure_is_audited_and_heartbeat_still_written(self, session_maker, tmp_path):
        broker = FakeBroker()
        broker.fail_open = ConnectionFailedError("gateway down")
        summary = await _run(session_maker, broker)
        assert summary.broker_ok is False
        assert await _audits(session_maker, "EXECUTOR_BROKER_UNAVAILABLE")
        assert (tmp_path / "heartbeat.json").exists()


class TestBrokerOpenNonBrokerErrorReleasesLock:
    @pytest.mark.asyncio
    async def test_non_broker_error_from_open_still_releases_the_executor_lock(self, session_maker, tmp_path):
        # #547: only `except BrokerError` guarded broker.open() — a
        # non-BrokerError escaping it (e.g. thread/factory construction
        # failing before open()'s own try/except runs) leaked the executor
        # lock until the 2h staleness break. Any exception must release it.
        broker = FakeBroker()
        broker.fail_open = RuntimeError("thread pool exhausted")
        with pytest.raises(RuntimeError, match="thread pool exhausted"):
            await _run(session_maker, broker)
        assert not (tmp_path / "executor.lock").exists()  # lock released, not leaked


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

    @pytest.mark.asyncio
    async def test_sign_inverted_credit_structure_is_blocked_not_staged(self, session_maker):
        # #621 (2026-08-21 22:45 UTC incident): a bull put spread (CREDIT) is
        # structurally correct — short strike above long strike — but a bad
        # per-leg quote (a thin, far-dated contract) produces a POSITIVE
        # net_mid within the width bound, so the existing |net_mid| >= width
        # check can't catch it. That would stage a DAY limit BUY willing to
        # PAY to open a credit structure.
        from types import SimpleNamespace

        from backend.executor import ExecutorRunSummary, _try_place_entry

        def leg(action: str, strike: float) -> SimpleNamespace:
            return SimpleNamespace(
                action=action, option_type="PUT", strike=strike, expiration_date="2026-10-30", quantity=1
            )

        spec = SimpleNamespace(
            underlying="AAPL",
            strategy_type="BULL_PUT_SPREAD",
            premium_direction="CREDIT",
            expiration_date="2026-10-30",
            max_loss_dollars=250.0,
            legs=[leg("BUY", 232.5), leg("SELL", 237.5)],
        )
        # BUY (long, lower strike) quoted ABOVE sell (short, higher strike) —
        # the real-incident shape: net_mid = 3.0 - 1.0 = +2.0, plausible
        # magnitude (width is 5.0) but the wrong sign for a credit structure.
        bad_quotes = {"AAPL261030P00232500": 3.0, "AAPL261030P00237500": 1.0}
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
            with patch.object(executor_mod, "fetch_options_latest_quotes", return_value=bad_quotes):
                ok = await _try_place_entry(session, broker, book, spec, playbook, summary)
            await session.commit()
        assert ok is True  # a skip, not an order-path abort
        assert broker.placed == []
        events = await _audits(session_maker, "CANDIDATE_UNPRICEABLE")
        (event,) = [e for e in events if e.payload.get("reason") == "sign-inverted net_mid"]
        assert event.payload["premium_direction"] == "CREDIT"
        assert event.payload["net_mid"] == 2.0

    @pytest.mark.asyncio
    async def test_sign_inverted_debit_structure_is_blocked_not_staged(self, session_maker):
        # The mirror case: a debit structure (bull call spread) must price
        # positive; a bad quote making it net negative must also be blocked.
        from types import SimpleNamespace

        from backend.executor import ExecutorRunSummary, _try_place_entry

        def leg(action: str, strike: float) -> SimpleNamespace:
            return SimpleNamespace(
                action=action, option_type="CALL", strike=strike, expiration_date="2026-10-30", quantity=1
            )

        spec = SimpleNamespace(
            underlying="AAPL",
            strategy_type="BULL_CALL_SPREAD",
            premium_direction="DEBIT",
            expiration_date="2026-10-30",
            max_loss_dollars=250.0,
            legs=[leg("BUY", 232.5), leg("SELL", 237.5)],
        )
        # BUY (long, lower strike) quoted BELOW sell (short, higher strike) —
        # net_mid = 1.0 - 3.0 = -2.0: plausible magnitude, wrong sign for DEBIT.
        bad_quotes = {"AAPL261030C00232500": 1.0, "AAPL261030C00237500": 3.0}
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
            with patch.object(executor_mod, "fetch_options_latest_quotes", return_value=bad_quotes):
                ok = await _try_place_entry(session, broker, book, spec, playbook, summary)
            await session.commit()
        assert ok is True
        assert broker.placed == []
        events = await _audits(session_maker, "CANDIDATE_UNPRICEABLE")
        (event,) = [e for e in events if e.payload.get("reason") == "sign-inverted net_mid"]
        assert event.payload["premium_direction"] == "DEBIT"
        assert event.payload["net_mid"] == -2.0

    @pytest.mark.asyncio
    async def test_correctly_signed_credit_structure_still_stages_normally(self, session_maker):
        # Non-regression: the sign gate must not block a legitimately-priced
        # credit spread (net_mid < 0, matching CREDIT).
        from types import SimpleNamespace

        from backend.executor import ExecutorRunSummary, _try_place_entry

        def leg(action: str, strike: float) -> SimpleNamespace:
            return SimpleNamespace(
                action=action, option_type="PUT", strike=strike, expiration_date="2026-10-30", quantity=1
            )

        spec = SimpleNamespace(
            underlying="AAPL",
            strategy_type="BULL_PUT_SPREAD",
            premium_direction="CREDIT",
            expiration_date="2026-10-30",
            max_loss_dollars=250.0,
            legs=[leg("BUY", 232.5), leg("SELL", 237.5)],
        )
        good_quotes = {"AAPL261030P00232500": 1.0, "AAPL261030P00237500": 3.0}
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
            with patch.object(executor_mod, "fetch_options_latest_quotes", return_value=good_quotes):
                ok = await _try_place_entry(session, broker, book, spec, playbook, summary)
            await session.commit()
        assert ok is True
        assert len(broker.placed) == 1
        assert not [
            e
            for e in await _audits(session_maker, "CANDIDATE_UNPRICEABLE")
            if e.payload.get("reason") == "sign-inverted net_mid"
        ]

    @pytest.mark.asyncio
    async def test_preview_rejection_blocks_the_order_even_when_the_sign_gate_passes(self, session_maker):
        # #626: defense in depth, deliberately NOT coupled to #621's sign
        # gate — this spec is CORRECTLY signed (net_mid < 0 for a CREDIT
        # structure, the #621 gate finds nothing wrong) so this proves the
        # preview gate is an independent precondition, not a fallback that
        # only fires when the sign gate already caught something.
        from types import SimpleNamespace

        from backend.broker import PreviewRejectedError
        from backend.executor import ExecutorRunSummary, _try_place_entry

        def leg(action: str, strike: float) -> SimpleNamespace:
            return SimpleNamespace(
                action=action, option_type="PUT", strike=strike, expiration_date="2026-10-30", quantity=1
            )

        spec = SimpleNamespace(
            underlying="AAPL",
            strategy_type="BULL_PUT_SPREAD",
            premium_direction="CREDIT",
            expiration_date="2026-10-30",
            max_loss_dollars=250.0,
            legs=[leg("BUY", 232.5), leg("SELL", 237.5)],
        )
        good_quotes = {"AAPL261030P00232500": 1.0, "AAPL261030P00237500": 3.0}
        broker = FakeBroker()
        # The real 2026-08-21 incident's own broker-side rejection text.
        broker.fail_preview = PreviewRejectedError(
            "whatIfOrder warning: Rejected by System: Guaranteed-to-Lose combination orders are not allowed"
        )
        summary = ExecutorRunSummary(run_started_at="2026-08-20T00:00:00+00:00", run_date="2026-08-20")
        async with session_maker() as session:
            book = await session.get(BookModel, "B30")
            playbook = (
                (await session.execute(select(PlaybookDefinitionModel).filter_by(id="aapl_earnings_condor_v1")))
                .scalars()
                .one()
                .to_schema()
            )
            with patch.object(executor_mod, "fetch_options_latest_quotes", return_value=good_quotes):
                ok = await _try_place_entry(session, broker, book, spec, playbook, summary)
            await session.commit()
        assert ok is True  # a skip, not an order-path abort
        assert broker.placed == []
        (event,) = await _audits(session_maker, "ENTRY_PREVIEW_REFUSED")
        assert "Guaranteed-to-Lose" in event.payload["reason"]

    @pytest.mark.asyncio
    async def test_preview_gate_runs_before_any_order_reaches_the_broker(self, session_maker):
        # Non-regression: the gate must not degrade to a no-op — a normal
        # night's entries all get previewed before they're placed.
        broker = FakeBroker()
        await _run(session_maker, broker)
        assert broker.previewed  # at least one candidate cleared preview
        assert len(broker.previewed) >= len(broker.placed)


def _tail_put_pos(pos_id: str, expiry_iso: str) -> PositionModel:
    return PositionModel(
        id=pos_id,
        underlying="XSP",
        strategy_type="LONG_PUT",
        execution_mode="PAPER",
        legs=[
            {
                "option_type": "PUT",
                "direction": "LONG",
                "strike": 610.0,
                "expiration": expiry_iso,
                "delta": -0.10,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            }
        ],
        entry_date="2026-08-01",
        expiration_date=expiry_iso,
        entry_premium=3.0,
        premium_direction="DEBIT",
        current_value_per_share=3.0,  # flat — no profit-take / stop-loss P1
        contracts=1,
        max_profit=607.0,
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
        book_id="B32",
        playbook_id="xsp_tail_put_v1",
        playbook_version="1.0",
    )


class TestPlaybookDedup:
    @pytest.mark.asyncio
    async def test_b32_holds_one_put_in_steady_state(self, session_maker):
        # Audit II R2 (#411): two slots exist for the roll-night overlap
        # ONLY. Without dedup, the night after the first put fills a second
        # lot passes every gate and the sleeve settles at 2× bleed.
        expiry = (market_today() + datetime.timedelta(days=75)).isoformat()
        pos = _tail_put_pos("pos_put1", expiry)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        async with session_maker() as session:
            session.add(pos)
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{datetime.date.fromisoformat(expiry):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=1.0, avg_cost=300.0, occ_symbol=occ)
        ]
        summary = await _run(session_maker, broker)
        assert not any("B32" in r for r in summary.entries_placed)
        assert any(b.book_id == "B32" and "dedup" in b.reason for b in summary.entries_blocked)
        assert await _audits(session_maker, "ENTRY_BLOCKED_PLAYBOOK_DEDUP")

    @pytest.mark.asyncio
    async def test_replacement_stages_in_the_exit_window(self, session_maker):
        # The roll night itself: the open put is at/inside 30 DTE, its time
        # exit fires, and the replacement must stage the same night — that
        # overlap IS what the second slot is for (#351).
        expiry = (market_today() + datetime.timedelta(days=20)).isoformat()
        pos = _tail_put_pos("pos_put2", expiry)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        async with session_maker() as session:
            session.add(pos)
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{datetime.date.fromisoformat(expiry):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=1.0, avg_cost=300.0, occ_symbol=occ)
        ]
        summary = await _run(session_maker, broker)
        assert any("B32" in r for r in summary.closes_placed)  # the time-exit roll
        assert any("B32" in r for r in summary.entries_placed)  # …and its replacement
        assert not await _audits(session_maker, "ENTRY_BLOCKED_PLAYBOOK_DEDUP")


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
            premium_direction="CREDIT",
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
    async def test_tp_intent_row_is_committed_before_placement(self, session_maker):
        # Audit II R2 (#409): place_spread sends the entry AND its GTC
        # profit-taker; a crash before the post-placement commit left the GTC
        # child resting at the broker with no DB row — never adopted, never
        # cancelled, a double-close when it fills. Both intent rows must
        # already be committed when placeOrder goes out.
        from types import SimpleNamespace

        from backend.executor import ExecutorRunSummary, _try_place_entry

        def leg(action: str, strike: float) -> SimpleNamespace:
            return SimpleNamespace(
                action=action, option_type="PUT", strike=strike, expiration_date="2026-10-30", quantity=1
            )

        spec = SimpleNamespace(
            underlying="AAPL",
            strategy_type="BULL_PUT_SPREAD",
            premium_direction="CREDIT",
            expiration_date="2026-10-30",
            max_loss_dollars=250.0,
            legs=[leg("BUY", 232.5), leg("SELL", 237.5)],
        )
        broker = FakeBroker()
        broker.fail_place = KeyboardInterrupt("process died mid-placement")
        summary = ExecutorRunSummary(run_started_at="2026-08-20T00:00:00+00:00", run_date="2026-08-20")
        with pytest.raises(KeyboardInterrupt):
            async with session_maker() as session:
                book = await session.get(BookModel, "B30")
                playbook = (
                    (await session.execute(select(PlaybookDefinitionModel).filter_by(id="aapl_earnings_condor_v1")))
                    .scalars()
                    .one()
                    .to_schema()
                )
                with patch.object(executor_mod, "fetch_options_latest_quotes", side_effect=_priced):
                    await _try_place_entry(session, broker, book, spec, playbook, summary)
        async with session_maker() as session:
            orders = (await session.execute(select(OrderModel))).scalars().all()
        by_ref = {o.order_ref: o for o in orders}
        (entry_ref,) = [r for r in by_ref if r.endswith(":open")]
        tp = by_ref[f"{entry_ref}:tp"]  # the crash left a traceable record
        assert tp.status == "STAGED"
        assert by_ref[entry_ref].status == "STAGED"

    @pytest.mark.asyncio
    async def test_duplicate_window_uses_run_date_not_market_today_mid_run(self, session_maker, monkeypatch):
        # #545 L1: the duplicate-order window was recomputed at entry-staging
        # time via market_evening_window_start(market_today()) instead of
        # threaded from summary.run_date — a run crossing ET midnight would
        # have market_today() answer TOMORROW, computing a FUTURE window
        # start; a non-STAGED order from tonight's run then falls outside
        # it, and duplicate detection goes inert for the rest of the run.
        # Simulate the crossing directly: market_today() answers tomorrow
        # while the run's own date (summary.run_date) is still tonight.
        from types import SimpleNamespace

        from backend.dates import market_evening_window_start
        from backend.executor import ExecutorRunSummary, _try_place_entry

        def leg(action: str, strike: float) -> SimpleNamespace:
            return SimpleNamespace(
                action=action, option_type="PUT", strike=strike, expiration_date="2026-10-30", quantity=1
            )

        spec = SimpleNamespace(
            underlying="AAPL",
            strategy_type="BULL_PUT_SPREAD",
            premium_direction="CREDIT",
            expiration_date="2026-10-30",
            max_loss_dollars=250.0,
            legs=[leg("BUY", 232.5), leg("SELL", 237.5)],
        )
        run_date = "2026-08-20"
        tomorrow = "2026-08-21"
        # Already transitioned out of STAGED this run — only non-STAGED rows
        # exercise the window comparison (a STAGED row is always in-window).
        submitted_at = market_evening_window_start(datetime.date.fromisoformat(run_date))
        async with session_maker() as session:
            session.add(
                OrderModel(
                    id="o_existing",
                    book_id="B30",
                    position_id=None,
                    order_ref="basis:B30:o_existing:open",
                    ib_order_id=1,
                    ib_perm_id=100,
                    action="OPEN",
                    combo_legs={
                        "legs": [
                            {"occ": "AAPL261030P00232500", "direction": "LONG"},
                            {"occ": "AAPL261030P00237500", "direction": "SHORT"},
                        ],
                        "quantity": 1,
                    },
                    order_type="LIMIT",
                    limit_price=-1.0,
                    decision_midpoint=-1.0,
                    status="SUBMITTED",
                    submitted_at=submitted_at,
                    encumbered_risk=0.0,
                )
            )
            await session.commit()

        broker = FakeBroker()
        summary = ExecutorRunSummary(run_started_at=f"{run_date}T23:50:00+00:00", run_date=run_date)
        monkeypatch.setattr(executor_mod, "market_today", lambda: datetime.date.fromisoformat(tomorrow))
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
        assert ok is True
        assert broker.placed == []  # blocked as a duplicate, never reached the broker
        assert any("DUPLICATE_ORDER" in b.reason for b in summary.entries_blocked)

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
    async def test_filled_entry_credits_price_improvement_from_actual_fills(self, session_maker):
        # #666: booking the LIMIT price unconditionally never credits price
        # improvement — the fills ledger has the REAL execution prices. The
        # short 610 put fills at 1.50 (better than the decided credit
        # implies) and the long 605 put at 0.20: net credit 1.30/share, not
        # the 1.20 the order was staged at.
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
        broker.ref_states[f"{ref}:tp"] = RefState.OPEN
        broker.execution_rows = [
            FillInfo(
                exec_id="x_short",
                con_id=1,
                side="SLD",
                quantity=1.0,
                price=1.50,
                order_ref=ref,
                commission=None,
                exec_time="2026-08-22T20:00:00+00:00",
            ),
            FillInfo(
                exec_id="x_long",
                con_id=2,
                side="BOT",
                quantity=1.0,
                price=0.20,
                order_ref=ref,
                commission=None,
                exec_time="2026-08-22T20:00:00+00:00",
            ),
        ]
        broker.position_rows = [
            LegPosition(
                con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol="XSP261218P00610000"
            ),
            LegPosition(
                con_id=2, symbol="XSP", sec_type="OPT", position=1.0, avg_cost=0, occ_symbol="XSP261218P00605000"
            ),
        ]
        await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_o_fill")
            book = await session.get(BookModel, "B01")
        assert pos.entry_premium == 1.30  # fill-derived credit, not the 1.20 limit
        assert pos.premium_direction == "CREDIT"
        assert book.cash_balance == 10000.0 + 130.0  # improvement credited, not just the limit's 120
        assert not await _audits(session_maker, "FILL_PRICE_UNAVAILABLE_LIMIT_FALLBACK")

    @pytest.mark.asyncio
    async def test_filled_entry_falls_back_to_limit_price_when_no_fills_are_on_the_ledger(self, session_maker):
        # #666: a FILLED verdict from reqCompletedOrders whose reqExecutions
        # this same reconcile pass didn't (yet) see must not crash or invent
        # a price — it falls back to the limit and audits that it did.
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
        broker.ref_states[f"{ref}:tp"] = RefState.OPEN
        # No execution_rows at all — the fallback path.
        broker.position_rows = [
            LegPosition(
                con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol="XSP261218P00610000"
            ),
            LegPosition(
                con_id=2, symbol="XSP", sec_type="OPT", position=1.0, avg_cost=0, occ_symbol="XSP261218P00605000"
            ),
        ]
        await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_o_fill")
            book = await session.get(BookModel, "B01")
        assert pos.entry_premium == 1.20  # limit-price fallback, unchanged behavior
        assert book.cash_balance == 10000.0 + 120.0
        fallback_audits = await _audits(session_maker, "FILL_PRICE_UNAVAILABLE_LIMIT_FALLBACK")
        assert len(fallback_audits) == 1
        assert fallback_audits[0].payload["action"] == "OPEN"

    @pytest.mark.asyncio
    async def test_filled_entry_with_one_leg_missing_from_the_fills_ledger_falls_back_to_limit(self, session_maker):
        # #693: reqExecutions can surface fills for SOME of a combo's legs
        # but not all, on the same "the reconcile pass didn't see it yet"
        # race #666 already names for the zero-fills case. A non-empty but
        # INCOMPLETE fills ledger used to compute a plausible-looking net
        # from whatever was captured and book it as fully measured — this
        # order's combo has 2 legs (ORDER_META); only the short leg's fill
        # ever lands.
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
        broker.ref_states[f"{ref}:tp"] = RefState.OPEN
        broker.execution_rows = [
            FillInfo(
                exec_id="x_short_only",
                con_id=1,
                side="SLD",
                quantity=1.0,
                price=1.50,
                order_ref=ref,
                commission=None,
                exec_time="2026-08-22T20:00:00+00:00",
            ),
            # con_id=2 (the long leg) never arrives this reconcile pass.
        ]
        broker.position_rows = [
            LegPosition(
                con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol="XSP261218P00610000"
            ),
            LegPosition(
                con_id=2, symbol="XSP", sec_type="OPT", position=1.0, avg_cost=0, occ_symbol="XSP261218P00605000"
            ),
        ]
        await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_o_fill")
            book = await session.get(BookModel, "B01")
        assert pos.entry_premium == 1.20  # limit-price fallback, NOT a number derived from the 1 captured leg
        assert book.cash_balance == 10000.0 + 120.0
        fallback_audits = await _audits(session_maker, "FILL_PRICE_UNAVAILABLE_LIMIT_FALLBACK")
        assert len(fallback_audits) == 1
        assert fallback_audits[0].payload["action"] == "OPEN"

    @pytest.mark.asyncio
    async def test_filled_close_with_short_quantity_on_a_leg_falls_back_to_limit(self, session_maker):
        # #693: both legs are represented in the fills ledger, but one leg's
        # captured quantity (0.5) is short of the order's full intended size
        # (1 contract) — a partial capture on quantity, not just leg count.
        ref = "basis:B01:o_cls:close"
        async with session_maker() as session:
            session.add(
                PositionModel(
                    id="pos_c2",
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
            close.position_id = "pos_c2"
            close.limit_price = -0.30
            close.encumbered_risk = 0.0
            session.add(close)
            await session.commit()
        broker = FakeBroker()
        broker.ref_states[ref] = RefState.FILLED
        broker.execution_rows = [
            FillInfo(
                exec_id="x_close_short",
                con_id=1,
                side="BOT",
                quantity=1.0,
                price=0.20,
                order_ref=ref,
                commission=None,
                exec_time="2026-08-22T20:00:00+00:00",
            ),
            FillInfo(
                exec_id="x_close_long_partial",
                con_id=2,
                side="SLD",
                quantity=0.5,  # short of the order's 1-contract size
                price=0.05,
                order_ref=ref,
                commission=None,
                exec_time="2026-08-22T20:00:00+00:00",
            ),
        ]
        await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_c2")
            book = await session.get(BookModel, "B01")
        assert pos.current_value_per_share == 0.30  # limit-price fallback, NOT the partial-fill-derived 0.15
        assert book.cash_balance == 10000.0 - 30.0
        fallback_audits = await _audits(session_maker, "FILL_PRICE_UNAVAILABLE_LIMIT_FALLBACK")
        assert len(fallback_audits) == 1
        assert fallback_audits[0].payload["action"] == "CLOSE"

    @pytest.mark.asyncio
    async def test_filled_close_on_a_ratio_expanded_bwb_leg_is_not_wrongly_treated_as_short_a_leg(self, session_maker):
        # #693 fix-forward (#132 interaction): a BWB body stores its ratio
        # expanded into duplicate leg dicts in both PositionModel.legs and a
        # CLOSE order's combo_legs["legs"] ([dict(l) for l in pos.legs]) —
        # 3 real legs (upper, body, lower) show up as 4 raw dicts. IBKR
        # combos carry the body's ratio on ONE conId, not two, so the fills
        # ledger only ever reports 3 distinct con_ids. Counting raw leg
        # dicts as "expected legs" would make this combo permanently look
        # one leg short and force every BWB close onto the limit-price
        # fallback — this asserts the fill-derived net is used instead.
        ref = "basis:B01:o_bwb:close"
        bwb_legs = [
            {
                "option_type": "PUT",
                "direction": "LONG",
                "strike": 620.0,
                "expiration": "2026-12-18",
                "delta": -0.10,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            },
            {
                "option_type": "PUT",
                "direction": "SHORT",
                "strike": 610.0,
                "expiration": "2026-12-18",
                "delta": -0.30,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            },
            {
                "option_type": "PUT",
                "direction": "SHORT",
                "strike": 610.0,
                "expiration": "2026-12-18",
                "delta": -0.30,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            },
            {
                "option_type": "PUT",
                "direction": "LONG",
                "strike": 600.0,
                "expiration": "2026-12-18",
                "delta": -0.05,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            },
        ]
        async with session_maker() as session:
            session.add(
                PositionModel(
                    id="pos_bwb",
                    underlying="XSP",
                    strategy_type="BROKEN_WING_BUTTERFLY",
                    execution_mode="PAPER",
                    legs=bwb_legs,
                    entry_date="2026-08-01",
                    expiration_date="2026-12-18",
                    entry_premium=2.50,
                    premium_direction="CREDIT",
                    current_value_per_share=0.50,
                    contracts=1,
                    max_profit=2.50,
                    max_loss=7.50,
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
            close = _order("o_bwb", "SUBMITTED", ref)
            close.action = "CLOSE"
            close.position_id = "pos_bwb"
            close.limit_price = -0.60
            close.encumbered_risk = 0.0
            close.combo_legs = {"legs": bwb_legs, "quantity": 1, "exit_trigger": "PROFIT_TARGET"}
            session.add(close)
            await session.commit()
        broker = FakeBroker()
        broker.ref_states[ref] = RefState.FILLED
        broker.execution_rows = [
            FillInfo(
                exec_id="x_upper",
                con_id=1,
                side="SLD",
                quantity=1.0,
                price=0.05,
                order_ref=ref,
                commission=None,
                exec_time="2026-08-22T20:00:00+00:00",
            ),
            FillInfo(
                exec_id="x_body",
                con_id=2,
                side="BOT",
                quantity=2.0,  # the body's ratio-2 fill lands on ONE conId
                price=0.30,
                order_ref=ref,
                commission=None,
                exec_time="2026-08-22T20:00:00+00:00",
            ),
            FillInfo(
                exec_id="x_lower",
                con_id=3,
                side="SLD",
                quantity=1.0,
                price=0.05,
                order_ref=ref,
                commission=None,
                exec_time="2026-08-22T20:00:00+00:00",
            ),
        ]
        await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_bwb")
        # fill-derived net = -0.05 (upper SLD) + 0.30*2 (body BOT, ratio 2) - 0.05 (lower SLD) = 0.50,
        # negated for CLOSE's signed-cash-flow convention then abs()'d — same 0.50 either way here.
        assert pos.current_value_per_share == pytest.approx(0.50)
        fallback_audits = await _audits(session_maker, "FILL_PRICE_UNAVAILABLE_LIMIT_FALLBACK")
        assert not fallback_audits  # NOT the permanent-fallback regression this test guards against

    @pytest.mark.asyncio
    async def test_filled_close_with_a_ratio_leg_short_on_quantity_still_falls_back(self, session_maker):
        # #693: a ratio-2 leg (the BWB body) can fill on its single conId
        # without filling its FULL ratio-scaled size — the per-conId floor
        # (q >= contracts) alone would pass a body that only filled 1 of
        # its 2 expected contracts, understating the fill-derived net and
        # booking it as measured. This must fall back instead, same as an
        # ordinary leg short on quantity.
        ref = "basis:B01:o_bwb2:close"
        bwb_legs = [
            {
                "option_type": "PUT",
                "direction": "LONG",
                "strike": 620.0,
                "expiration": "2026-12-18",
                "delta": -0.10,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            },
            {
                "option_type": "PUT",
                "direction": "SHORT",
                "strike": 610.0,
                "expiration": "2026-12-18",
                "delta": -0.30,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            },
            {
                "option_type": "PUT",
                "direction": "SHORT",
                "strike": 610.0,
                "expiration": "2026-12-18",
                "delta": -0.30,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            },
            {
                "option_type": "PUT",
                "direction": "LONG",
                "strike": 600.0,
                "expiration": "2026-12-18",
                "delta": -0.05,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            },
        ]
        async with session_maker() as session:
            session.add(
                PositionModel(
                    id="pos_bwb2",
                    underlying="XSP",
                    strategy_type="BROKEN_WING_BUTTERFLY",
                    execution_mode="PAPER",
                    legs=bwb_legs,
                    entry_date="2026-08-01",
                    expiration_date="2026-12-18",
                    entry_premium=2.50,
                    premium_direction="CREDIT",
                    current_value_per_share=0.50,
                    contracts=1,
                    max_profit=2.50,
                    max_loss=7.50,
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
            close = _order("o_bwb2", "SUBMITTED", ref)
            close.action = "CLOSE"
            close.position_id = "pos_bwb2"
            close.limit_price = -0.60
            close.encumbered_risk = 0.0
            close.combo_legs = {"legs": bwb_legs, "quantity": 1, "exit_trigger": "PROFIT_TARGET"}
            session.add(close)
            await session.commit()
        broker = FakeBroker()
        broker.ref_states[ref] = RefState.FILLED
        broker.execution_rows = [
            FillInfo(
                exec_id="x_upper2",
                con_id=1,
                side="SLD",
                quantity=1.0,
                price=0.05,
                order_ref=ref,
                commission=None,
                exec_time="2026-08-22T20:00:00+00:00",
            ),
            FillInfo(
                exec_id="x_body2",
                con_id=2,
                side="BOT",
                quantity=1.0,  # only 1 of the body's 2 expected contracts filled
                price=0.30,
                order_ref=ref,
                commission=None,
                exec_time="2026-08-22T20:00:00+00:00",
            ),
            FillInfo(
                exec_id="x_lower2",
                con_id=3,
                side="SLD",
                quantity=1.0,
                price=0.05,
                order_ref=ref,
                commission=None,
                exec_time="2026-08-22T20:00:00+00:00",
            ),
        ]
        await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_bwb2")
        assert pos.current_value_per_share == 0.60  # limit-price fallback, NOT the understated fill-derived net
        fallback_audits = await _audits(session_maker, "FILL_PRICE_UNAVAILABLE_LIMIT_FALLBACK")
        assert len(fallback_audits) == 1
        assert fallback_audits[0].payload["action"] == "CLOSE"

    @pytest.mark.asyncio
    async def test_filled_close_credits_price_improvement_from_actual_fills(self, session_maker):
        # #666: buying back the credit spread at a BETTER (cheaper) price
        # than the limit asked for must book the CHEAPER real cost, not the
        # limit's more conservative one.
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
            close.limit_price = -0.30  # asked to pay up to 0.30/share
            close.encumbered_risk = 0.0
            session.add(close)
            await session.commit()
        broker = FakeBroker()
        broker.ref_states[ref] = RefState.FILLED
        # Closing the bag SELLS the short leg back and BUYS the long leg
        # back — the mirror of the entry's own sides. Filled cheaper than
        # asked: buy back the short for 0.20 (BOT), sell the long for 0.05
        # (SLD) -> raw fill-derived net (BOT-positive) = 0.20-0.05 = 0.15,
        # negated (#347, close reverses sides) -> -0.15, i.e. paid 0.15/share
        # to close, better than the 0.30 the limit allowed.
        broker.execution_rows = [
            FillInfo(
                exec_id="x_close_short",
                con_id=1,
                side="BOT",
                quantity=1.0,
                price=0.20,
                order_ref=ref,
                commission=None,
                exec_time="2026-08-22T20:00:00+00:00",
            ),
            FillInfo(
                exec_id="x_close_long",
                con_id=2,
                side="SLD",
                quantity=1.0,
                price=0.05,
                order_ref=ref,
                commission=None,
                exec_time="2026-08-22T20:00:00+00:00",
            ),
        ]
        await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_c")
            book = await session.get(BookModel, "B01")
        assert pos.current_value_per_share == pytest.approx(0.15)  # fill-derived exit, not the 0.30 limit
        assert book.cash_balance == pytest.approx(10000.0 - 15.0)  # cheaper buy-back debits less
        assert not await _audits(session_maker, "FILL_PRICE_UNAVAILABLE_LIMIT_FALLBACK")

    @pytest.mark.asyncio
    async def test_close_fill_on_already_closed_position_applies_no_cash(self, session_maker):
        # Audit II (#342): the cash adjustment sat OUTSIDE the OPEN guard — if
        # an operator external-close resolution booked the exit first, the
        # broker fill arriving later re-applied the same cash flow.
        ref = "basis:B01:o_dbl:close"
        async with session_maker() as session:
            session.add(
                PositionModel(
                    id="pos_dbl",
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
                    status="CLOSED",  # a resolution already closed it (and moved the cash)
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
            close = _order("o_dbl", "SUBMITTED", ref)
            close.action = "CLOSE"
            close.position_id = "pos_dbl"
            close.limit_price = -0.30
            close.encumbered_risk = 0.0
            session.add(close)
            await session.commit()
        broker = FakeBroker()
        broker.ref_states[ref] = RefState.FILLED
        summary = await _run(session_maker, broker)
        async with session_maker() as session:
            order = await session.get(OrderModel, "o_dbl")
            book = await session.get(BookModel, "B01")
            pms = (
                (await session.execute(select(ClosurePostMortemModel).filter_by(position_id="pos_dbl"))).scalars().all()
            )
        assert order.status == "FILLED"  # the order itself still settles
        assert book.cash_balance == 10000.0  # cash NOT applied a second time
        assert pms == []  # no duplicate expectancy row either
        assert await _audits(session_maker, "CLOSE_FILL_ON_NON_OPEN")
        assert any("CLOSE FILL ON NON-OPEN" in n for n in summary.notes)

    @pytest.mark.asyncio
    async def test_close_fill_race_with_a_concurrent_external_close_applies_no_cash(self, session_maker, monkeypatch):
        # #463 (Audit II R3 F3): the `pos.status == "OPEN"` check here reads
        # the (possibly stale) identity map — an operator's external-close
        # resolution landing via a DIFFERENT session in the gap between this
        # fetch and the write must not also get this fill's cash applied.
        # Simulate the gap deterministically: intercept the FIRST read this
        # call makes (the book fetch, before #466's order-status stamp opens
        # a write transaction on this session — racing a second SQLite
        # writer after that point deadlocks rather than interleaving) and,
        # from inside it, close the position via a second session before
        # returning control. The conditional UPDATE, not the fetched
        # pos.status, must be what stops the double-book.
        ref = "basis:B01:o_race:close"
        async with session_maker() as session:
            session.add(
                PositionModel(
                    id="pos_race",
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
            close = _order("o_race", "SUBMITTED", ref)
            close.action = "CLOSE"
            close.position_id = "pos_race"
            close.limit_price = -0.30
            close.encumbered_risk = 0.0
            session.add(close)
            await session.commit()

        from backend.executor import ExecutorRunSummary, _order_to_position

        original_get = AsyncSession.get
        triggered = False

        async def racing_get(self_session, model, ident, *a, **kw):
            nonlocal triggered
            if not triggered and model is BookModel:
                triggered = True
                monkeypatch.setattr(AsyncSession, "get", original_get)
                async with session_maker() as other:
                    other_pos = await other.get(PositionModel, "pos_race")
                    other_pos.status = "CLOSED"
                    await other.commit()
            return await original_get(self_session, model, ident, *a, **kw)

        monkeypatch.setattr(AsyncSession, "get", racing_get)
        summary = ExecutorRunSummary(run_started_at="2026-08-20T00:00:00+00:00", run_date="2026-08-20")
        async with session_maker() as session:
            order = await session.get(OrderModel, "o_race")
            await _order_to_position(session, order, summary)
            await session.commit()

        async with session_maker() as session:
            order = await session.get(OrderModel, "o_race")
            book = await session.get(BookModel, "B01")
            pms = (
                (await session.execute(select(ClosurePostMortemModel).filter_by(position_id="pos_race")))
                .scalars()
                .all()
            )
        assert order.status == "FILLED"  # the order itself still settles
        assert book.cash_balance == 10000.0  # cash NOT applied — the race lost
        assert pms == []  # no duplicate expectancy row
        assert await _audits(session_maker, "CLOSE_FILL_ON_NON_OPEN")

    @pytest.mark.asyncio
    async def test_sync_skips_a_verdict_when_a_console_terminalization_wins_the_race(self, session_maker, monkeypatch):
        # #466 (Audit II R3 F7): the sync loads its pending snapshot, computes
        # a verdict from a single broker report, and used to stamp that
        # verdict onto the row unconditionally. An operator's
        # acknowledge_cancelled terminalization (record_external_close)
        # landing on THIS row via a DIFFERENT session, in the gap between the
        # snapshot and the sync's own write, must win — not be silently
        # overwritten by the sync's own (now stale) verdict, which would
        # resurrect a pending latch on an already-terminalized order and
        # contradict the terminalization's own audit row. Intercept the
        # sync's first order-status UPDATE and, from inside it, terminalize
        # the row via a second session before the real UPDATE runs.
        from sqlalchemy.sql.dml import Update

        ref = "basis:B01:o_stale:open"
        async with session_maker() as session:
            session.add(_order("o_stale", "STAGED", ref))
            # #650: a real, same-day reconciliation baseline — without one,
            # the missing-baseline case holds this row instead of ever
            # reaching the status UPDATE this test intercepts.
            session.add(
                ReconciliationRunModel(
                    run_at=f"{_FROZEN_TODAY.isoformat()}T22:50:00+00:00",
                    broker_snapshot={},
                    books_expected={},
                    result="CLEAN",
                    drift_details=None,
                )
            )
            await session.commit()

        original_execute = AsyncSession.execute
        triggered = False

        async def racing_execute(self_session, statement, *a, **kw):
            nonlocal triggered
            if not triggered and isinstance(statement, Update) and statement.table.name == "orders":
                triggered = True
                monkeypatch.setattr(AsyncSession, "execute", original_execute)
                async with session_maker() as other:
                    other_order = await other.get(OrderModel, "o_stale")
                    other_order.status = "CANCELLED"
                    other_order.completed_at = "2026-08-20T00:00:00+00:00"
                    await other.commit()
            return await original_execute(self_session, statement, *a, **kw)

        monkeypatch.setattr(AsyncSession, "execute", racing_execute)
        broker = FakeBroker()  # ref UNKNOWN at broker
        summary = await _run(session_maker, broker)

        assert ref not in summary.intents_expired  # the sync's own verdict never landed
        async with session_maker() as session:
            order = await session.get(OrderModel, "o_stale")
        assert order.status == "CANCELLED"  # exactly as the concurrent terminalization left it
        assert order.completed_at == "2026-08-20T00:00:00+00:00"
        assert await _audits(session_maker, "ORDER_SYNC_SKIPPED_CONCURRENT_WRITE")

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
            FillInfo(
                exec_id="e_part1",
                con_id=1,
                side="SLD",
                quantity=1.0,
                price=1.85,
                order_ref=ref,
                commission=1.0,
                exec_time="2024-01-01T00:00:00+00:00",
            )
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
            # #650: a real, same-day reconciliation baseline — otherwise the
            # missing-baseline case (its own test below) holds this instead
            # of terminalizing it.
            session.add(
                ReconciliationRunModel(
                    run_at=f"{_FROZEN_TODAY.isoformat()}T22:50:00+00:00",
                    broker_snapshot={},
                    books_expected={},
                    result="CLEAN",
                    drift_details=None,
                )
            )
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

    @pytest.mark.asyncio
    async def test_broker_rejection_is_stamped_rejected_not_cancelled(self, session_maker):
        # #627: the sync used to collapse every non-filled terminal order to
        # CANCELLED regardless of WHY — a genuine broker rejection (the
        # sign-inverted incident's own shape) is now distinguishable, using
        # the completedStatus text the reconcile()-level capture shim
        # recovers.
        ref = "basis:B01:o_rej:open"
        async with session_maker() as session:
            session.add(_order("o_rej", "SUBMITTED", ref))
            await session.commit()
        broker = FakeBroker()
        broker.ref_states[ref] = RefState.CANCELLED
        broker.rejection_reasons[ref] = "Rejected by System: Guaranteed-to-Lose combination orders are not allowed"
        await _run(session_maker, broker)
        async with session_maker() as session:
            order = await session.get(OrderModel, "o_rej")
        assert order.status == "REJECTED"
        (event,) = await _audits(session_maker, "ORDER_REJECTED")
        assert event.payload["reason"] == "Rejected by System: Guaranteed-to-Lose combination orders are not allowed"
        assert event.payload["order_ref"] == ref
        # A genuine rejection is urgent — unlike routine expiry, it must
        # reach the operator via the nightly urgent push, not just the trail.
        assert not await _audits(session_maker, "ORDER_EXPIRED_AT_BROKER")

    @pytest.mark.asyncio
    async def test_restore_gap_holds_unknown_staged_intent_instead_of_expiring(self, session_maker):
        # #542: after restoring a backup ≥2 market days old,
        # reqCompletedOrders/reqExecutions cannot see a fill from the gap —
        # a restored pending row that actually filled in the gap reads
        # UNKNOWN with no local FillModel evidence either (the restore lost
        # it too). Terminalizing (INTENT_EXPIRED) would bury real broker
        # state on evidence the restore destroyed.
        async with session_maker() as session:
            session.add(
                ReconciliationRunModel(
                    run_at="2026-08-10T22:50:00+00:00",  # well over a week ago
                    broker_snapshot={},
                    books_expected={},
                    result="CLEAN",
                    drift_details=None,
                )
            )
            ref = "basis:B01:o_gap_staged:open"
            session.add(_order("o_gap_staged", "STAGED", ref))
            await session.commit()
        broker = FakeBroker()  # ref UNKNOWN at broker
        summary = await _run(session_maker, broker)
        assert ref not in summary.intents_expired
        assert ref in summary.restore_gap_held
        assert not await _audits(session_maker, "INTENT_EXPIRED")
        assert await _audits(session_maker, "RESTORE_GAP_UNKNOWN_HELD")
        async with session_maker() as session:
            order = await session.get(OrderModel, "o_gap_staged")
        assert order.status == "STAGED"  # untouched, not CANCELLED
        assert any("RESTORE GAP" in n for n in summary.notes)

    @pytest.mark.asyncio
    async def test_restore_gap_holds_unknown_submitted_order_instead_of_losing_it(self, session_maker):
        # #542: the general UNKNOWN arm (not just the STAGED intent one) has
        # the same restore-gap blind spot — an entry that actually filled at
        # the broker during the gap must not be stamped ORDER_LOST_AT_BROKER
        # and have its encumbrance released while the position never got
        # booked.
        ref = "basis:B01:o_gap_submitted:open"
        async with session_maker() as session:
            session.add(
                ReconciliationRunModel(
                    run_at="2026-08-10T22:50:00+00:00",
                    broker_snapshot={},
                    books_expected={},
                    result="CLEAN",
                    drift_details=None,
                )
            )
            session.add(_order("o_gap_submitted", "SUBMITTED", ref))
            await session.commit()
        broker = FakeBroker()  # ref UNKNOWN at broker, no fills recorded
        summary = await _run(session_maker, broker)
        assert ref in summary.restore_gap_held
        assert not await _audits(session_maker, "ORDER_LOST_AT_BROKER")
        assert await _audits(session_maker, "RESTORE_GAP_UNKNOWN_HELD")
        async with session_maker() as session:
            order = await session.get(OrderModel, "o_gap_submitted")
        assert order.status == "SUBMITTED"  # untouched
        assert order.encumbered_risk == 380.0  # encumbrance never released

    @pytest.mark.asyncio
    async def test_never_reconciled_database_holds_unknowns_instead_of_reading_as_zero_gap(self, session_maker):
        # #650: no prior ReconciliationRunModel row at all (an empty/fresh
        # database, or a restore of a pre-reconciliation backup) used to
        # compute gap_trading_days=0 — "no gap," the most-dangerous-possible
        # default — and terminalize UNKNOWN verdicts on the exact run where
        # trust in the ledger is lowest. It must hold exactly like a real
        # multi-day gap does, not behave like a clean same-day run.
        ref = "basis:B01:o_never_reconciled:open"
        async with session_maker() as session:
            session.add(_order("o_never_reconciled", "STAGED", ref))
            await session.commit()
        broker = FakeBroker()  # ref UNKNOWN at broker
        summary = await _run(session_maker, broker)
        assert ref not in summary.intents_expired
        assert ref in summary.restore_gap_held
        assert not await _audits(session_maker, "INTENT_EXPIRED")
        assert await _audits(session_maker, "RESTORE_GAP_UNKNOWN_HELD")
        assert await _audits(session_maker, "NO_RECONCILIATION_BASELINE")
        async with session_maker() as session:
            order = await session.get(OrderModel, "o_never_reconciled")
        assert order.status == "STAGED"  # untouched, not CANCELLED
        assert any("NO RECONCILIATION BASELINE" in n for n in summary.notes)

    @pytest.mark.asyncio
    async def test_small_gap_still_terminalizes_unknowns_as_before(self, session_maker):
        # #542: the hold is gap-gated — a 1-trading-day gap (an ordinary
        # single missed night, not a restore) must behave exactly as before.
        ref = "basis:B01:o_small_gap:open"
        async with session_maker() as session:
            session.add(
                ReconciliationRunModel(
                    run_at=(market_today() - datetime.timedelta(days=1)).isoformat() + "T22:50:00+00:00",
                    broker_snapshot={},
                    books_expected={},
                    result="CLEAN",
                    drift_details=None,
                )
            )
            session.add(_order("o_small_gap", "STAGED", ref))
            await session.commit()
        broker = FakeBroker()  # ref UNKNOWN at broker
        summary = await _run(session_maker, broker)
        assert ref in summary.intents_expired
        assert ref not in summary.restore_gap_held
        assert await _audits(session_maker, "INTENT_EXPIRED")
        assert not await _audits(session_maker, "RESTORE_GAP_UNKNOWN_HELD")
        async with session_maker() as session:
            order = await session.get(OrderModel, "o_small_gap")
        assert order.status == "CANCELLED"

    @pytest.mark.asyncio
    async def test_held_row_resolves_normally_once_a_run_lands_in_window(self, session_maker):
        # #542: terminalization resumes once a run occurs within the
        # 1-trading-day window — the held row isn't stuck forever, it just
        # waits for a run whose gap is small enough to trust an UNKNOWN
        # verdict again.
        ref = "basis:B01:o_gap_resolve:open"
        async with session_maker() as session:
            session.add(
                ReconciliationRunModel(
                    run_at="2026-08-10T22:50:00+00:00",
                    broker_snapshot={},
                    books_expected={},
                    result="CLEAN",
                    drift_details=None,
                )
            )
            session.add(_order("o_gap_resolve", "STAGED", ref))
            await session.commit()
        broker1 = FakeBroker()  # ref UNKNOWN — first run holds it
        summary1 = await _run(session_maker, broker1)
        assert ref in summary1.restore_gap_held
        async with session_maker() as session:
            order = await session.get(OrderModel, "o_gap_resolve")
        assert order.status == "STAGED"  # still held

        # The first run wrote a fresh ReconciliationRunModel row (today), so
        # the next run's gap collapses back under the 1-trading-day window —
        # the held row is now eligible for a normal verdict again.
        broker2 = FakeBroker()  # still UNKNOWN, no fills — genuinely dead
        summary2 = await _run(session_maker, broker2)
        assert ref not in summary2.restore_gap_held
        assert ref in summary2.intents_expired
        async with session_maker() as session:
            order = await session.get(OrderModel, "o_gap_resolve")
        assert order.status == "CANCELLED"


def _expired_pos(pos_id: str, expiry_iso: str, value: float = 0.10) -> PositionModel:
    return PositionModel(
        id=pos_id,
        underlying="XSP",
        strategy_type="BULL_PUT_SPREAD",
        execution_mode="PAPER",
        # Fresh mark by default (#415): settlement and closes both guard on
        # mark age now; tests exercising staleness override this to None.
        last_priced_at=datetime.datetime.now(datetime.UTC).isoformat(),
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


def _expired_calendar_pos(pos_id: str, front_expiry_iso: str, back_expiry_iso: str) -> PositionModel:
    """A B21-style calendar spread (#691): same strike/option_type, front
    leg SHORT expiring today, back leg LONG expiring later — pos.expiration_date
    is documented as the FRONT leg's date only."""
    return PositionModel(
        id=pos_id,
        underlying="XSP",
        strategy_type="CALENDAR_SPREAD",
        execution_mode="PAPER",
        last_priced_at=datetime.datetime.now(datetime.UTC).isoformat(),
        legs=[
            {
                "option_type": "CALL",
                "direction": "SHORT",
                "strike": 610.0,
                "expiration": front_expiry_iso,
                "delta": -0.5,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            },
            {
                "option_type": "CALL",
                "direction": "LONG",
                "strike": 610.0,
                "expiration": back_expiry_iso,
                "delta": 0.5,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            },
        ],
        entry_date="2026-07-01",
        expiration_date=front_expiry_iso,
        entry_premium=-1.20,
        premium_direction="DEBIT",
        current_value_per_share=1.30,
        contracts=1,
        max_profit=5.0,
        max_loss=1.20,
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
    async def test_calendar_spread_with_a_live_back_leg_is_blocked_not_settled_at_zero(self, session_maker):
        # #691: pos.expiration_date is the FRONT leg's date only — a
        # calendar reaching _settle_expired on its front expiration still
        # has a real, live back-leg contract at the broker.
        # _intrinsic_settlement_value used to price BOTH same-strike legs
        # off the front leg's underlying close, collapsing long/short
        # intrinsic to exactly $0 regardless of the true value and silently
        # discarding the back leg's real remaining worth. It must instead
        # refuse automated settlement and leave the position OPEN for the
        # resolution panel.
        front = (market_today() - datetime.timedelta(days=1)).isoformat()
        back = (market_today() + datetime.timedelta(days=27)).isoformat()
        async with session_maker() as session:
            session.add(_expired_calendar_pos("pos_cal", front, back))
            # Even with a close on record for the front date, the leg
            # mismatch must block BEFORE any intrinsic lookup is attempted.
            session.add(IndexHistoryModel(date=front, symbol="SPY", close=620.0))
            await session.commit()
        broker = FakeBroker()
        summary = await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_cal")
            book = await session.get(BookModel, "B01")
            pms = (
                (await session.execute(select(ClosurePostMortemModel).filter_by(position_id="pos_cal"))).scalars().all()
            )
        assert pos.status == "OPEN"  # not settled — the back leg is still live
        assert pos.current_value_per_share == 1.30  # untouched
        assert book.cash_balance == 10000.0  # no cash moved
        assert pms == []  # no post-mortem for a position that never closed
        assert not await _audits(session_maker, "POSITION_EXPIRED")
        events = await _audits(session_maker, "EXPIRY_SETTLEMENT_BLOCKED_MULTI_EXPIRATION")
        assert len(events) == 1
        assert events[0].payload["position_expiration"] == front
        assert events[0].payload["leg_expirations"] == sorted([front, back])
        assert any("different expiration" in n for n in summary.notes)

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
    async def test_worthless_expiry_settles_at_zero_not_the_residual_mark(self, session_maker):
        # #667: a short 610 put with the underlying closing at 620 on expiry
        # day is OTM — worthless, intrinsic 0 — even though its last evening
        # mark (0.10, the fixture default) still carried residual time
        # value. Settling at the mark instead of 0 would book a small
        # systematic loss on every worthless expiry.
        expiry = (market_today() - datetime.timedelta(days=1)).isoformat()
        async with session_maker() as session:
            session.add(_expired_pos("pos_worthless", expiry))
            session.add(IndexHistoryModel(date=expiry, symbol="SPY", close=620.0))  # XSP proxies SPY (#139/#190)
            await session.commit()
        broker = FakeBroker()
        await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_worthless")
            book = await session.get(BookModel, "B01")
            pm = (
                (await session.execute(select(ClosurePostMortemModel).filter_by(position_id="pos_worthless")))
                .scalars()
                .one()
            )
        assert pos.status == "EXPIRED"
        assert pos.current_value_per_share == 0.0
        assert book.cash_balance == 10000.0  # settling at 0 moves no cash, unlike the residual 0.10 mark would
        assert pm.realized_pnl == 120.0  # the full 1.20 credit realized, nothing paid back
        assert not await _audits(session_maker, "EXPIRY_SETTLED_AT_MARK_FALLBACK")
        settled_event = (await _audits(session_maker, "POSITION_EXPIRED"))[0]
        assert settled_event.payload["settled_at"] == "intrinsic"

    @pytest.mark.asyncio
    async def test_itm_expiry_settles_at_computed_intrinsic(self, session_maker):
        # Underlying closes at 600 against the short 610 put -> ITM by 10.
        expiry = (market_today() - datetime.timedelta(days=1)).isoformat()
        async with session_maker() as session:
            session.add(_expired_pos("pos_itm", expiry))
            session.add(IndexHistoryModel(date=expiry, symbol="SPY", close=600.0))
            await session.commit()
        broker = FakeBroker()
        await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_itm")
            book = await session.get(BookModel, "B01")
            pm = (
                (await session.execute(select(ClosurePostMortemModel).filter_by(position_id="pos_itm"))).scalars().one()
            )
        assert pos.current_value_per_share == 10.0  # intrinsic, not the 0.10 fixture mark
        assert book.cash_balance == 10000.0 - 1000.0  # buy back the 10.0 intrinsic value
        assert pm.outcome == "LOSS"
        assert pm.realized_pnl == -880.0  # (1.20 collected - 10.0 paid) * 100

    @pytest.mark.asyncio
    async def test_expiry_falls_back_to_mark_when_the_underlying_close_is_unavailable(self, session_maker):
        # AAPL is outside the ten index_history-tracked symbols — no proxy,
        # no row for this date. Falls back to the last mark, audited.
        expiry = (market_today() - datetime.timedelta(days=1)).isoformat()
        async with session_maker() as session:
            pos = _expired_pos("pos_noidx", expiry)
            pos.underlying = "AAPL"
            session.add(pos)
            await session.commit()
        broker = FakeBroker()
        summary = await _run(session_maker, broker)
        async with session_maker() as session:
            pos2 = await session.get(PositionModel, "pos_noidx")
            book = await session.get(BookModel, "B01")
        assert pos2.status == "EXPIRED"
        assert pos2.current_value_per_share == 0.10  # the fixture's last mark, unchanged fallback behavior
        assert book.cash_balance == 10000.0 - 10.0
        assert await _audits(session_maker, "EXPIRY_SETTLED_AT_MARK_FALLBACK")
        settled_event = (await _audits(session_maker, "POSITION_EXPIRED"))[0]
        assert settled_event.payload["settled_at"] == "mark_fallback"
        assert any("1 at last-mark fallback" in n for n in summary.notes)

    @pytest.mark.asyncio
    async def test_same_night_expiry_settles_at_intrinsic_using_the_just_persisted_close(self, session_maker):
        # #692/#683: the ROUTINE case is a position expiring TODAY — the
        # executor runs the evening of expiration day. Nothing pre-seeds
        # IndexHistoryModel here (unlike the worthless/ITM tests above,
        # which use expiration_date = yesterday and pre-seed directly,
        # sidestepping the real call-order dependency entirely). Instead
        # this drives persist_index_history's OWN fetch through the real
        # pipeline — proving _settle_expired sees today's close that the
        # SAME run just wrote, not a stale/missing row.
        # _FROZEN_TODAY directly, not market_today() — the module-level
        # constant is what executor_mod.market_today is pinned to
        # (object-patched, reliable); avoids depending on the string-target
        # monkeypatch of this test module's own market_today reference for
        # an EXACT-equality date (the yesterday-relative fixtures elsewhere
        # in this class only ever need same-or-before, which tolerates it).
        today_iso = _FROZEN_TODAY.isoformat()
        async with session_maker() as session:
            session.add(_expired_pos("pos_sameday", today_iso))  # short 610 put
            await session.commit()

        def _index_closes(symbol, days):
            return [(today_iso, 620.0)] if symbol == "SPY" else None

        broker = FakeBroker()
        await _run(session_maker, broker, index_closes=_index_closes)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_sameday")
            book = await session.get(BookModel, "B01")
        assert pos.status == "EXPIRED"
        assert pos.current_value_per_share == 0.0  # intrinsic (OTM put), not the 0.10 fixture mark
        assert book.cash_balance == 10000.0  # settling at 0 moves no cash
        assert not await _audits(session_maker, "EXPIRY_SETTLED_AT_MARK_FALLBACK")
        settled_event = (await _audits(session_maker, "POSITION_EXPIRED"))[0]
        assert settled_event.payload["settled_at"] == "intrinsic"
        async with session_maker() as session:
            row = await session.get(IndexHistoryModel, (today_iso, "SPY"))
        assert row is not None and row.close == 620.0  # persist_index_history really ran first

    @pytest.mark.asyncio
    async def test_expiry_settlement_blocked_while_a_partial_is_latched(self, session_maker):
        # Audit II (#348): a PARTIAL order means the true filled size is
        # unknown — settling full size fabricates cash, and cancelling the
        # latch erases the flag the human resolves by (#283).
        expiry = (market_today() - datetime.timedelta(days=1)).isoformat()
        async with session_maker() as session:
            pos = _expired_pos("pos_pexp", expiry)
            session.add(pos)
            part = _order("o_pexp", "PARTIAL", "basis:B01:o_pexp:close")
            part.action = "CLOSE"
            part.position_id = "pos_pexp"
            part.limit_price = -0.10
            part.encumbered_risk = 0.0
            session.add(part)
            await session.commit()
        broker = FakeBroker()
        summary = await _run(session_maker, broker)
        async with session_maker() as session:
            pos2 = await session.get(PositionModel, "pos_pexp")
            part2 = await session.get(OrderModel, "o_pexp")
            book = await session.get(BookModel, "B01")
            pms = (
                (await session.execute(select(ClosurePostMortemModel).filter_by(position_id="pos_pexp")))
                .scalars()
                .all()
            )
        assert pos2.status == "OPEN"  # NOT settled
        assert part2.status == "PARTIAL"  # latch intact
        assert book.cash_balance == 10000.0  # no fabricated cash
        assert pms == []
        assert await _audits(session_maker, "EXPIRY_SETTLEMENT_BLOCKED_PARTIAL")
        assert any("EXPIRY SETTLEMENT BLOCKED" in n for n in summary.notes)

    @pytest.mark.asyncio
    async def test_expiry_settlement_blocked_after_a_resolved_partial(self, session_maker):
        # Audit II R3 (#469): a PARTIAL row terminalized via the resolution
        # panel no longer trips the PARTIAL-row guard — but the true filled
        # size is STILL unknown, and this is exactly the night the
        # PARTIAL_DRIFT halt goes reconciliation-neutral. The audit trail of
        # the resolution must keep blocking full-size settlement.
        expiry = (market_today() - datetime.timedelta(days=1)).isoformat()
        async with session_maker() as session:
            pos = _expired_pos("pos_rpexp", expiry)
            session.add(pos)
            part = _order("o_rpexp", "CANCELLED", "basis:B01:o_rpexp:close")
            part.action = "CLOSE"
            part.position_id = "pos_rpexp"
            part.limit_price = -0.10
            part.encumbered_risk = 0.0
            session.add(part)
            session.add(
                AuditEventModel(
                    run_at="t0",
                    book_id="B01",
                    event_type="RESOLUTION_PARTIAL_TERMINALIZED",
                    actor="resolution",
                    payload={"order_ref": "basis:B01:o_rpexp:close", "released_encumbrance": 0.0, "reason": "r"},
                )
            )
            await session.commit()
        broker = FakeBroker()
        summary = await _run(session_maker, broker)
        async with session_maker() as session:
            pos2 = await session.get(PositionModel, "pos_rpexp")
            book = await session.get(BookModel, "B01")
        assert pos2.status == "OPEN"  # NOT settled — external close is the only path
        assert book.cash_balance == 10000.0  # no fabricated cash
        assert await _audits(session_maker, "EXPIRY_SETTLEMENT_BLOCKED_PARTIAL_HISTORY")
        assert any("resolved-PARTIAL history" in n for n in summary.notes)

    @pytest.mark.asyncio
    async def test_expiry_settlement_blocked_on_a_stale_mark(self, session_maker):
        # Audit II R2 (#415): after a missed night the "last mark" can be
        # days old — booking it fabricates cash off a price the market left
        # long ago. Block; the human settles via the resolution panel.
        expiry = (market_today() - datetime.timedelta(days=1)).isoformat()
        async with session_maker() as session:
            pos = _expired_pos("pos_stale_exp", expiry)
            pos.last_priced_at = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=4)).isoformat()
            session.add(pos)
            await session.commit()
        broker = FakeBroker()
        summary = await _run(session_maker, broker)
        async with session_maker() as session:
            pos2 = await session.get(PositionModel, "pos_stale_exp")
            book = await session.get(BookModel, "B01")
        assert pos2.status == "OPEN"  # NOT settled
        assert book.cash_balance == 10000.0  # no fabricated cash
        assert await _audits(session_maker, "EXPIRY_SETTLEMENT_BLOCKED_STALE_MARK")
        assert any("mark is stale" in n for n in summary.notes)

    @pytest.mark.asyncio
    async def test_expiry_settlement_blocked_on_a_naive_mark_timestamp_not_crashed(self, session_maker):
        # #545 L4: a naive (tz-less) last_priced_at makes the aware-minus-
        # naive subtraction raise TypeError, not ValueError — uncaught, that
        # crashed the whole run over one bad row (fail-loud, but a whole
        # night lost). It must read as stale instead, exactly like an
        # unparseable timestamp.
        expiry = (market_today() - datetime.timedelta(days=1)).isoformat()
        async with session_maker() as session:
            pos = _expired_pos("pos_naive_mark", expiry)
            pos.last_priced_at = "2026-08-14T12:00:00"  # no tzinfo
            session.add(pos)
            await session.commit()
        broker = FakeBroker()
        await _run(session_maker, broker)
        async with session_maker() as session:
            pos2 = await session.get(PositionModel, "pos_naive_mark")
            book = await session.get(BookModel, "B01")
        assert pos2.status == "OPEN"  # NOT settled, run did not crash
        assert book.cash_balance == 10000.0

    @pytest.mark.asyncio
    async def test_thanksgiving_friday_mark_settles_not_stale(self, session_maker):
        # #535: Thanksgiving Thu 2026-11-26 is a holiday (heartbeat-only, no
        # pricing run). A position expiring Fri 2026-11-27 settles off
        # Wednesday evening's mark — the last TRADING evening, legitimately
        # 2 calendar days but only 1 TRADING day old. The wall-clock 30h
        # guard used to false-block this every time; the session-aware guard
        # must not.
        from backend.executor import ExecutorRunSummary, _settle_expired

        async with session_maker() as session:
            pos = _expired_pos("pos_thanksgiving", "2026-11-27")
            pos.last_priced_at = "2026-11-25T22:45:00+00:00"  # Wednesday evening
            session.add(pos)
            await session.commit()

        summary = ExecutorRunSummary(run_started_at="2026-11-27T22:45:00+00:00", run_date="2026-11-27")
        async with session_maker() as session:
            await _settle_expired(session, summary)

        async with session_maker() as session:
            pos2 = await session.get(PositionModel, "pos_thanksgiving")
            book = await session.get(BookModel, "B01")
        assert pos2.status == "EXPIRED"  # settled, not false-blocked
        assert book.cash_balance != 10000.0  # cash actually moved
        assert not await _audits(session_maker, "EXPIRY_SETTLEMENT_BLOCKED_STALE_MARK")

    @pytest.mark.asyncio
    async def test_two_trading_days_old_mark_still_blocks(self, session_maker):
        # #535: session-awareness fixes the false positive around holidays —
        # it must not defang the real guard. A mark 2 trading days old (a
        # genuinely missed pricing night) still blocks settlement.
        from backend.executor import ExecutorRunSummary, _settle_expired

        async with session_maker() as session:
            pos = _expired_pos("pos_genuinely_stale", "2026-11-27")
            # Tuesday evening: Wed and Fri are both trading days (Thu is the
            # Thanksgiving holiday) — 2 trading days back from Friday.
            pos.last_priced_at = "2026-11-24T21:45:00+00:00"
            session.add(pos)
            await session.commit()

        summary = ExecutorRunSummary(run_started_at="2026-11-27T22:45:00+00:00", run_date="2026-11-27")
        async with session_maker() as session:
            await _settle_expired(session, summary)

        async with session_maker() as session:
            pos2 = await session.get(PositionModel, "pos_genuinely_stale")
            book = await session.get(BookModel, "B01")
        assert pos2.status == "OPEN"  # NOT settled
        assert book.cash_balance == 10000.0  # no fabricated cash
        assert await _audits(session_maker, "EXPIRY_SETTLEMENT_BLOCKED_STALE_MARK")

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

    @pytest.mark.asyncio
    async def test_settlement_skips_a_position_closed_concurrently(self, session_maker, monkeypatch):
        # #463 (Audit II R3 F3): `rows` below is a run-start OPEN snapshot —
        # a position an operator external-closes (a different session, e.g.
        # the console) in the gap before this loop reaches it must not also
        # settle here. Simulate that gap deterministically: intercept the
        # first query this call makes (the OPEN snapshot itself) and, from
        # inside it, close the position via a second session before this
        # function ever looks at it. The conditional UPDATE, not the
        # in-memory pos.status, must be what stops the double-book.
        from backend.executor import ExecutorRunSummary, _settle_expired

        expiry = (market_today() - datetime.timedelta(days=1)).isoformat()
        async with session_maker() as session:
            session.add(_expired_pos("pos_exp_race", expiry))
            await session.commit()

        original_execute = AsyncSession.execute
        triggered = False

        async def racing_execute(self_session, statement, *a, **kw):
            nonlocal triggered
            result = await original_execute(self_session, statement, *a, **kw)
            if not triggered:
                triggered = True
                monkeypatch.setattr(AsyncSession, "execute", original_execute)
                async with session_maker() as other:
                    other_pos = await other.get(PositionModel, "pos_exp_race")
                    other_pos.status = "CLOSED"
                    await other.commit()
            return result

        monkeypatch.setattr(AsyncSession, "execute", racing_execute)
        summary = ExecutorRunSummary(run_started_at="2026-08-20T00:00:00+00:00", run_date=market_today().isoformat())
        async with session_maker() as session:
            await _settle_expired(session, summary)

        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_exp_race")
            book = await session.get(BookModel, "B01")
            pms = (
                (await session.execute(select(ClosurePostMortemModel).filter_by(position_id="pos_exp_race")))
                .scalars()
                .all()
            )
        assert pos.status == "CLOSED"  # left exactly as the concurrent closer set it
        assert book.cash_balance == 10000.0  # no expiry cash stacked on top
        assert pms == []  # no duplicate expectancy row
        assert any("EXPIRY SETTLEMENT SKIPPED" in n for n in summary.notes)
        assert await _audits(session_maker, "EXPIRY_SETTLEMENT_SKIPPED_CONCURRENT_CLOSE")


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
    async def test_roll_skipped_on_a_stale_telemetry_night(self, session_maker):
        # Audit II (#350): the roll is an ENTRY. On a stale night Layer C
        # blocks every ordinary entry — the roll must not place off
        # possibly-garbage quotes either. The close still goes out.
        expiry = market_today() + datetime.timedelta(days=12)
        pos = _roll_pos("pos_stale_roll", expiry, current_value=2.6)  # loser
        async with session_maker() as session:
            session.add(pos)
            await session.commit()
        broker = FakeBroker()
        broker.position_rows = [_roll_leg_at_broker(expiry)]
        with (
            patch.object(operator_mod, "fetch_market_telemetry", return_value=None),  # stale
            patch.object(operator_mod, "fetch_options_latest_quotes", return_value={}),
            patch.object(operator_mod, "fetch_index_daily_closes", return_value=None),
            patch.object(executor_mod, "fetch_options_latest_quotes", side_effect=_priced),
        ):
            summary = await run_executor_evening(session_maker=session_maker, broker_factory=lambda: broker)
        assert any("B31" in ref and ref.endswith(":close") for ref in summary.closes_placed)
        assert broker.placed == []  # no entries at all — roll included
        assert not await _audits(session_maker, "ROLL_STAGED")
        (skip,) = await _audits(session_maker, "ROLL_SKIPPED")
        assert skip.payload["reason"] == "stale telemetry"

    @pytest.mark.asyncio
    async def test_roll_never_stamps_insufficient_data_as_a_regime(self, session_maker):
        # Audit II (#350): a non-reading is not a regime — stamping it would
        # poison the regime-flip exit and the hit-rate analysis.
        from backend.regime_variants import INSUFFICIENT_DATA

        expiry = market_today() + datetime.timedelta(days=12)
        pos = _roll_pos("pos_nodata", expiry, current_value=2.6)
        async with session_maker() as session:
            session.add(pos)
            await session.commit()
        broker = FakeBroker()
        broker.position_rows = [_roll_leg_at_broker(expiry)]
        p1, p2, p3, p4 = _patches()
        with (
            p1,
            p2,
            p3,
            p4,
            patch.object(executor_mod, "persist_regime_readings", return_value={"V0": INSUFFICIENT_DATA}),
        ):
            await run_executor_evening(session_maker=session_maker, broker_factory=lambda: broker)
        async with session_maker() as session:
            entries = (
                (await session.execute(select(OrderModel).filter_by(book_id="B31", action="OPEN"))).scalars().all()
            )
        (roll_entry,) = [o for o in entries if o.combo_legs.get("rolled_from")]
        assert roll_entry.combo_legs["entry_regime"] == ""

    @pytest.mark.asyncio
    async def test_roll_stages_only_once_while_the_close_rests(self, session_maker):
        # Audit II (#344): the close can rest for several evenings on the
        # escalation ladder, and the trigger re-fired nightly while pos.rolls
        # still counted the ORIGINAL's rolls — a fresh roll entry every night.
        expiry = market_today() + datetime.timedelta(days=12)
        pos = _roll_pos("pos_once", expiry, current_value=2.6)
        async with session_maker() as session:
            session.add(pos)
            await session.commit()
        broker = FakeBroker()
        broker.position_rows = [_roll_leg_at_broker(expiry)]
        await _run(session_maker, broker)
        # Night 2: the close is still working at the broker, the position is
        # still OPEN, the time exit fires again.
        async with session_maker() as session:
            close = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_once", action="CLOSE")))
                .scalars()
                .one()
            )
        broker.ref_states[close.order_ref] = RefState.OPEN
        await _run(session_maker, broker)
        async with session_maker() as session:
            all_entries = (
                (await session.execute(select(OrderModel).filter_by(book_id="B31", action="OPEN"))).scalars().all()
            )
            stamped = await session.get(PositionModel, "pos_once")
        rolls = [o for o in all_entries if o.combo_legs.get("rolled_from") == "pos_once"]
        assert len(rolls) == 1  # night 2 did NOT stage a second roll
        assert stamped.journal["rolled_to_ref"] == rolls[0].order_ref
        assert len(await _audits(session_maker, "ROLL_STAGED")) == 1

    @pytest.mark.asyncio
    async def test_roll_broker_error_aborts_layer_c_entries(self, session_maker):
        # Audit II R2 (#421): a roll is an ENTRY on the order path. Its
        # BrokerError used to be swallowed while Layer C placed ordinary
        # entries against the same broker minutes later — design §3.2 says
        # the first order-path error ends the submission phase.
        from backend.broker import BrokerError

        expiry = market_today() + datetime.timedelta(days=12)
        pos = _roll_pos("pos_rbe", expiry, current_value=2.6)  # losing → roll fires
        async with session_maker() as session:
            session.add(pos)
            await session.commit()
        broker = FakeBroker()
        broker.position_rows = [_roll_leg_at_broker(expiry)]
        broker.fail_place = BrokerError("simulated competing-session 162")
        summary = await _run(session_maker, broker)
        assert summary.closes_placed  # the exit itself still went out
        assert summary.entries_placed == []
        rejected = await _audits(session_maker, "ORDER_REJECTED")
        assert len(rejected) == 1  # the roll's rejection only — Layer C never ran
        (aborted,) = await _audits(session_maker, "ENTRY_PHASE_ABORTED")
        assert "roll" in aborted.payload["reason"]

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
    async def test_rolled_fill_latches_the_source_position_even_without_the_atomic_stamp(self, session_maker):
        # #483: rolled_to_ref is normally stamped atomically with the roll
        # order's own SUBMITTED commit (#421) — but a crash between
        # placeOrder and that commit leaves the source position's journal
        # unstamped even though the order genuinely rests at the broker. This
        # simulates exactly that: the source position has NO rolled_to_ref
        # yet, and the roll order's OWN row never went through
        # _try_place_entry (it's inserted directly, STAGED, as if the crash
        # happened right after placeOrder). The sync discovering its FILL
        # must stamp the latch itself — otherwise the source's still-pending
        # close would keep laddering on later nights with no latch in sight,
        # staging a second roll.
        source = PositionModel(
            id="pos_old",
            underlying="XSP",
            strategy_type="BULL_PUT_SPREAD",
            execution_mode="PAPER",
            legs=[],
            entry_date="2026-08-01",
            expiration_date="2026-09-01",
            entry_premium=2.0,
            premium_direction="CREDIT",
            current_value_per_share=2.0,
            contracts=1,
            max_profit=2.0,
            max_loss=3.0,
            notes="",
            rolls=0,
            status="OPEN",  # its own close is still pending, unrelated to this fill
            journal={
                "core_thesis_rationale": "t",
                "structural_invalidation": "t",
                "expected_underlying_move_pct": 1.0,
                "pre_trade_emotional_state": "Calm",
                "pre_trade_confidence_rating": 3,
            },
            book_id="B31",
        )
        order = _order("o_rolled2", "STAGED", "basis:B31:o_rolled2:open")  # never reached SUBMITTED — the crash
        order.book_id = "B31"
        order.combo_legs = {**ORDER_META, "rolls": 1, "rolled_from": "pos_old"}
        async with session_maker() as session:
            session.add(source)
            session.add(order)
            await session.commit()
        broker = FakeBroker()
        broker.ref_states["basis:B31:o_rolled2:open"] = RefState.FILLED
        await _run(session_maker, broker)
        async with session_maker() as session:
            refreshed_source = await session.get(PositionModel, "pos_old")
        assert refreshed_source.journal.get("rolled_to_ref") == "basis:B31:o_rolled2:open"

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
    async def test_skips_staging_a_close_on_a_position_closed_concurrently(self, session_maker, monkeypatch):
        # #465 (Audit II R3 F4): `open_positions` at the top of this function
        # is a run-start snapshot. An operator flattening at the broker and
        # recording the external close (a DIFFERENT session) in the gap
        # between that snapshot and this candidate's staging must not still
        # get a full-size SELL of the bag submitted — that is a real naked
        # short at the broker, not just a books discrepancy. Simulate the gap
        # deterministically: intercept the fresh populate_existing re-read
        # this fix adds and, from inside it, close the position via a second
        # session before the real re-read runs.
        expiry = market_today() + datetime.timedelta(days=12)  # P1_TIME_EXIT: due at 21 DTE default
        occ = f"XSP{expiry:%y%m%d}P00610000"
        pos = PositionModel(
            id="pos_race_close",
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
            current_value_per_share=1.9,
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
            playbook_snapshot=_snapshot(),
            last_priced_at=datetime.datetime.now(datetime.UTC).isoformat(),
            book_id="B01",
        )
        async with session_maker() as session:
            session.add(pos)
            await session.commit()

        original_get = AsyncSession.get
        triggered = False

        async def racing_get(self_session, model, ident, *a, **kw):
            nonlocal triggered
            if not triggered and model is PositionModel and kw.get("populate_existing"):
                triggered = True
                monkeypatch.setattr(AsyncSession, "get", original_get)
                async with session_maker() as other:
                    other_pos = await other.get(PositionModel, ident)
                    other_pos.status = "CLOSED"
                    await other.commit()
            return await original_get(self_session, model, ident, *a, **kw)

        monkeypatch.setattr(AsyncSession, "get", racing_get)
        broker = FakeBroker()
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ),
        ]
        summary = await _run(session_maker, broker)

        assert broker.closed == []  # no SELL ever reached the broker
        assert summary.closes_placed == []
        async with session_maker() as session:
            closes = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_race_close", action="CLOSE")))
                .scalars()
                .all()
            )
        assert closes == []  # no staged order at all — not even REJECTED
        assert await _audits(session_maker, "CLOSE_SKIPPED_NOT_OPEN")

    @pytest.mark.asyncio
    async def test_layer_a_control_read_forces_a_fresh_row(self, session_maker, monkeypatch):
        # #546 F8: _layer_a_closes read every control row via a plain select
        # — a row already in this session's identity map (e.g. this run's
        # own sync latching HALT_ENTRIES on a book earlier tonight) could
        # shadow a console FLATTEN_REQUESTED posted mid-run on that same
        # scope, missing the flatten. Fix: execution_options(populate_existing=True),
        # matching #464's own fix on the per-order choke-point read
        # (trading_control.get_control_state). Pinned at the statement
        # level, since it's the option itself — not a downstream symptom —
        # that this fix adds.
        far = (market_today() + datetime.timedelta(days=90)).isoformat()
        pos = _expired_pos("pos_control_read", far, value=1.15)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        async with session_maker() as session:
            session.add(pos)
            await session.commit()

        original_execute = AsyncSession.execute
        seen: list[bool] = []

        async def spying_execute(self_session, statement, *a, **kw):
            if "trading_control" in str(statement):
                seen.append(statement.get_execution_options().get("populate_existing") is True)
            return await original_execute(self_session, statement, *a, **kw)

        monkeypatch.setattr(AsyncSession, "execute", spying_execute)
        broker = FakeBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        await _run(session_maker, broker)
        assert seen, "the Layer A control select never ran — test setup invalid"
        assert all(seen)

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
    async def test_stale_mark_skips_the_close_on_a_naive_timestamp_not_crashed(self, session_maker):
        # #545 L4: same TypeError-vs-ValueError gap as the expiry-settlement
        # guard, on the close-path's own stale-mark check — a naive
        # last_priced_at must skip the close, not crash the run.
        stale = _expired_pos("pos_stale_naive", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        stale.last_priced_at = "2026-08-14T12:00:00"  # no tzinfo
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
                (await session.execute(select(OrderModel).filter_by(position_id="pos_stale_naive", action="CLOSE")))
                .scalars()
                .all()
            )
            pos = await session.get(PositionModel, "pos_stale_naive")
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
    async def test_rejected_and_crash_expired_attempts_are_not_rungs(self, session_maker):
        # Audit II R2 (#420): a REJECTED row never reached the broker and a
        # crash-expired intent never rested — neither is a market attempt,
        # so neither may advance the concession or exhaust the ladder.
        pos = _expired_pos("pos_rungs", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        pos.current_value_per_share = 0.30  # P1 profit target
        async with session_maker() as session:
            session.add(pos)
            rejected = _order("o_rej", "REJECTED", "basis:B01:o_rej:close")
            rejected.action = "CLOSE"
            rejected.position_id = "pos_rungs"
            rejected.encumbered_risk = 0.0
            crash_expired = _order("o_crash", "CANCELLED", "basis:B01:o_crash:close")
            crash_expired.action = "CLOSE"
            crash_expired.position_id = "pos_rungs"
            crash_expired.submitted_at = None  # STAGED intent expired after a crash
            crash_expired.encumbered_risk = 0.0
            genuine = _order("o_real", "CANCELLED", "basis:B01:o_real:close")
            genuine.action = "CLOSE"
            genuine.position_id = "pos_rungs"
            genuine.encumbered_risk = 0.0
            session.add_all([rejected, crash_expired, genuine])
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        await _run(session_maker, broker)
        (event,) = await _audits(session_maker, "CLOSE_SUBMITTED")
        assert event.payload["rung"] == 1  # only the genuine market attempt counts

    @pytest.mark.asyncio
    async def test_rerun_with_resting_close_does_not_stage_a_duplicate(self, session_maker):
        # Audit II R2 (#405): every pass mints a fresh uuid ref, so neither
        # duplicate guard catches a same-evening re-run — two live closes on
        # the same legs, both able to fill next session.
        pos = _expired_pos("pos_dup", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        pos.current_value_per_share = 0.30  # P1 profit target every run
        async with session_maker() as session:
            session.add(pos)
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        await _run(session_maker, broker)
        async with session_maker() as session:
            close = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_dup", action="CLOSE")))
                .scalars()
                .one()
            )
        # Same-evening catch-up run: the first close is still working.
        broker.ref_states[close.order_ref] = RefState.OPEN
        await _run(session_maker, broker)
        async with session_maker() as session:
            closes = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_dup", action="CLOSE")))
                .scalars()
                .all()
            )
        assert len(closes) == 1  # the re-run staged nothing
        assert await _audits(session_maker, "CLOSE_ALREADY_PENDING")

    @pytest.mark.asyncio
    async def test_externally_closed_legs_are_not_re_sold(self, session_maker):
        # Audit II R2 (#407): reconciliation says the broker no longer holds
        # these legs (EXTERNAL_CLOSE drift), but marks come from market data,
        # so the P1 still fires and Layer A would sell a bag the account
        # doesn't hold — a naked short if it fills.
        pos = _expired_pos("pos_ext", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        pos.current_value_per_share = 0.30  # P1 profit target
        async with session_maker() as session:
            session.add(pos)
            await session.commit()
        broker = FakeBroker()  # no position_rows: the legs are gone at the broker
        await _run(session_maker, broker)
        async with session_maker() as session:
            closes = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_ext", action="CLOSE")))
                .scalars()
                .all()
            )
        assert closes == []
        assert await _audits(session_maker, "CLOSE_SKIPPED_DRIFTED_LEGS")

    @pytest.mark.asyncio
    async def test_leg_bought_back_after_reconciliation_still_blocks_the_close(self, session_maker):
        # #684: reconciliation's drift snapshot is a run-start read — an
        # operator buying back a short leg directly at the broker AFTER that
        # read (but before Layer A reaches this position) used to sail
        # straight past both the #407 drift skip (computed too early to see
        # it) and the #465 fresh-read guard (DB-only; reconciliation never
        # writes PositionModel.status). The leg is present for the FIRST
        # broker.positions() read (reconciliation stays CLEAN) and gone by
        # the SECOND (the fresh re-check right before Layer A) — the fresh
        # re-check must still catch it.
        pos = _expired_pos("pos_lateext", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        pos.current_value_per_share = 0.30  # P1 profit target — would stage a close
        async with session_maker() as session:
            session.add(pos)
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        broker.position_rows_after_reconciliation = []  # bought back between the two reads
        summary = await _run(session_maker, broker)
        assert summary.reconciliation == "CLEAN"  # the FIRST read saw nothing wrong
        async with session_maker() as session:
            closes = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_lateext", action="CLOSE")))
                .scalars()
                .all()
            )
        assert closes == []  # the SECOND read caught it before staging
        assert await _audits(session_maker, "CLOSE_SKIPPED_DRIFTED_LEGS")

    @pytest.mark.asyncio
    async def test_ghost_order_drift_blocks_close_staging_same_run(self, session_maker):
        # #559: the exact incident shape (2026-08-20 18:45 evening run) — an
        # unresolved GHOST_ORDER on a position's own entry (a legacy GTC
        # take-profit live at the broker with no DB row) in the SAME run
        # that would otherwise stage a fresh close on that position. Both
        # live is a double-exit window: if both fill, the account ends up
        # short the spread. The position id (pos_o_ghost1a2b) is derived
        # from the ghost ref's order id exactly like _order_to_position
        # would key it on a real fill.
        pos = _expired_pos("pos_o_ghost1a2b", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        pos.current_value_per_share = 0.30  # P1 profit target — would stage a close
        async with session_maker() as session:
            session.add(pos)
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        # Ghost TP: live at the broker, no DB row anywhere for this ref —
        # exactly the "legacy order placed before its DB row existed" shape.
        ghost_ref = "basis:B01:o_ghost1a2b:open:tp"
        broker.open_order_rows = [OpenOrderInfo(order_ref=ghost_ref, order_id=99, perm_id=None, status="Submitted")]
        summary = await _run(session_maker, broker)
        assert summary.reconciliation == "DRIFT"
        async with session_maker() as session:
            closes = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_o_ghost1a2b", action="CLOSE")))
                .scalars()
                .all()
            )
        assert closes == []  # no close staged — the double-exit window never opens
        events = await _audits(session_maker, "CLOSE_SKIPPED_GHOST_ORDER_DRIFT")
        assert len(events) == 1
        assert events[0].payload["position_id"] == "pos_o_ghost1a2b"

    @pytest.mark.asyncio
    async def test_ghost_order_drift_on_a_different_position_does_not_block_this_one(self, session_maker):
        # #559: the skip must be scoped to the position the ghost ref
        # actually belongs to — an unresolved ghost elsewhere must not
        # blanket-block every close tonight (that's what the pre-existing
        # global HALT_ENTRIES already does for entries; Layer A closes are
        # deliberately still evaluated per-position).
        pos = _expired_pos("pos_o_clean9z8y", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        pos.current_value_per_share = 0.30  # P1 profit target
        async with session_maker() as session:
            session.add(pos)
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        # A ghost belonging to an entirely different (nonexistent) position.
        ghost_ref = "basis:B01:o_unrelated1:open:tp"
        broker.open_order_rows = [OpenOrderInfo(order_ref=ghost_ref, order_id=99, perm_id=None, status="Submitted")]
        await _run(session_maker, broker)
        async with session_maker() as session:
            closes = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_o_clean9z8y", action="CLOSE")))
                .scalars()
                .all()
            )
        assert len(closes) == 1  # this position's close still staged normally
        assert not await _audits(session_maker, "CLOSE_SKIPPED_GHOST_ORDER_DRIFT")

    @pytest.mark.asyncio
    async def test_partial_tp_blocks_close_staging(self, session_maker):
        # Audit II R2 (#413): a PARTIAL order means the true filled size is
        # UNKNOWN (#348) — a full-size close would over-close into naked
        # exposure, and the latch is a human's to resolve, not ours to
        # cancel over.
        pos = _expired_pos("pos_ptp", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        pos.current_value_per_share = 0.30  # P1 profit target
        tp = _order("o_ptp_tp", "PARTIAL", "basis:B01:o_ptp:open:tp")
        tp.action = "CLOSE"
        tp.position_id = "pos_ptp"
        tp.encumbered_risk = 0.0
        async with session_maker() as session:
            session.add(pos)
            session.add(tp)
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        await _run(session_maker, broker)
        async with session_maker() as session:
            closes = (
                (await session.execute(select(OrderModel).filter_by(position_id="pos_ptp", action="CLOSE")))
                .scalars()
                .all()
            )
            tp_after = await session.get(OrderModel, "o_ptp_tp")
        assert [o.id for o in closes] == ["o_ptp_tp"]  # nothing new staged
        assert tp_after.status == "PARTIAL"  # the latch survives untouched
        assert await _audits(session_maker, "CLOSE_SKIPPED_PARTIAL_TP")

    @pytest.mark.asyncio
    async def test_tp_cancel_with_same_day_fills_latches_partial(self, session_maker):
        # Audit II R2 (#413): the GTC TP can execute part of the position in
        # the morning and still be resting at cancel time — stamping
        # CANCELLED would bury the partial. Latch it like the sync does.
        pos = _expired_pos("pos_tpf", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        pos.current_value_per_share = 0.30  # P1 profit target → cancel-first
        tp = _order("o_tpf_tp", "SUBMITTED", "basis:B01:o_tpf:open:tp")
        tp.action = "CLOSE"
        tp.position_id = "pos_tpf"
        tp.encumbered_risk = 0.0
        async with session_maker() as session:
            session.add(pos)
            session.add(tp)
            session.add(
                FillModel(
                    exec_id="x_tpf_1",
                    order_id="o_tpf_tp",
                    book_id="B01",
                    con_id=1,
                    side="BOT",
                    quantity=1.0,
                    price=0.30,
                    commission=1.1,
                    fill_time="2026-08-20T13:31:00+00:00",
                )
            )
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        broker.ref_states["basis:B01:o_tpf:open:tp"] = RefState.OPEN  # still resting at sync
        await _run(session_maker, broker)
        async with session_maker() as session:
            tp_after = await session.get(OrderModel, "o_tpf_tp")
            new_closes = (
                (
                    await session.execute(
                        select(OrderModel).filter(
                            OrderModel.position_id == "pos_tpf",
                            OrderModel.action == "CLOSE",
                            OrderModel.id != "o_tpf_tp",
                        )
                    )
                )
                .scalars()
                .all()
            )
            control = await session.get(TradingControlModel, "B01")
        assert tp_after.status == "PARTIAL"
        assert new_closes == []  # unknown size — no close staged
        assert control.state == "HALT_ENTRIES"
        assert await _audits(session_maker, "PARTIAL_FILL")

    @pytest.mark.asyncio
    async def test_unconfirmed_tp_cancel_stages_no_close_and_stays_submitted(self, session_maker, monkeypatch):
        # Audit II R3 (#467): cancelOrder is fire-and-return and IBKR rejects
        # a cancel racing a fill. If the order is still on the broker's book
        # after the confirm checks, stamping CANCELLED would make the row
        # terminal (the sync never re-reads it) — and staging the replacement
        # close would put two live exits on the same legs.
        monkeypatch.setattr(executor_mod, "TP_CANCEL_CONFIRM_DELAY_S", 0.0)
        pos = _expired_pos("pos_utp", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        pos.current_value_per_share = 0.30  # P1 profit target → cancel-first
        tp = _order("o_utp_tp", "SUBMITTED", "basis:B01:o_utp:open:tp")
        tp.action = "CLOSE"
        tp.position_id = "pos_utp"
        tp.encumbered_risk = 0.0
        async with session_maker() as session:
            session.add(pos)
            session.add(tp)
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        broker.ref_states["basis:B01:o_utp:open:tp"] = RefState.OPEN
        # The cancel never takes: the order sits in PendingCancel forever.
        broker.open_order_rows = [
            OpenOrderInfo(order_ref="basis:B01:o_utp:open:tp", order_id=7, perm_id=None, status="PendingCancel")
        ]
        summary = await _run(session_maker, broker)
        async with session_maker() as session:
            tp_after = await session.get(OrderModel, "o_utp_tp")
            new_closes = (
                (
                    await session.execute(
                        select(OrderModel).filter(
                            OrderModel.position_id == "pos_utp",
                            OrderModel.action == "CLOSE",
                            OrderModel.id != "o_utp_tp",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert tp_after.status == "SUBMITTED"  # NOT stamped terminal on faith
        assert new_closes == []  # no second live exit
        assert await _audits(session_maker, "TP_CANCEL_UNCONFIRMED")
        assert any("TP CANCEL UNCONFIRMED" in n for n in summary.notes)

    @pytest.mark.asyncio
    async def test_unconfirmed_tp_cancel_escalates_after_three_consecutive_nights(self, session_maker, monkeypatch):
        # #546 liveness: if a TP cancel is persistently unconfirmed, Layer A
        # skips the close nightly with only a digest note — no rung
        # consumed, no escalation ever, so a stuck close could skip silently
        # forever. Three consecutive unconfirmed nights on the same ref must
        # escalate urgently (TP_CANCEL_STUCK), directing manual cancellation.
        monkeypatch.setattr(executor_mod, "TP_CANCEL_CONFIRM_DELAY_S", 0.0)
        pos = _expired_pos("pos_stuck_tp", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        pos.current_value_per_share = 0.30  # P1 profit target → cancel-first
        tp = _order("o_stuck_tp", "SUBMITTED", "basis:B01:o_stuck:open:tp")
        tp.action = "CLOSE"
        tp.position_id = "pos_stuck_tp"
        tp.encumbered_risk = 0.0
        async with session_maker() as session:
            session.add(pos)
            session.add(tp)
            await session.commit()
        broker = FakeBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        broker.ref_states["basis:B01:o_stuck:open:tp"] = RefState.OPEN
        # The cancel never takes, every single night.
        broker.open_order_rows = [
            OpenOrderInfo(order_ref="basis:B01:o_stuck:open:tp", order_id=7, perm_id=None, status="PendingCancel")
        ]
        summary1 = await _run(session_maker, broker)
        summary2 = await _run(session_maker, broker)
        summary3 = await _run(session_maker, broker)
        assert not any("TP CANCEL STUCK" in n for n in summary1.notes)
        assert not any("TP CANCEL STUCK" in n for n in summary2.notes)
        assert any("TP CANCEL STUCK" in n for n in summary3.notes)
        stuck_events = await _audits(session_maker, "TP_CANCEL_STUCK")
        assert len(stuck_events) == 1
        assert stuck_events[0].payload["consecutive_unconfirmed"] == 3
        assert stuck_events[0].payload["order_ref"] == "basis:B01:o_stuck:open:tp"

    @pytest.mark.asyncio
    async def test_tp_that_filled_during_cancel_latches_partial(self, session_maker):
        # Audit II R3 (#467): the order leaving the open-order book is
        # ambiguous — Filled orders leave it too. Executions on the ref that
        # the sync hadn't backfilled yet are the tell; latch PARTIAL for a
        # human instead of stamping CANCELLED over moved contracts.
        pos = _expired_pos("pos_rtp", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        pos.current_value_per_share = 0.30  # P1 profit target → cancel-first
        tp = _order("o_rtp_tp", "SUBMITTED", "basis:B01:o_rtp:open:tp")
        tp.action = "CLOSE"
        tp.position_id = "pos_rtp"
        tp.encumbered_risk = 0.0
        async with session_maker() as session:
            session.add(pos)
            session.add(tp)
            await session.commit()

        class RacingBroker(FakeBroker):
            # The fill lands exactly as the cancel goes up: no executions at
            # sync time, one on the ref by the time Layer A re-checks.
            def cancel_by_ref(self, ref):
                result = super().cancel_by_ref(ref)
                self.execution_rows.append(
                    FillInfo(
                        exec_id="x_race",
                        con_id=1,
                        side="BOT",
                        quantity=1.0,
                        price=0.30,
                        order_ref=ref,
                        commission=None,
                        exec_time="2024-01-01T00:00:00+00:00",
                    )
                )
                return result

        broker = RacingBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        broker.ref_states["basis:B01:o_rtp:open:tp"] = RefState.OPEN
        await _run(session_maker, broker)
        async with session_maker() as session:
            tp_after = await session.get(OrderModel, "o_rtp_tp")
            new_closes = (
                (
                    await session.execute(
                        select(OrderModel).filter(
                            OrderModel.position_id == "pos_rtp",
                            OrderModel.action == "CLOSE",
                            OrderModel.id != "o_rtp_tp",
                        )
                    )
                )
                .scalars()
                .all()
            )
            control = await session.get(TradingControlModel, "B01")
        assert tp_after.status == "PARTIAL"  # moved contracts latched for a human
        assert new_closes == []  # unknown size — no close staged
        assert control.state == "HALT_ENTRIES"
        assert await _audits(session_maker, "PARTIAL_FILL")

    @pytest.mark.asyncio
    async def test_unknown_ref_with_fills_latches_partial_instead_of_terminalizing(self, session_maker):
        # Audit II R3 (#470, fix-attacker F4): a GTC order that partially
        # fills Monday and falls out of Tuesday's reqCompletedOrders window
        # reads UNKNOWN — the sync's UNKNOWN branch used to terminalize it
        # with no fills check, the one route around the PARTIAL latch that
        # left no halt and no human in the loop.
        async with session_maker() as session:
            session.add(_order("o_unkf", "SUBMITTED", "basis:B01:o_unkf:open"))
            session.add(
                FillModel(
                    exec_id="x_unkf_1",
                    order_id="o_unkf",
                    book_id="B01",
                    con_id=1,
                    side="SLD",
                    quantity=1.0,
                    price=1.20,
                    commission=1.1,
                    fill_time="2026-08-20T13:31:00+00:00",
                )
            )
            await session.commit()
        broker = FakeBroker()  # ref absent from ref_states → UNKNOWN verdict
        await _run(session_maker, broker)
        async with session_maker() as session:
            row = await session.get(OrderModel, "o_unkf")
            control = await session.get(TradingControlModel, "B01")
        assert row.status == "PARTIAL"  # latched, not buried as CANCELLED
        assert control.state == "HALT_ENTRIES"
        assert await _audits(session_maker, "PARTIAL_FILL")
        assert not await _audits(session_maker, "ORDER_LOST_AT_BROKER")
        # 1 of 2 intended units filled — this is NOT the full-fill case.
        assert not await _audits(session_maker, "PARTIAL_LATCH_FULL_FILL")

    @pytest.mark.asyncio
    async def test_full_fill_with_wrong_verdict_is_marked_for_the_operator(self, session_maker):
        # Audit II R3 (#470, fix-attacker F3): with #406 a fully-filled entry
        # whose completed-orders verdict was wrongly non-Filled dead-ends in
        # the PARTIAL latch with no in-band recovery — no position exists to
        # externally close. The marker tells the operator which case this is.
        async with session_maker() as session:
            session.add(_order("o_ffvd", "SUBMITTED", "basis:B01:o_ffvd:open"))
            for i, side in enumerate(("SLD", "BOT")):  # both legs, full quantity
                session.add(
                    FillModel(
                        exec_id=f"x_ffvd_{i}",
                        order_id="o_ffvd",
                        book_id="B01",
                        con_id=i + 1,
                        side=side,
                        quantity=1.0,
                        price=1.20,
                        commission=1.1,
                        fill_time="2026-08-20T13:31:00+00:00",
                    )
                )
            await session.commit()
        broker = FakeBroker()
        broker.ref_states["basis:B01:o_ffvd:open"] = RefState.CANCELLED  # wrong verdict, fills say otherwise
        await _run(session_maker, broker)
        async with session_maker() as session:
            row = await session.get(OrderModel, "o_ffvd")
        assert row.status == "PARTIAL"
        (marker,) = await _audits(session_maker, "PARTIAL_LATCH_FULL_FILL")
        assert marker.payload["filled_units"] == 2.0
        assert marker.payload["intended_units"] == 2

    @pytest.mark.asyncio
    async def test_resting_order_with_fills_latches_and_blocks_expiry_settlement(self, session_maker):
        # Audit II R4 (#531 F1): a partially-filled order STILL RESTING for
        # its remainder reads OPEN — the one verdict arm with no fills check.
        # On an expiry night _settle_expired then saw no PARTIAL row and
        # booked a FULL-size settlement over contracts the broker traded.
        expiry = (market_today() - datetime.timedelta(days=1)).isoformat()
        async with session_maker() as session:
            pos = _expired_pos("pos_of", expiry)  # fresh mark by default
            session.add(pos)
            tp = _order("o_of_tp", "SUBMITTED", "basis:B01:o_of:open:tp")
            tp.action = "CLOSE"
            tp.position_id = "pos_of"
            tp.encumbered_risk = 0.0
            session.add(tp)
            session.add(
                FillModel(
                    exec_id="x_of_1",
                    order_id="o_of_tp",
                    book_id="B01",
                    con_id=1,
                    side="BOT",
                    quantity=1.0,
                    price=0.30,
                    commission=1.1,
                    fill_time="2026-08-20T13:31:00+00:00",
                )
            )
            await session.commit()
        broker = FakeBroker()
        broker.ref_states["basis:B01:o_of:open:tp"] = RefState.OPEN  # resting for the remainder
        await _run(session_maker, broker)
        async with session_maker() as session:
            row = await session.get(OrderModel, "o_of_tp")
            pos2 = await session.get(PositionModel, "pos_of")
            book = await session.get(BookModel, "B01")
            control = await session.get(TradingControlModel, "B01")
        assert row.status == "PARTIAL"  # latched, not left SUBMITTED
        assert pos2.status == "OPEN"  # NOT settled at full size
        assert book.cash_balance == 10000.0  # no fabricated settlement cash
        assert control.state == "HALT_ENTRIES"
        assert await _audits(session_maker, "EXPIRY_SETTLEMENT_BLOCKED_PARTIAL")

    @pytest.mark.asyncio
    async def test_staged_unknown_with_fills_latches_instead_of_expiring_the_intent(self, session_maker):
        # Audit II R4 (#531 F3): "crash before submission" is only a
        # hypothesis — a crash AFTER placement leaves STAGED too, and a
        # feed blip can read the placed order UNKNOWN. Fills are the tell.
        async with session_maker() as session:
            order = _order("o_su", "STAGED", "basis:B01:o_su:open")
            session.add(order)
            session.add(
                FillModel(
                    exec_id="x_su_1",
                    order_id="o_su",
                    book_id="B01",
                    con_id=1,
                    side="SLD",
                    quantity=1.0,
                    price=1.20,
                    commission=1.1,
                    fill_time="2026-08-20T13:31:00+00:00",
                )
            )
            await session.commit()
        broker = FakeBroker()  # ref absent → UNKNOWN
        summary = await _run(session_maker, broker)
        async with session_maker() as session:
            row = await session.get(OrderModel, "o_su")
        assert row.status == "PARTIAL"  # latched, not INTENT_EXPIRED over fills
        assert "basis:B01:o_su:open" not in summary.intents_expired
        assert await _audits(session_maker, "PARTIAL_FILL")

    @pytest.mark.asyncio
    async def test_tp_that_fully_filled_during_cancel_gets_the_disagreement_marker(self, session_maker):
        # Audit II R4 (#531 F4): a TP that FULLY filled during the cancel
        # race is a healthy exit wearing the latch — Layer A now routes
        # through the shared _latch_partial, so the operator gets the
        # full-fill breadcrumb instead of a lockout labeled "partial".
        pos = _expired_pos("pos_ftp", (market_today() + datetime.timedelta(days=90)).isoformat(), value=0.30)
        pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
        pos.current_value_per_share = 0.30  # P1 profit target → cancel-first
        tp = _order("o_ftp_tp", "SUBMITTED", "basis:B01:o_ftp:open:tp")
        tp.action = "CLOSE"
        tp.position_id = "pos_ftp"
        tp.encumbered_risk = 0.0
        async with session_maker() as session:
            session.add(pos)
            session.add(tp)
            await session.commit()

        class FullFillRacingBroker(FakeBroker):
            def cancel_by_ref(self, ref):
                result = super().cancel_by_ref(ref)
                for i, side in enumerate(("BOT", "SLD")):  # both legs, full quantity
                    self.execution_rows.append(
                        FillInfo(
                            exec_id=f"x_ftp_{i}",
                            con_id=i + 1,
                            side=side,
                            quantity=1.0,
                            price=0.30,
                            order_ref=ref,
                            commission=None,
                            exec_time="2024-01-01T00:00:00+00:00",
                        )
                    )
                return result

        broker = FullFillRacingBroker()
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        broker.ref_states["basis:B01:o_ftp:open:tp"] = RefState.OPEN
        await _run(session_maker, broker)
        async with session_maker() as session:
            tp_after = await session.get(OrderModel, "o_ftp_tp")
        assert tp_after.status == "PARTIAL"
        (marker,) = await _audits(session_maker, "PARTIAL_LATCH_FULL_FILL")
        assert marker.payload["filled_units"] == 2.0

    @pytest.mark.asyncio
    async def test_position_inherits_the_orders_decision_time_config_hash(self, session_maker):
        # #534 (Audit II R4): the position's era fingerprint is the hash the
        # ORDER was decided under, not whatever the book carries at
        # fill-sync time — a seed-sync between stage and fill (any process
        # start runs init_db) must not re-attribute the trade.
        async with session_maker() as session:
            order = _order("o_hash", "SUBMITTED", "basis:B01:o_hash:open")
            order.config_hash = "decided123"  # stamped at stage time
            session.add(order)
            await session.commit()
        broker = FakeBroker()
        broker.ref_states["basis:B01:o_hash:open"] = RefState.FILLED
        broker.position_rows = [
            LegPosition(
                con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol="XSP261218P00610000"
            ),
            LegPosition(
                con_id=2, symbol="XSP", sec_type="OPT", position=1.0, avg_cost=0, occ_symbol="XSP261218P00605000"
            ),
        ]
        await _run(session_maker, broker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_o_hash")
            book = await session.get(BookModel, "B01")
        assert pos is not None
        assert pos.config_hash == "decided123"
        assert pos.config_hash != book.config_hash  # NOT the fill-time book hash

    @pytest.mark.asyncio
    async def test_staged_but_resting_order_is_promoted_to_submitted(self, session_maker):
        # Audit II R3 (#481 F10): a crash between placeOrder and the
        # SUBMITTED commit leaves a genuinely-resting order STAGED. The
        # pending skip already prevents duplicates, but the rung counter
        # (#420) never counts it and its fill lands with a null
        # analysis timestamp. Promote with a best-effort stamp.
        async with session_maker() as session:
            order = _order("o_rest", "STAGED", "basis:B01:o_rest:open")
            order.submitted_at = None  # the crash beat the stamp
            session.add(order)
            await session.commit()
        broker = FakeBroker()
        broker.ref_states["basis:B01:o_rest:open"] = RefState.OPEN  # genuinely resting
        await _run(session_maker, broker)
        async with session_maker() as session:
            row = await session.get(OrderModel, "o_rest")
        assert row.status == "SUBMITTED"
        assert row.submitted_at is not None  # best-effort, no longer null for analysis
        assert await _audits(session_maker, "STAGED_ORDER_FOUND_RESTING")

    @pytest.mark.asyncio
    async def test_replayed_entry_fill_is_audited_not_silent(self, session_maker):
        # Audit II R3 (#481 F11): the idempotent replay (position already
        # exists from a run that crashed before the FILLED commit) correctly
        # moves no cash and mints no duplicate — but it used to leave no
        # audit trace at all.
        async with session_maker() as session:
            order = _order("o_replay", "SUBMITTED", "basis:B01:o_replay:open")
            session.add(order)
            pos = _expired_pos("pos_o_replay", (market_today() + datetime.timedelta(days=90)).isoformat())
            pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()
            session.add(pos)
            book_cash_before = 10000.0
            await session.commit()
        broker = FakeBroker()
        broker.ref_states["basis:B01:o_replay:open"] = RefState.FILLED
        occ = f"XSP{market_today() + datetime.timedelta(days=90):%y%m%d}P00610000"
        broker.position_rows = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=0, occ_symbol=occ)
        ]
        await _run(session_maker, broker)
        async with session_maker() as session:
            row = await session.get(OrderModel, "o_replay")
            book = await session.get(BookModel, "B01")
        assert row.status == "FILLED"
        assert book.cash_balance == book_cash_before  # replay books nothing twice
        (event,) = await _audits(session_maker, "ENTRY_FILL_REPLAYED")
        assert event.payload["position_id"] == "pos_o_replay"

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
