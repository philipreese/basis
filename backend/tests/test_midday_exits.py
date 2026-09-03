"""Tests for the 12:30 midday exit pass (backend/midday_exits.py, #960).

Every broker/gateway interaction is faked — no network, no processes. The
charter assertions matter most: the pass places EXITS and nothing else, never
creates two live exits on one position, never touches a close that already
filled, and refuses to trade when the books and the broker disagree about legs
the evening sync cannot explain (drift that IS sync-explainable does not stop
the pass, but its legs are still never traded — see TestDriftHalt).
"""

import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import executor, midday_exits
from backend.broker import BrokerError, LegPosition, PlacedOrder, ReconcileReport, RefState
from backend.dates import MARKET_TZ
from backend.executor import MIDDAY_REPRICE_OF_KEY, ExecutorRunSummary, _layer_a_closes
from backend.market_data import format_occ_symbol
from backend.midday_exits import (
    MIDDAY_EXITS_ACTED,
    MIDDAY_EXITS_DEGRADED,
    MIDDAY_EXITS_HALTED,
    MIDDAY_EXITS_OUT_OF_WINDOW,
    MIDDAY_EXITS_QUIET,
    MiddayResult,
    compose_midday_push,
    run_midday_exits,
)
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    FillModel,
    MarketStateModel,
    OrderModel,
    PositionModel,
    TradingControlModel,
)
from backend.run_lock import GATEWAY_TENANT_LOCKS
from backend.states import ORDER_PENDING_STATUSES

TODAY = datetime.date(2026, 8, 24)  # a Monday, ordinary trading day
MIDDAY = datetime.datetime(2026, 8, 24, 12, 30, tzinfo=MARKET_TZ)  # inside the 09:45-15:45 ET window
FAR_EXPIRY = "2026-12-18"  # well beyond the 21-DTE mandatory time exit
_GREEKS = {"delta": -0.2, "theta": 0.01, "vega": 0.05}  # entry-frozen, never read by an exit rule
_JOURNAL = {
    "core_thesis_rationale": "t",
    "structural_invalidation": "t",
    "expected_underlying_move_pct": 1.0,
    "pre_trade_emotional_state": "Calm",
    "pre_trade_confidence_rating": 3,
}


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _occ(strike: float, expiration: str = FAR_EXPIRY) -> str:
    return format_occ_symbol("XSP", expiration, "PUT", strike)


def _position(
    *,
    position_id: str = "p1",
    entry_premium: float = 1.00,
    current_value: float = 0.90,
    expiration: str = FAR_EXPIRY,
    leg_expirations: tuple[str, str] | None = None,
    priced_at: str | None = None,
    strikes: tuple[float, float] = (610.0, 605.0),
    book_id: str = "B01",
) -> PositionModel:
    front, back = leg_expirations or (expiration, expiration)
    short_strike, long_strike = strikes
    return PositionModel(
        id=position_id,
        underlying="XSP",
        strategy_type="BULL_PUT_SPREAD",
        execution_mode="PAPER",
        legs=[
            {"option_type": "PUT", "direction": "SHORT", "strike": short_strike, "expiration": front, **_GREEKS},
            {"option_type": "PUT", "direction": "LONG", "strike": long_strike, "expiration": back, **_GREEKS},
        ],
        entry_date="2026-08-10",
        expiration_date=expiration,
        entry_premium=entry_premium,
        premium_direction="CREDIT",
        current_value_per_share=current_value,
        contracts=1,
        max_profit=entry_premium,
        max_loss=5.0 - entry_premium,
        notes="",
        rolls=0,
        status="OPEN",
        journal=dict(_JOURNAL),
        book_id=book_id,
        last_priced_at=priced_at if priced_at is not None else _now(),
    )


def _close_order(
    order_id: str,
    *,
    position_id: str = "p1",
    status: str = "SUBMITTED",
    limit_price: float = -1.18,
    rung: int | None = 0,
    submitted: bool = True,
    repriced_from: str | None = None,
    ref: str | None = None,
) -> OrderModel:
    legs = {
        "legs": [
            {"option_type": "PUT", "direction": "SHORT", "strike": 610.0, "expiration": FAR_EXPIRY, **_GREEKS},
            {"option_type": "PUT", "direction": "LONG", "strike": 605.0, "expiration": FAR_EXPIRY, **_GREEKS},
        ],
        "quantity": 1,
        "exit_trigger": "PROFIT_TARGET",
    }
    if rung is not None:
        legs["rung"] = rung
    if repriced_from is not None:
        legs[MIDDAY_REPRICE_OF_KEY] = repriced_from
    return OrderModel(
        id=order_id,
        book_id="B01",
        position_id=position_id,
        order_ref=ref or f"basis:B01:{order_id}:close",
        ib_order_id=1,
        ib_perm_id=None,
        action="CLOSE",
        combo_legs=legs,
        order_type="LIMIT",
        limit_price=limit_price,
        decision_midpoint=limit_price,
        status=status,
        submitted_at=_now() if submitted else None,
        completed_at=None if status in ORDER_PENDING_STATUSES else _now(),
        encumbered_risk=0.0,
    )


def _broker_legs(contracts: int = 1, strikes: tuple[float, float] = (610.0, 605.0)) -> list[LegPosition]:
    """What the broker holds for one `_position()` — the clean, no-drift case.
    Books expect a SHORT leg as -1 and a LONG leg as +1 per contract."""
    short_strike, long_strike = strikes
    return [
        LegPosition(
            con_id=1,
            symbol="XSP",
            sec_type="OPT",
            position=-1.0 * contracts,
            avg_cost=100.0,
            occ_symbol=_occ(short_strike),
        ),
        LegPosition(
            con_id=2,
            symbol="XSP",
            sec_type="OPT",
            position=1.0 * contracts,
            avg_cost=80.0,
            occ_symbol=_occ(long_strike),
        ),
    ]


class FakeBroker:
    """Exactly the BrokerSession surface the midday pass consumes. Entry-path
    methods are recorded as forbidden calls — an exits-only pass must never
    reach them."""

    def __init__(
        self,
        *,
        open_exc: BaseException | None = None,
        close_exc: BaseException | None = None,
        close_exc_limit: int | None = None,
    ) -> None:
        self.open_exc = open_exc
        self.close_exc = close_exc
        # How many close_spread calls the broker refuses before accepting one.
        # None = refuse every time. A limit of 1 is the rejected-reprice case
        # where the pass's own fallback close must still go through.
        self.close_exc_limit = close_exc_limit
        self.close_failures: list[str] = []
        self.opened = False
        self.closed = False
        self.broker_positions: list[LegPosition] = _broker_legs()
        self.working: list[SimpleNamespace] = []  # open orders, by ref
        self.execs: list[SimpleNamespace] = []
        self.placed: list[tuple] = []
        self.cancelled: list[str] = []
        self.cancel_leaves_order_open = False
        self.forbidden_calls: list[str] = []
        self.reconciled = False

    def open(self) -> None:
        if self.open_exc is not None:
            raise self.open_exc
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def positions(self) -> list:
        return list(self.broker_positions)

    def executions(self, since: str | None = None) -> list:
        return list(self.execs)

    def open_orders(self) -> list:
        return list(self.working)

    def reconcile(self, refs, since=None) -> ReconcileReport:
        self.reconciled = True
        working = {o.order_ref for o in self.working}
        return ReconcileReport(
            states={r: (RefState.OPEN if r in working else RefState.UNKNOWN) for r in refs},
            broker_refs=frozenset(working),
        )

    def cancel_by_ref(self, ref: str) -> bool:
        self.cancelled.append(ref)
        if not self.cancel_leaves_order_open:
            self.working = [o for o in self.working if o.order_ref != ref]
        return True

    def close_spread(self, spread, ref: str) -> PlacedOrder:
        if self.close_exc is not None and (
            self.close_exc_limit is None or len(self.close_failures) < self.close_exc_limit
        ):
            self.close_failures.append(ref)
            raise self.close_exc
        self.placed.append((spread, ref))
        self.working.append(SimpleNamespace(order_ref=ref, order_id=99, perm_id=None, status="Submitted"))
        return PlacedOrder(order_id=99, perm_id=None, ref=ref, status="Submitted")

    # -- entry path: forbidden for an exits-only pass ------------------------

    def place_spread(self, *args, **kwargs):
        self.forbidden_calls.append("place_spread")

    def preview_spread(self, *args, **kwargs):
        self.forbidden_calls.append("preview_spread")


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        session.add(
            BookModel(
                id="B01",
                name="B01",
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
        session.add(
            MarketStateModel(
                id=1,
                current_regime="RANGE_BOUND",
                spy_price=645.0,
                spy_sma20=640.0,
                vix_close=15.0,
                underlying_ivrs={},
                spy_daily_return=0.0,
                catalyst_dates=[],
                regime_scores={},
            )
        )
        await session.commit()
    yield maker
    await engine.dispose()


@pytest.fixture
def pushes(monkeypatch):
    sent: list[tuple[str, str, str]] = []

    def _record(title: str, body: str, priority: str = "default", **kwargs) -> bool:
        sent.append((title, body, priority))
        return True

    monkeypatch.setattr(midday_exits, "send_ntfy_with_retry", _record)
    return sent


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    """Fake Gateway plumbing plus the two market-data refreshes.

    `refresh_position_values` and `refresh_market_state` are the pass's live
    quote path; both are exercised by their own tests in test_operator.py.
    Here they are replaced with fakes that stamp mark freshness and hand back
    the stored state, so each test controls the marks the exit rules see."""
    start_script = tmp_path / "StartGateway.bat"
    start_script.write_text("rem fake", encoding="utf-8")
    monkeypatch.setenv("IBC_START_SCRIPT", str(start_script))
    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path / "locks"))
    (tmp_path / "locks").mkdir()

    proc = SimpleNamespace(pid=4242)
    stopped: list = []
    monkeypatch.setattr(midday_exits, "get_free_memory_gb", lambda: 8.0)
    monkeypatch.setattr(midday_exits, "launch_gateway", lambda script: proc)
    monkeypatch.setattr(
        midday_exits,
        "wait_for_gateway_port",
        lambda host, port, **kw: SimpleNamespace(is_open=True, status="OPEN", elapsed_seconds=1.0),
    )
    monkeypatch.setattr(
        midday_exits, "stop_gateway_tree_only", lambda p=None, created_after=None, **k: stopped.append(p)
    )
    monkeypatch.setattr(midday_exits, "TP_CANCEL_CONFIRM_DELAY_S", 0.0)

    async def _fake_reprice(session) -> int:
        rows = (await session.execute(select(PositionModel).filter_by(status="OPEN"))).scalars().all()
        for row in rows:
            row.last_priced_at = _now()
        await session.commit()
        return len(rows)

    async def _fake_state(session, today=None):
        return (await session.execute(select(MarketStateModel))).scalars().first(), True

    monkeypatch.setattr(midday_exits, "refresh_position_values", _fake_reprice)
    monkeypatch.setattr(midday_exits, "refresh_market_state", _fake_state)
    return SimpleNamespace(proc=proc, stopped=stopped, tmp_path=tmp_path)


async def _run(session_maker, broker, *, now_et: datetime.datetime = MIDDAY) -> int:
    return await run_midday_exits(
        today=TODAY,
        broker_factory=lambda: broker,
        session_maker=session_maker,
        sleep=lambda s: None,
        now_et=lambda: now_et,
    )


async def _events(session_maker, event_type: str) -> list[dict]:
    async with session_maker() as session:
        rows = (await session.execute(select(AuditEventModel).filter_by(event_type=event_type))).scalars().all()
        return [r.payload for r in rows]


async def _orders(session_maker) -> list[OrderModel]:
    async with session_maker() as session:
        return list((await session.execute(select(OrderModel))).scalars().all())


async def _seed(session_maker, *rows) -> None:
    async with session_maker() as session:
        for row in rows:
            session.add(row)
        await session.commit()


class TestNewCloses:
    @pytest.mark.asyncio
    async def test_loss_limit_breach_submits_a_close(self, session_maker, pushes, gateway):
        # 2.5x the credit collected against a 2.0x limit — the B07 shape.
        await _seed(session_maker, _position(entry_premium=1.00, current_value=3.50))
        broker = FakeBroker()
        assert await _run(session_maker, broker) == 0
        assert len(broker.placed) == 1
        spread, ref = broker.placed[0]
        assert ref.endswith(":close")
        assert spread.net_limit_price == pytest.approx(-3.50)  # rung 0, no concession
        title, _, priority = pushes[-1]
        assert title == "basis midday exits: 1 close(s)"
        assert priority == "high"

    @pytest.mark.asyncio
    async def test_entry_candidates_are_never_touched(self, session_maker, pushes, gateway):
        await _seed(session_maker, _position(entry_premium=1.00, current_value=3.50))
        broker = FakeBroker()
        await _run(session_maker, broker)
        assert broker.forbidden_calls == []

    @pytest.mark.asyncio
    async def test_halt_entries_does_not_block_exits(self, session_maker, pushes, gateway):
        async with session_maker() as session:
            row = (await session.execute(select(TradingControlModel))).scalars().one()
            row.state = "HALT_ENTRIES"
            row.reason = "operator"
            await session.commit()
        await _seed(session_maker, _position(entry_premium=1.00, current_value=3.50))
        broker = FakeBroker()
        assert await _run(session_maker, broker) == 0
        assert len(broker.placed) == 1

    @pytest.mark.asyncio
    async def test_flatten_requested_is_left_to_the_nightly_ladder(self, session_maker, pushes, gateway):
        async with session_maker() as session:
            row = (await session.execute(select(TradingControlModel))).scalars().one()
            row.state = "FLATTEN_REQUESTED"
            row.reason = "operator"
            await session.commit()
        # A position with no P1 of its own: only the flatten could close it.
        await _seed(session_maker, _position(entry_premium=1.00, current_value=0.90))
        broker = FakeBroker()
        assert await _run(session_maker, broker) == 0
        assert broker.placed == []
        quiet = await _events(session_maker, MIDDAY_EXITS_QUIET)
        assert len(quiet) == 1
        assert any("ADR-0011" in note for note in quiet[0]["notes"])


class TestRestingExitReprice:
    @pytest.mark.asyncio
    async def test_unfilled_resting_exit_is_cancelled_and_reissued_once(self, session_maker, pushes, gateway):
        pos = _position(entry_premium=1.00, current_value=1.40)
        old = _close_order("o_old", limit_price=-1.18, rung=1)
        await _seed(session_maker, pos, old)
        broker = FakeBroker()
        broker.working = [SimpleNamespace(order_ref=old.order_ref, order_id=1, perm_id=None, status="Submitted")]

        assert await _run(session_maker, broker) == 0

        assert broker.cancelled == [old.order_ref]
        assert len(broker.placed) == 1
        spread, new_ref = broker.placed[0]
        assert new_ref != old.order_ref
        # Same rung (1 → 15% concession), re-marked to the CURRENT mid of 1.40.
        assert spread.net_limit_price == pytest.approx(-1.61)

        rows = await _orders(session_maker)
        by_ref = {o.order_ref: o for o in rows}
        assert by_ref[old.order_ref].status == "CANCELLED"
        assert by_ref[new_ref].status == "SUBMITTED"
        assert by_ref[new_ref].combo_legs[MIDDAY_REPRICE_OF_KEY] == old.order_ref
        assert by_ref[new_ref].combo_legs["rung"] == 1
        # The exit trigger travels with the replacement — the post-mortem
        # must still record why the position actually left.
        assert by_ref[new_ref].combo_legs["exit_trigger"] == "PROFIT_TARGET"

        # Never two live exits: exactly one pending close for the position.
        pending = [o for o in rows if o.action == "CLOSE" and o.status in ORDER_PENDING_STATUSES]
        assert len(pending) == 1
        # ...and exactly one live order at the broker.
        assert len({o.order_ref for o in broker.working}) == 1

    @pytest.mark.asyncio
    async def test_a_filled_resting_exit_is_left_alone(self, session_maker, pushes, gateway):
        pos = _position(entry_premium=1.00, current_value=1.40)
        old = _close_order("o_old", limit_price=-1.18, rung=1)
        await _seed(session_maker, pos, old)
        broker = FakeBroker()
        broker.working = []  # gone from the open-order book: it filled at the open

        assert await _run(session_maker, broker) == 0
        assert broker.cancelled == []
        assert broker.placed == []
        # Quiet: booking that fill is the evening sync's job, unchanged.
        assert len(await _events(session_maker, MIDDAY_EXITS_QUIET)) == 1
        rows = await _orders(session_maker)
        assert rows[0].status == "SUBMITTED"  # untouched

    @pytest.mark.asyncio
    async def test_an_unconfirmed_cancel_places_nothing(self, session_maker, pushes, gateway):
        pos = _position(entry_premium=1.00, current_value=1.40)
        old = _close_order("o_old", rung=0)
        await _seed(session_maker, pos, old)
        broker = FakeBroker()
        broker.cancel_leaves_order_open = True
        broker.working = [SimpleNamespace(order_ref=old.order_ref, order_id=1, perm_id=None, status="Submitted")]

        assert await _run(session_maker, broker) == 0
        assert broker.placed == []
        rows = await _orders(session_maker)
        assert rows[0].status == "SUBMITTED"  # left for the evening sync to verdict
        assert await _events(session_maker, midday_exits.MIDDAY_CANCEL_UNCONFIRMED)
        # ...and it is REPORTED. Lower harm than a rejected re-issue — the old
        # order is still live at the broker — but the pass could not do the
        # job it exists to do on this position, so it does not go quiet.
        title, body, priority = pushes[-1]
        assert "UNPROTECTED" in title and priority == "urgent"
        assert "CANCEL UNCONFIRMED" in body
        assert len(await _events(session_maker, MIDDAY_EXITS_DEGRADED)) == 1
        assert await _events(session_maker, MIDDAY_EXITS_QUIET) == []

    @pytest.mark.asyncio
    async def test_executions_discovered_during_the_cancel_stop_the_reissue(self, session_maker, pushes, gateway):
        pos = _position(entry_premium=1.00, current_value=1.40)
        old = _close_order("o_old", rung=0)
        await _seed(session_maker, pos, old)
        broker = FakeBroker()
        broker.working = [SimpleNamespace(order_ref=old.order_ref, order_id=1, perm_id=None, status="Submitted")]
        broker.execs = [SimpleNamespace(order_ref=old.order_ref, exec_id="e1")]

        assert await _run(session_maker, broker) == 0
        assert broker.placed == []
        assert await _events(session_maker, midday_exits.MIDDAY_EXIT_REPRICE_SKIPPED)

    @pytest.mark.asyncio
    async def test_a_stale_mark_blocks_the_reprice(self, session_maker, pushes, gateway, monkeypatch):
        pos = _position(entry_premium=1.00, current_value=1.40)
        old = _close_order("o_old", rung=0)
        await _seed(session_maker, pos, old)
        broker = FakeBroker()
        broker.working = [SimpleNamespace(order_ref=old.order_ref, order_id=1, perm_id=None, status="Submitted")]

        # Quotes unavailable: nothing repriced, so every mark stays old.
        async def _no_quotes(session) -> int:
            return 0

        monkeypatch.setattr(midday_exits, "refresh_position_values", _no_quotes)
        async with session_maker() as session:
            row = await session.get(PositionModel, "p1")
            row.last_priced_at = "2026-08-01T12:00:00+00:00"
            await session.commit()

        assert await _run(session_maker, broker) == 0
        assert broker.placed == []
        assert broker.cancelled == []
        assert await _events(session_maker, "STALE_MARK_CLOSE_SKIPPED")

    @pytest.mark.asyncio
    async def test_a_rejected_reissue_leaves_no_live_exit_and_says_so(self, session_maker, pushes, gateway):
        pos = _position(entry_premium=1.00, current_value=1.40)
        old = _close_order("o_old", rung=0)
        await _seed(session_maker, pos, old)
        broker = FakeBroker(close_exc=BrokerError("refused"))
        broker.working = [SimpleNamespace(order_ref=old.order_ref, order_id=1, perm_id=None, status="Submitted")]

        code = await _run(session_maker, broker)
        assert code == 0
        rows = {o.order_ref: o for o in await _orders(session_maker)}
        assert rows[old.order_ref].status == "CANCELLED"
        assert [o.status for o in rows.values() if o.order_ref != old.order_ref] == ["REJECTED"]
        # CLOSE_REJECTED, so anomaly.py's REPEATED_REJECTION counter sees it.
        assert await _events(session_maker, "CLOSE_REJECTED")
        # The position now has NO live exit for the rest of the session and
        # this position is at no P1, so _layer_a_closes places no replacement.
        # The pass MUST say so: silence here, recorded as MIDDAY_EXITS_QUIET,
        # is strictly worse than not having run at all (#960 review A).
        assert broker.working == []
        title, body, priority = pushes[-1]
        assert priority == "urgent"
        assert "UNPROTECTED" in title
        assert "REPRICE REJECTED" in body and "cancelled and NOT replaced" in body
        degraded = await _events(session_maker, MIDDAY_EXITS_DEGRADED)
        assert len(degraded) == 1
        assert degraded[0]["needs_attention"]
        assert await _events(session_maker, MIDDAY_EXITS_QUIET) == []
        assert await _events(session_maker, MIDDAY_EXITS_ACTED) == []

    @pytest.mark.asyncio
    async def test_the_two_halves_never_both_act_on_one_position(self, session_maker, pushes, gateway):
        # The invariant the design calls load-bearing, and the one thing this
        # module must never create. A position with a resting exit AND a mark
        # past a P1 threshold is the only shape where both halves want to act:
        # the reprice cancels and re-issues, and _layer_a_closes' pending-close
        # skip (#405) is what stops it staging a SECOND live close on the same
        # legs moments later.
        pos = _position(entry_premium=1.00, current_value=3.50)  # 2.5x: a loss-limit P1
        old = _close_order("o_old", rung=0)
        await _seed(session_maker, pos, old)
        broker = FakeBroker()
        broker.working = [SimpleNamespace(order_ref=old.order_ref, order_id=1, perm_id=None, status="Submitted")]

        assert await _run(session_maker, broker) == 0

        assert len(broker.placed) == 1  # the reprice, and nothing else
        assert len(broker.working) == 1  # exactly one live exit at the broker
        rows = await _orders(session_maker)
        pending = [o for o in rows if o.action == "CLOSE" and o.status in ORDER_PENDING_STATUSES]
        assert len(pending) == 1
        # ...and it was the pending-close skip that stopped the second one,
        # not some unrelated guard failing first.
        assert await _events(session_maker, "CLOSE_ALREADY_PENDING")

    @pytest.mark.asyncio
    async def test_a_sibling_exit_still_live_after_the_cancel_blocks_the_replacement(
        self, session_maker, pushes, gateway
    ):
        # The last gate before placement: re-read the open-order book after
        # the cancel rather than trusting the snapshot from the top of the
        # pass. A resting GTC :tp on the same legs is a live exit — placing
        # the replacement alongside it is the double-exit this pass exists to
        # avoid creating.
        pos = _position(entry_premium=1.00, current_value=1.40)
        old = _close_order("o_old", rung=0)
        tp = _close_order("o_tp", ref="basis:B01:o_e:open:tp")
        await _seed(session_maker, pos, old, tp)
        broker = FakeBroker()
        broker.working = [
            SimpleNamespace(order_ref=old.order_ref, order_id=1, perm_id=None, status="Submitted"),
            SimpleNamespace(order_ref=tp.order_ref, order_id=2, perm_id=None, status="Submitted"),
        ]

        assert await _run(session_maker, broker) == 0

        assert broker.cancelled == [old.order_ref]  # the :tp is not this pass's to cancel
        assert broker.placed == []
        skips = await _events(session_maker, midday_exits.MIDDAY_EXIT_REPRICE_SKIPPED)
        assert [s["reason"] for s in skips] == ["live exit still at the broker"]
        assert skips[0]["live_refs"] == [tp.order_ref]

    @pytest.mark.asyncio
    async def test_two_live_closes_on_one_position_are_never_added_to(self, session_maker, pushes, gateway):
        # Already broken: two live exits on one position. Do not make it
        # three — the evening sync and the resolution panel own this.
        pos = _position(entry_premium=1.00, current_value=1.40)
        first = _close_order("o_a", rung=0)
        second = _close_order("o_b", rung=1)
        await _seed(session_maker, pos, first, second)
        broker = FakeBroker()
        broker.working = [
            SimpleNamespace(order_ref=first.order_ref, order_id=1, perm_id=None, status="Submitted"),
            SimpleNamespace(order_ref=second.order_ref, order_id=2, perm_id=None, status="Submitted"),
        ]

        assert await _run(session_maker, broker) == 0

        assert broker.cancelled == []
        assert broker.placed == []
        skips = await _events(session_maker, midday_exits.MIDDAY_EXIT_REPRICE_SKIPPED)
        assert [s["reason"] for s in skips] == ["multiple live closes"]

    @pytest.mark.asyncio
    async def test_mismatched_leg_expirations_block_the_reprice(self, session_maker, pushes, gateway):
        # #761/#691: legs that do not all share pos.expiration_date (a B21
        # calendar's back leg genuinely outlives the front) are unreliable for
        # ANY automated close, re-marked or not — the same refusal
        # _settle_expired and _layer_a_closes already make, through the same
        # shared predicate.
        pos = _position(entry_premium=1.00, current_value=1.40, leg_expirations=(FAR_EXPIRY, "2027-01-15"))
        old = _close_order("o_old", rung=0)
        await _seed(session_maker, pos, old)
        broker = FakeBroker()
        # Both legs present at the broker, so no drift: the expiration
        # mismatch is the only thing that can stop this reprice.
        broker.broker_positions = [
            LegPosition(
                con_id=1, symbol="XSP", sec_type="OPT", position=-1.0, avg_cost=100.0, occ_symbol="XSP261218P00610000"
            ),
            LegPosition(
                con_id=2, symbol="XSP", sec_type="OPT", position=1.0, avg_cost=80.0, occ_symbol="XSP270115P00605000"
            ),
        ]
        broker.working = [SimpleNamespace(order_ref=old.order_ref, order_id=1, perm_id=None, status="Submitted")]

        assert await _run(session_maker, broker) == 0

        assert broker.cancelled == []
        assert broker.placed == []
        skips = await _events(session_maker, midday_exits.MIDDAY_EXIT_REPRICE_SKIPPED)
        assert [s["reason"] for s in skips] == ["mismatched leg expirations"]


class TestRungAccounting:
    """`rung` counts SESSIONS spent chasing a fill. A midday reprice is the
    same rung re-marked — if it counted, a repriced close would concede twice
    as fast and exhaust the ladder in half the evenings it was meant to have.
    """

    @pytest.mark.asyncio
    async def test_a_midday_replacement_does_not_advance_the_nightly_rung(self, session_maker, gateway):
        # Two genuine nightly attempts, plus one midday replacement of the
        # second. The next nightly close must be rung 2, not rung 3.
        pos = _position(position_id="p1", entry_premium=1.00, current_value=3.50)
        await _seed(
            session_maker,
            pos,
            _close_order("o_n1", status="CANCELLED", rung=0),
            _close_order("o_n2", status="CANCELLED", rung=1),
            _close_order("o_mid", status="CANCELLED", rung=1, repriced_from="basis:B01:o_n2:close"),
        )
        broker = FakeBroker()
        async with session_maker() as session:
            state = (await session.execute(select(MarketStateModel))).scalars().one()
            summary = ExecutorRunSummary(run_started_at=_now(), run_date=TODAY.isoformat())
            await _layer_a_closes(session, broker, state, summary, TODAY)
        submitted = await _events(session_maker, "CLOSE_SUBMITTED")
        assert len(submitted) == 1
        assert submitted[0]["rung"] == 2

    @pytest.mark.asyncio
    async def test_the_nightly_run_stamps_the_rung_it_priced_with(self, session_maker, gateway):
        await _seed(session_maker, _position(entry_premium=1.00, current_value=3.50))
        broker = FakeBroker()
        async with session_maker() as session:
            state = (await session.execute(select(MarketStateModel))).scalars().one()
            summary = ExecutorRunSummary(run_started_at=_now(), run_date=TODAY.isoformat())
            await _layer_a_closes(session, broker, state, summary, TODAY)
        rows = [o for o in await _orders(session_maker) if o.action == "CLOSE"]
        assert rows[0].combo_legs["rung"] == 0

    @pytest.mark.asyncio
    async def test_a_rejected_reissue_does_not_escalate_the_rung_in_the_same_pass(self, session_maker, pushes, gateway):
        # The rejected-reissue path is the one place a rung could advance
        # INTRADAY: the cancelled original is CANCELLED-with-submitted_at and
        # counts, the rejected replacement is excluded, so the fallback close
        # _layer_a_closes places moments later in the SAME pass would price a
        # rung deeper. spec/supervision.md says the midday pass re-marks at the
        # same rung and never advances it; this is what makes that true.
        pos = _position(entry_premium=1.00, current_value=3.50)  # a loss-limit P1
        old = _close_order("o_old", rung=0)
        await _seed(session_maker, pos, old)
        # Refuse the reprice, accept the fallback close.
        broker = FakeBroker(close_exc=BrokerError("refused"), close_exc_limit=1)
        broker.working = [SimpleNamespace(order_ref=old.order_ref, order_id=1, perm_id=None, status="Submitted")]

        assert await _run(session_maker, broker) == 0

        submitted = await _events(session_maker, "CLOSE_SUBMITTED")
        assert len(submitted) == 1
        assert submitted[0]["rung"] == 0  # not 1
        spread, _ref = broker.placed[-1]
        assert spread.net_limit_price == pytest.approx(-3.50)  # rung 0: no concession

    def test_the_cut_short_exclusion_lapses_once_a_later_close_has_rested(self):
        # Self-limiting, not permanent. Excluding the original forever would
        # shift this position's whole ladder down one rung on every future
        # evening — a nightly behaviour change wearing a midday fix's clothes.
        old = _close_order("o_old", status="CANCELLED", rung=0)
        old.submitted_at = "2026-08-21T22:45:00+00:00"
        old.completed_at = "2026-08-24T16:30:00+00:00"
        rejected = _close_order("o_mid", status="REJECTED", rung=0, repriced_from=old.order_ref, submitted=False)
        rejected.completed_at = "2026-08-24T16:30:01+00:00"
        assert executor._cut_short_by_a_failed_midday_reprice([old, rejected]) == {old.order_ref}
        # The same-pass fallback rests; from then on the original counts again.
        fallback = _close_order("o_fallback", status="CANCELLED", rung=0)
        fallback.submitted_at = "2026-08-24T16:31:00+00:00"
        assert executor._cut_short_by_a_failed_midday_reprice([old, rejected, fallback]) == set()


class TestRungFallback:
    """`_rung_of` prefers the rung stamped on the order — but the stamp ships
    in #960, so EVERY close order already in the live database will take the
    re-derivation fallback on its first midday reprice. That makes the
    fallback, not the stamped path, the code that actually runs first."""

    def test_falls_back_to_re_deriving_the_rung_when_none_is_stamped(self):
        # Three prior evenings; `old` is the third. Only the two that preceded
        # it count, so it was placed at rung 2.
        first = _close_order("o_1", status="CANCELLED", rung=None)
        first.submitted_at = "2026-08-20T22:45:00+00:00"
        second = _close_order("o_2", status="CANCELLED", rung=None)
        second.submitted_at = "2026-08-21T22:45:00+00:00"
        old = _close_order("o_3", rung=None)
        old.submitted_at = "2026-08-22T22:45:00+00:00"
        later = _close_order("o_4", status="CANCELLED", rung=None)
        later.submitted_at = "2026-08-25T22:45:00+00:00"  # after `old` — not a preceding rung
        assert midday_exits._rung_of(old, [first, second, old, later]) == 2

    def test_the_fallback_ignores_rejected_unsubmitted_tp_and_midday_rows(self):
        old = _close_order("o_real", rung=None)
        old.submitted_at = "2026-08-22T22:45:00+00:00"
        rejected = _close_order("o_rej", status="REJECTED", rung=None)
        rejected.submitted_at = "2026-08-20T22:45:00+00:00"
        never_sent = _close_order("o_staged", status="STAGED", rung=None, submitted=False)
        tp = _close_order("o_tp", status="CANCELLED", rung=None, ref="basis:B01:o_e:open:tp")
        tp.submitted_at = "2026-08-20T22:45:00+00:00"
        midday = _close_order("o_mid", status="CANCELLED", rung=None, repriced_from="basis:B01:o_x:close")
        midday.submitted_at = "2026-08-21T12:30:00+00:00"
        # A rung is a nightly SESSION at the market: a rejection never reached
        # the broker, a STAGED intent never rested, a TP never chased, and a
        # midday reprice is a re-mark of a rung rather than a new one.
        assert midday_exits._rung_of(old, [rejected, never_sent, tp, midday, old]) == 0

    def test_a_stamped_rung_wins_over_the_fallback(self):
        old = _close_order("o_real", rung=3)
        old.submitted_at = "2026-08-22T22:45:00+00:00"
        sibling = _close_order("o_1", status="CANCELLED", rung=None)
        sibling.submitted_at = "2026-08-20T22:45:00+00:00"
        # The recorded number is what the close was actually priced with; the
        # re-derivation would say 1 here and the stamp is the authority.
        assert midday_exits._rung_of(old, [sibling, old]) == 3


class TestDriftHalt:
    @pytest.mark.asyncio
    async def test_drift_halts_the_pass_and_places_nothing(self, session_maker, pushes, gateway):
        await _seed(session_maker, _position(entry_premium=1.00, current_value=3.50))
        broker = FakeBroker()
        # The books expect two short/long XSP legs; the broker holds neither.
        broker.broker_positions = [
            LegPosition(con_id=7, symbol="SPY", sec_type="STK", position=100.0, avg_cost=1.0, occ_symbol=None)
        ]
        assert await _run(session_maker, broker) == 0
        assert broker.placed == []
        halted = await _events(session_maker, MIDDAY_EXITS_HALTED)
        assert len(halted) == 1
        assert "RECONCILIATION_DRIFT" in halted[0]["reason"]
        title, _body, priority = pushes[-1]
        assert title == "basis midday exits: HALTED"
        assert priority == "urgent"

    @pytest.mark.asyncio
    async def test_this_mornings_unbooked_entry_fill_does_not_halt_the_pass(self, session_maker, pushes, gateway):
        # The routine case, and the one that would have made the pass useless:
        # expected leg quantities come from OPEN POSITIONS, and positions are
        # created by the EVENING sync — so a DAY entry that filled at today's
        # open is at the broker with nothing in the books expecting it. That
        # reads ORPHAN at 12:30 every single morning. #840's carve-out (drift
        # carried on a live STAGED/SUBMITTED order's legs) is what keeps it
        # from stopping the whole session.
        entry = OrderModel(
            id="o_entry",
            book_id="B01",
            position_id=None,
            order_ref="basis:B01:o_entry:open",
            ib_order_id=5,
            ib_perm_id=None,
            action="OPEN",
            combo_legs={"legs": [{"occ": "XSP261218P00600000"}, {"occ": "XSP261218P00595000"}]},
            order_type="LIMIT",
            limit_price=-1.0,
            decision_midpoint=-1.0,
            status="SUBMITTED",
            submitted_at=_now(),
            encumbered_risk=400.0,
        )
        await _seed(session_maker, _position(entry_premium=1.00, current_value=3.50), entry)
        broker = FakeBroker()
        broker.broker_positions = [
            *_broker_legs(),
            LegPosition(
                con_id=3,
                symbol="XSP",
                sec_type="OPT",
                position=-1.0,
                avg_cost=90.0,
                occ_symbol="XSP261218P00600000",
            ),
            LegPosition(
                con_id=4,
                symbol="XSP",
                sec_type="OPT",
                position=1.0,
                avg_cost=70.0,
                occ_symbol="XSP261218P00595000",
            ),
        ]
        assert await _run(session_maker, broker) == 0
        assert await _events(session_maker, MIDDAY_EXITS_HALTED) == []
        # ...and the unrelated position's loss-limit close still fires.
        assert len(broker.placed) == 1
        acted = await _events(session_maker, MIDDAY_EXITS_ACTED)
        assert any("pending tonight's sync" in note for note in acted[0]["notes"])

    @pytest.mark.asyncio
    async def test_an_unexplained_orphan_still_halts(self, session_maker, pushes, gateway):
        # Same shape, no pending order to explain it: this is real drift.
        await _seed(session_maker, _position(entry_premium=1.00, current_value=3.50))
        broker = FakeBroker()
        broker.broker_positions = [
            *_broker_legs(),
            LegPosition(
                con_id=3,
                symbol="XSP",
                sec_type="OPT",
                position=-1.0,
                avg_cost=90.0,
                occ_symbol="XSP261218P00600000",
            ),
        ]
        assert await _run(session_maker, broker) == 0
        assert broker.placed == []
        assert len(await _events(session_maker, MIDDAY_EXITS_HALTED)) == 1

    @pytest.mark.asyncio
    async def test_a_morning_fill_on_a_leg_another_book_holds_does_not_halt(self, session_maker, pushes, gateway):
        # Same-direction leg sharing across books is sanctioned (the netting
        # gate blocks OPPOSITE sides only). So when book B's entry fills this
        # morning onto an OCC book A already holds, the broker shows -2
        # against an expected -1 — PARTIAL_DRIFT, not ORPHAN. That is the same
        # unbooked-morning-fill story one classification over, and halting the
        # whole pass on it is the failure the #840 carve-out exists to prevent.
        entry = OrderModel(
            id="o_entry",
            book_id="B01",
            position_id=None,
            order_ref="basis:B01:o_entry:open",
            ib_order_id=5,
            ib_perm_id=None,
            action="OPEN",
            combo_legs={"legs": [{"occ": _occ(610.0)}, {"occ": _occ(595.0)}]},
            order_type="LIMIT",
            limit_price=-1.0,
            decision_midpoint=-1.0,
            status="SUBMITTED",
            submitted_at=_now(),
            encumbered_risk=400.0,
        )
        shared = _position(position_id="p1", entry_premium=1.00, current_value=3.50)
        other = _position(position_id="p2", entry_premium=1.00, current_value=3.50, strikes=(700.0, 695.0))
        await _seed(session_maker, shared, other, entry)
        broker = FakeBroker()
        broker.broker_positions = [
            # p1's short leg, now carrying the morning fill's contract too.
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-2.0, avg_cost=100.0, occ_symbol=_occ(610.0)),
            LegPosition(con_id=2, symbol="XSP", sec_type="OPT", position=1.0, avg_cost=80.0, occ_symbol=_occ(605.0)),
            # the morning fill's own long leg: an ordinary explained ORPHAN
            LegPosition(con_id=3, symbol="XSP", sec_type="OPT", position=1.0, avg_cost=70.0, occ_symbol=_occ(595.0)),
            *_broker_legs(strikes=(700.0, 695.0)),
        ]

        assert await _run(session_maker, broker) == 0

        assert await _events(session_maker, MIDDAY_EXITS_HALTED) == []
        # The shared leg is still never TRADED — p1 goes through the #407
        # close-skip — but the unrelated position's loss-limit close fires.
        skipped = await _events(session_maker, "CLOSE_SKIPPED_DRIFTED_LEGS")
        assert [s["position_id"] for s in skipped] == ["p1"]
        assert len(broker.placed) == 1
        _spread, ref = broker.placed[0]
        assert ref.startswith("basis:B01:")

    @pytest.mark.asyncio
    async def test_a_partial_drift_in_the_leg_missing_direction_still_halts(self, session_maker, pushes, gateway):
        # The other direction is the naked-short case the halt exists for, and
        # a pending order on the same legs must not buy it a pass: two books
        # expect +2 of the long leg and the broker holds +1, so a full-size
        # SELL on those legs is a bag the account cannot cover.
        entry = OrderModel(
            id="o_entry",
            book_id="B01",
            position_id=None,
            order_ref="basis:B01:o_entry:open",
            ib_order_id=5,
            ib_perm_id=None,
            action="OPEN",
            combo_legs={"legs": [{"occ": _occ(610.0)}, {"occ": _occ(605.0)}]},
            order_type="LIMIT",
            limit_price=-1.0,
            decision_midpoint=-1.0,
            status="SUBMITTED",
            submitted_at=_now(),
            encumbered_risk=400.0,
        )
        await _seed(
            session_maker,
            _position(position_id="p1", entry_premium=1.00, current_value=3.50),
            _position(position_id="p2", entry_premium=1.00, current_value=3.50),
            entry,
        )
        broker = FakeBroker()
        broker.broker_positions = [
            LegPosition(con_id=1, symbol="XSP", sec_type="OPT", position=-2.0, avg_cost=100.0, occ_symbol=_occ(610.0)),
            LegPosition(con_id=2, symbol="XSP", sec_type="OPT", position=1.0, avg_cost=80.0, occ_symbol=_occ(605.0)),
        ]

        assert await _run(session_maker, broker) == 0

        assert broker.placed == []
        halted = await _events(session_maker, MIDDAY_EXITS_HALTED)
        assert len(halted) == 1
        assert "PARTIAL_DRIFT" in halted[0]["reason"]

    @pytest.mark.asyncio
    async def test_drifted_legs_are_never_repriced(self, session_maker, pushes, gateway):
        # A resting close whose own legs the broker no longer holds: the
        # benign reading is "the exit filled and the sync hasn't booked it,"
        # the dangerous one is a real external close. Both say don't re-mark.
        pos = _position(entry_premium=1.00, current_value=1.40)
        old = _close_order("o_old", rung=0)
        await _seed(session_maker, pos, old)
        broker = FakeBroker()
        # Books expect both legs; the broker holds only the long one.
        broker.broker_positions = [_broker_legs()[1]]
        broker.working = [SimpleNamespace(order_ref=old.order_ref, order_id=1, perm_id=None, status="Submitted")]
        assert await _run(session_maker, broker) == 0
        assert broker.cancelled == []
        assert broker.placed == []

    @pytest.mark.asyncio
    async def test_a_colliding_gateway_tenant_halts_loudly(self, session_maker, pushes, gateway):
        from backend.run_lock import acquire_run_lock, release_run_lock

        other = acquire_run_lock("executor")
        try:
            broker = FakeBroker()
            assert await _run(session_maker, broker) == 0
            assert not broker.opened
            assert pushes[-1][0] == "basis midday exits: HALTED"
        finally:
            release_run_lock(other)

    @pytest.mark.asyncio
    async def test_an_unopenable_broker_session_halts(self, session_maker, pushes, gateway):
        broker = FakeBroker(open_exc=BrokerError("gateway said no"))
        assert await _run(session_maker, broker) == 0
        assert pushes[-1][0] == "basis midday exits: HALTED"
        assert "broker session failed to open" in pushes[-1][1]


class TestQuietPass:
    @pytest.mark.asyncio
    async def test_a_quiet_pass_writes_one_event_and_pushes_nothing(self, session_maker, pushes, gateway):
        await _seed(session_maker, _position(entry_premium=1.00, current_value=0.90))
        broker = FakeBroker()
        assert await _run(session_maker, broker) == 0
        assert broker.placed == []
        assert pushes == []
        assert len(await _events(session_maker, MIDDAY_EXITS_QUIET)) == 1
        assert await _events(session_maker, MIDDAY_EXITS_ACTED) == []

    @pytest.mark.asyncio
    async def test_a_holiday_does_nothing_at_all(self, session_maker, pushes, gateway):
        broker = FakeBroker()
        code = await run_midday_exits(
            today=datetime.date(2026, 12, 25),
            broker_factory=lambda: broker,
            session_maker=session_maker,
            sleep=lambda s: None,
            now_et=lambda: datetime.datetime(2026, 12, 25, 12, 30, tzinfo=MARKET_TZ),
        )
        assert code == 0
        assert not broker.opened
        assert pushes == []


class TestCharter:
    @pytest.mark.asyncio
    async def test_never_writes_the_executor_heartbeat(self, session_maker, pushes, gateway, monkeypatch):
        # The 22:00 dead-man watchdog exists to notice that the EVENING run
        # did not happen; a 12:30 pass stamping the heartbeat would pacify it
        # every single day.
        stamped: list = []
        monkeypatch.setattr(executor, "_write_heartbeat", lambda summary: stamped.append(summary))
        await _seed(session_maker, _position(entry_premium=1.00, current_value=3.50))
        await _run(session_maker, FakeBroker())
        assert stamped == []

    @pytest.mark.asyncio
    async def test_never_writes_a_reconciliation_run(self, session_maker, pushes, gateway):
        from backend.models import ReconciliationRunModel

        await _seed(session_maker, _position(entry_premium=1.00, current_value=3.50))
        await _run(session_maker, FakeBroker())
        async with session_maker() as session:
            rows = (await session.execute(select(ReconciliationRunModel))).scalars().all()
        assert rows == []

    @pytest.mark.asyncio
    async def test_never_books_a_fill_or_latches_control(self, session_maker, pushes, gateway):
        # "Never books fills, never latches control state" is a claim about a
        # path INSIDE _layer_a_closes, which this pass calls: its TP-cancel
        # race branch reaches _backfill_missed_fills (the sole FillModel
        # writer, which also debits commission) and _latch_partial (which ends
        # in set_control(HALT_ENTRIES)). The guarantee holds by an ORDERING
        # relationship — the #407 drift skip runs first — not by a gate, so it
        # is pinned here rather than merely asserted in prose.
        #
        # The shape that reaches for that branch: a GTC :tp that filled this
        # morning. Both legs are gone from the broker, the :tp explains them
        # away (so the pass does not halt), and the position is skipped.
        # A red here means the guard ordering or the charter has to move.
        pos = _position(entry_premium=1.00, current_value=3.50)  # a P1: the pass wants to act
        tp = _close_order("o_tp", ref="basis:B01:o_e:open:tp")
        await _seed(session_maker, pos, tp)
        broker = FakeBroker()
        broker.broker_positions = []  # the :tp took both legs off the books
        broker.working = [SimpleNamespace(order_ref=tp.order_ref, order_id=2, perm_id=None, status="Submitted")]
        broker.execs = [SimpleNamespace(order_ref=tp.order_ref, exec_id="e1")]

        assert await _run(session_maker, broker) == 0

        async with session_maker() as session:
            fills = (await session.execute(select(FillModel))).scalars().all()
            controls = (await session.execute(select(TradingControlModel))).scalars().all()
        assert fills == []
        assert [(c.scope, c.state, c.actor) for c in controls] == [("GLOBAL", "ACTIVE", "test")]
        assert broker.placed == []
        # ...and it was the drift skip that got there first.
        assert await _events(session_maker, "CLOSE_SKIPPED_DRIFTED_LEGS")

    def test_the_pass_is_a_registered_gateway_tenant(self):
        # Without this the nightly teardown's system-wide ibgateway sweep
        # would kill the midday pass's Gateway mid-order.
        assert midday_exits.LOCK_NAME in GATEWAY_TENANT_LOCKS


class TestMarketHoursWindow:
    """The task carries -StartWhenAvailable, so a machine asleep at 12:30 runs
    the pass whenever it wakes. This is the first task that is both
    time-sensitive and trade-mutating."""

    @pytest.mark.asyncio
    async def test_a_catch_up_run_after_the_close_refuses_and_says_so(self, session_maker, pushes, gateway):
        await _seed(session_maker, _position(entry_premium=1.00, current_value=3.50))
        broker = FakeBroker()

        code = await _run(session_maker, broker, now_et=datetime.datetime(2026, 8, 24, 16, 30, tzinfo=MARKET_TZ))

        assert code == 0
        assert not broker.opened  # no Gateway is launched at all
        assert broker.placed == []
        assert len(await _events(session_maker, MIDDAY_EXITS_OUT_OF_WINDOW)) == 1
        title, body, priority = pushes[-1]
        # `high`, not `urgent`: nothing is unprotected that was not already,
        # but a pass firing at the wrong hour is a scheduling defect to fix.
        assert (title, priority) == ("basis midday exits: OUT OF WINDOW", "high")
        assert "16:30" in body

    @pytest.mark.asyncio
    async def test_the_open_of_the_window_is_inclusive(self, session_maker, pushes, gateway):
        await _seed(session_maker, _position(entry_premium=1.00, current_value=3.50))
        broker = FakeBroker()
        code = await _run(session_maker, broker, now_et=datetime.datetime(2026, 8, 24, 9, 45, tzinfo=MARKET_TZ))
        assert code == 0
        assert await _events(session_maker, MIDDAY_EXITS_OUT_OF_WINDOW) == []
        assert len(broker.placed) == 1

    @pytest.mark.asyncio
    async def test_a_minute_past_the_close_of_the_window_refuses(self, session_maker, pushes, gateway):
        # The upper bound, pinned so an off-by-one on it cannot ship quietly:
        # 15:45 still runs (a close placed then has minutes to live and the
        # evening run is coming), 15:46 does not.
        await _seed(session_maker, _position(entry_premium=1.00, current_value=3.50))
        broker = FakeBroker()
        code = await _run(session_maker, broker, now_et=datetime.datetime(2026, 8, 24, 15, 46, tzinfo=MARKET_TZ))
        assert code == 0
        assert not broker.opened
        assert len(await _events(session_maker, MIDDAY_EXITS_OUT_OF_WINDOW)) == 1

    @pytest.mark.asyncio
    async def test_a_lost_out_of_window_report_is_not_success(self, session_maker, gateway, monkeypatch):
        monkeypatch.setattr(midday_exits, "send_ntfy_with_retry", lambda *a, **k: False)
        broker = FakeBroker()
        code = await _run(session_maker, broker, now_et=datetime.datetime(2026, 8, 24, 19, 5, tzinfo=MARKET_TZ))
        assert code == midday_exits.EXIT_PUSH_FAILED


class TestExitCodes:
    """A scheduled task watching the exit code must not read a lost report, a
    crash, or a refusal to start as success (preflight's contract, #840)."""

    @pytest.mark.asyncio
    async def test_a_lost_push_returns_exit_push_failed(self, session_maker, gateway, monkeypatch):
        monkeypatch.setattr(midday_exits, "send_ntfy_with_retry", lambda *a, **k: False)
        await _seed(session_maker, _position(entry_premium=1.00, current_value=3.50))
        broker = FakeBroker()
        code = await _run(session_maker, broker)
        assert len(broker.placed) == 1  # the work happened...
        assert code == midday_exits.EXIT_PUSH_FAILED  # ...but the operator never heard

    @pytest.mark.asyncio
    async def test_its_own_lock_being_held_aborts_without_trading(self, session_maker, pushes, gateway):
        from backend.run_lock import acquire_run_lock, release_run_lock

        held = acquire_run_lock(midday_exits.LOCK_NAME)
        try:
            await _seed(session_maker, _position(entry_premium=1.00, current_value=3.50))
            broker = FakeBroker()
            assert await _run(session_maker, broker) == 4
            assert not broker.opened
            assert broker.placed == []
            assert pushes == []  # a previous pass is live and doing this work
        finally:
            release_run_lock(held)

    def test_a_crash_alerts_and_returns_4(self, monkeypatch):
        from backend import run_logging

        monkeypatch.setattr(run_logging, "setup_run_logging", lambda name: None)

        async def _boom():
            raise RuntimeError("kaboom")

        alerts: list[tuple] = []
        monkeypatch.setattr(midday_exits, "run_midday_exits", _boom)
        monkeypatch.setattr(midday_exits, "alert_crash", lambda *args: alerts.append(args))
        assert midday_exits.main() == 4
        assert "kaboom" in alerts[0][1]


class TestComposePush:
    def test_quiet_composes_nothing(self):
        assert compose_midday_push(MiddayResult()) is None

    def test_halted_is_urgent(self):
        title, body, priority = compose_midday_push(MiddayResult(halted="drift"))
        assert (title, priority) == ("basis midday exits: HALTED", "urgent")
        assert "drift" in body

    def test_acted_lists_both_kinds(self):
        title, body, priority = compose_midday_push(
            MiddayResult(closes_placed=["r1"], repriced=["r2 -> r3"], skipped=["r4: filled"])
        )
        assert title == "basis midday exits: 1 close(s), 1 repriced"
        assert priority == "high"
        assert "Closed: r1" in body and "Repriced: r2 -> r3" in body and "Left alone: r4: filled" in body

    def test_a_pass_that_only_left_something_unprotected_is_never_quiet(self):
        # The push predicate asks "did this pass change the operator's
        # exposure or their expectations?", not "did it place an order?".
        title, body, priority = compose_midday_push(MiddayResult(needs_attention=["REPRICE REJECTED: r1"]))
        assert title == "basis midday exits: 1 UNPROTECTED"
        assert priority == "urgent"
        assert "ATTENTION: REPRICE REJECTED: r1" in body

    def test_attention_outranks_a_successful_close_in_the_same_pass(self):
        title, _body, priority = compose_midday_push(
            MiddayResult(closes_placed=["r1"], needs_attention=["REPRICE REJECTED: r2"])
        )
        assert title == "basis midday exits: 1 UNPROTECTED, 1 close(s)"
        assert priority == "urgent"

    def test_titles_are_ascii(self):
        for result in (
            MiddayResult(halted="x"),
            MiddayResult(closes_placed=["r"]),
            MiddayResult(needs_attention=["r"]),
        ):
            push = compose_midday_push(result)
            assert push is not None
            push[0].encode("ascii")
