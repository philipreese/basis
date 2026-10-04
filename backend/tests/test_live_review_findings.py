"""#1101: the independent review of the live executor, finding by finding,
end to end against the same temp live database and fake broker as
test_live_executor.py (its fixture and helpers are reused here)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from backend import live_executor as live
from backend.broker import FillInfo, LegPosition, RefState
from backend.live_executor import run_live_executor
from backend.models import AuditEventModel, BookModel, BookMtmHistoryModel, ShareHoldingModel, TradingControlModel
from backend.tests.test_live_executor import (
    B36_CONFIG,
    LIVE_ID,
    NEXT_DAY,
    SIGNAL_DAY,
    STAKE,
    LiveFakeBroker,
    _add_book,
    _config,
    _evening,
    _events,
    _held_book,
    _orders,
    _run,
    live_database,
)


@pytest_asyncio.fixture
async def maker(tmp_path, monkeypatch):
    async for m in live_database(tmp_path, monkeypatch):
        yield m


def _cost_with_commissions(placed: list[tuple]) -> float:
    """Each placed BUY at its limit plus the fake preview's 1.00 commission."""
    return sum(q * lim + 1.0 for _, side, q, lim, _ in placed if side == "BUY")


# ---------------------------------------------------------------------------
# Defect 1: one account, one cash figure, across books
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("transmit", [True, False])
async def test_two_live_books_share_one_broker_cash_figure(maker, monkeypatch, transmit):
    # TotalCashValue is one number for the account. Each book used to read it
    # fresh, so two books could each spend all of it. With cash for about one
    # book's buys, the second book gets only what is left.
    monkeypatch.setenv("BASIS_LIVE_STAKE_B37", str(STAKE))
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await _add_book(session, "B37", B36_CONFIG)
        await session.commit()
    broker_cash = STAKE + 100.0
    broker = LiveFakeBroker(cash=broker_cash)
    summary = await _run(maker, broker, config=_config(transmit))
    # Each book alone would spend close to the whole stake — the two together
    # would be about twice the cash available.
    if transmit:
        assert {ref.split(":")[1] for *_, ref in broker.placed} >= {"B36"}
        assert _cost_with_commissions(broker.placed) <= broker_cash
    else:
        assert broker.placed == []
        would = [p for p in broker.previews if p[1] == "BUY"]
        assert sum(q * lim + 1.0 for _, _, q, lim in would) <= broker_cash
        assert any(w.startswith("B36 ") for w in summary.would_place)


@pytest.mark.asyncio
async def test_without_the_shared_figure_two_books_would_overspend(maker, monkeypatch):
    # The control for the test above: with cash for both, both books buy —
    # so the cap there is the shared figure, not an accident of sizing.
    monkeypatch.setenv("BASIS_LIVE_STAKE_B37", str(STAKE))
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await _add_book(session, "B37", B36_CONFIG)
        await session.commit()
    broker = LiveFakeBroker(cash=100_000.0)
    await _run(maker, broker)
    assert {ref.split(":")[1] for *_, ref in broker.placed} == {"B36", "B37"}
    assert _cost_with_commissions(broker.placed) > STAKE + 100.0


# ---------------------------------------------------------------------------
# Defect 5: a placement timeout / dropped connection; reruns
# ---------------------------------------------------------------------------


class InterruptingBroker(LiveFakeBroker):
    """Placement attempt number *fail_at* (1-based, 0 = never) raises
    *error*, as a hung or dropped Gateway does on placeOrder's round trip."""

    def __init__(self, fail_at: int, error: Exception, **kw):
        super().__init__(**kw)
        self.fail_at = fail_at
        self.error = error
        self.attempts = 0

    def place_share_order(self, symbol, side, quantity, limit_price, ref):
        self.attempts += 1
        if self.attempts == self.fail_at:
            raise self.error
        return super().place_share_order(symbol, side, quantity, limit_price, ref)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), ConnectionError("Socket disconnect")])
async def test_a_placement_timeout_finishes_the_digest_and_a_rerun_places_nothing_twice(maker, error):
    # It used to crash the run with no digest. Now the run closes out: urgent,
    # audited, the placed order listed, the timed-out one left STAGED (it may
    # have reached IBKR), and nothing else placed.
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await session.commit()
    broker = InterruptingBroker(fail_at=2, error=error)
    summary = await _run(maker, broker)
    assert len(broker.placed) == 1 and broker.attempts == 2  # the third order never tried
    first_ref = broker.placed[0][4]
    assert summary.placed == [first_ref]
    assert any("STOPPED mid-run" in u for u in summary.urgent)
    _title, body, priority = live.compose_live_digest(summary)
    assert priority == "urgent" and first_ref in body
    orders = {o.order_ref: o.status for o in await _orders(maker)}
    assert sorted(orders.values()) == ["STAGED", "SUBMITTED"]
    staged = next(r for r, s in orders.items() if s == "STAGED")
    assert any(staged in u for u in summary.urgent)
    interrupted = await _events(maker, live.LIVE_RUN_INTERRUPTED)
    assert len(interrupted) == 1 and interrupted[0].payload["outcome_unknown"] == [staged]

    # A restart the same evening: the placed order is working, the timed-out
    # one is unknown at the broker. Nothing is placed a second time.
    broker.fail_at = 0
    broker.ref_states = {first_ref: RefState.OPEN}
    attempts = broker.attempts
    await _run(maker, broker)
    assert broker.attempts == attempts
    assert len(await _orders(maker)) == 2


@pytest.mark.asyncio
async def test_an_interrupted_buy_phase_is_never_bought_twice(maker):
    # The buy phase's own guard: a rebalance BUY row for the signal already
    # exists (placed, or STAGED by an interrupted run) -> no second batch.
    await _held_book(maker)
    broker = InterruptingBroker(fail_at=0, error=ConnectionError("drop"))
    broker.position_rows = [LegPosition(9, "SCHF", "STK", 100.0, 28.0)]
    night1 = await _run(maker, broker)
    sell_ref = night1.placed[0]
    broker.ref_states = {sell_ref: RefState.FILLED}
    broker.execution_rows = [FillInfo("x1", 9, "SLD", 100.0, 28.0, sell_ref, 1.0, "2026-11-02T14:31:00+00:00")]
    broker.position_rows = []
    broker.placed.clear()
    broker.fail_at = broker.attempts + 2  # the second buy drops the connection
    night2 = await _run(maker, broker, day=NEXT_DAY)
    assert len(broker.placed) == 1 and any("STOPPED mid-run" in u for u in night2.urgent)
    assert len([o for o in await _orders(maker) if o.side == "BUY"]) == 2  # SUBMITTED + STAGED

    broker.fail_at = 0
    broker.ref_states = {sell_ref: RefState.FILLED, broker.placed[0][4]: RefState.OPEN}
    attempts = broker.attempts
    rerun = await _run(maker, broker, day=NEXT_DAY)
    assert broker.attempts == attempts
    assert any("already exist" in n for n in rerun.notes)
    assert len([o for o in await _orders(maker) if o.side == "BUY"]) == 2


# ---------------------------------------------------------------------------
# Policy: the remote-HALT channel must be heard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("transmit", [True, False])
async def test_an_unreadable_remote_halt_channel_places_nothing(maker, monkeypatch, transmit):
    from backend.trading_control import NtfyPollFailed

    monkeypatch.setattr(live, "apply_ntfy_commands", AsyncMock(side_effect=NtfyPollFailed("the poll failed")))
    await _held_book(maker)
    async with maker() as session:
        (await session.get(TradingControlModel, "B36")).state = "FLATTEN_REQUESTED"
        await session.commit()
    broker = LiveFakeBroker()
    broker.position_rows = [LegPosition(9, "SCHF", "STK", 100.0, 28.0)]
    summary = await _run(maker, broker, config=_config(transmit))
    assert broker.placed == [] and broker.previews == [] and summary.would_place == []
    assert any("remote-HALT channel unreadable" in u for u in summary.urgent)
    assert len(await _events(maker, live.LIVE_NTFY_UNREADABLE)) == 1
    assert (await _events(maker, live.LIVE_RUN_SUMMARY))[0].payload["orders_allowed"] is False
    live.run_post_session_anomalies.assert_awaited_once()  # the sweep still runs
    live.apply_ntfy_commands.assert_awaited_once()
    assert live.apply_ntfy_commands.await_args.kwargs == {"strict": True}


# ---------------------------------------------------------------------------
# Policy: an armed flatten BUY spends cash like any buy
# ---------------------------------------------------------------------------


async def _short_book(m, holding: int, *, grant: bool = True):
    async with m() as session:
        await _add_book(session, "B36", B36_CONFIG, cash=5_000.0, grant=grant, authority="LIVE" if grant else "PAPER")
        session.add(ShareHoldingModel(book_id="B36", symbol="SCHF", quantity=float(holding), updated_at="t0"))
        (await session.get(TradingControlModel, "B36")).state = "FLATTEN_REQUESTED"
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("holding", "broker_cash", "grant", "fragment"),
    [
        (-10, 50.0, True, "no borrowing"),  # the no-debit rule
        (-200, 100_000.0, True, "exceeds the stake"),  # the stake cap
        (-10, 100_000.0, False, "cover by hand"),  # no grant, no stake to cap at
    ],
)
async def test_an_armed_flatten_buy_is_capped_and_cannot_debit(maker, holding, broker_cash, grant, fragment):
    await _short_book(maker, holding, grant=grant)
    broker = LiveFakeBroker(cash=broker_cash)
    broker.position_rows = [LegPosition(9, "SCHF", "STK", float(holding), 28.0)]
    summary = await _run(maker, broker, day=NEXT_DAY)
    assert broker.placed == []
    assert any("FLATTEN refused" in u and fragment in u for u in summary.urgent)
    assert [o.status for o in await _orders(maker)] == ["REJECTED"]
    assert len(await _events(maker, live.LIVE_FLATTEN_BUY_REFUSED)) == 1
    assert str(STAKE) not in " ".join(summary.urgent)


@pytest.mark.asyncio
async def test_an_armed_flatten_buy_within_stake_and_cash_is_placed(maker):
    await _short_book(maker, -10)
    broker = LiveFakeBroker(cash=10_000.0)
    broker.position_rows = [LegPosition(9, "SCHF", "STK", -10.0, 28.0)]
    summary = await _run(maker, broker, day=NEXT_DAY)
    assert [(s, side, q) for s, side, q, _, _ in broker.placed] == [("SCHF", "BUY", 10)]
    assert not any("FLATTEN refused" in u for u in summary.urgent)


def test_book_of_share_ref_round_trips_and_fails_closed():
    from backend.share_book import share_order_ref

    assert live.book_of_share_ref(share_order_ref("B36", "abc")) == "B36"
    for bad in ("", "basis:B36:abc:open", "other:B36:abc:share", "basis::abc:share", "basis:B36:share"):
        assert live.book_of_share_ref(bad) is None


# ---------------------------------------------------------------------------
# Test gap: the -30% halt through the REAL anomaly sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("breach", [True, False])
async def test_a_thirty_percent_breach_stages_nothing_through_the_real_sweep(maker, monkeypatch, breach):
    # On a signal day with an all-cash month: the control case (no breach)
    # places the month's buys; the breach case stages nothing the same night.
    from backend import anomaly

    monkeypatch.setattr(live, "run_post_session_anomalies", anomaly.run_post_session_anomalies)
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG, cash=10_000.0 - (STAKE * 0.35 if breach else 0.0))
        (await session.get(BookMtmHistoryModel, ("B36", "2026-10-19"))).mtm = 10_000.0  # the grant's baseline
        await session.commit()
    broker = LiveFakeBroker()
    summary = await _run(maker, broker)
    async with maker() as session:
        book = await session.get(BookModel, "B36")
        control = await session.get(TradingControlModel, "B36")
    if breach:
        assert broker.placed == [] and broker.previews == []
        assert await _orders(maker) == []
        assert any("STAKE_DRAWDOWN_HALT" in u for u in summary.urgent)
        assert book.live_authority == "REVOKED" and control.state == "HALT_ENTRIES"
    else:
        assert broker.placed, "the control case must trade, or the breach case proves nothing"
        assert book.live_authority == "LIVE"


# ---------------------------------------------------------------------------
# Test gap: a dry run through the real, transmit-locked BrokerSession
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_through_the_real_transmit_locked_session(maker, monkeypatch):
    # The other dry-run tests use a fake broker that ignores the lock. This
    # runs the real BrokerSession (faked only at ib_async), built by the real
    # factory from a dry-run config, and proves the session itself refuses a
    # placement.
    from backend import broker as broker_mod
    from backend.broker import TransmitNotArmedError
    from backend.tests.test_broker import FakeIB

    fake = FakeIB(accounts=(LIVE_ID,))
    fake.what_if_state = SimpleNamespace(
        initMarginChange="30.0",
        maintMarginChange="30.0",
        equityWithLoanAfter="50000.0",
        initMarginAfter="200.0",
        minCommission=1.0,
        maxCommission=1.0,
        warningText="",
    )

    async def cash_rows(account=""):
        return [SimpleNamespace(account=LIVE_ID, tag="TotalCashValue", value="100000", currency="USD")]

    fake.accountSummaryAsync = cash_rows
    monkeypatch.setattr(broker_mod, "_default_ib_factory", lambda: fake)
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await session.commit()
    probed: list[bool] = []
    original = live._rebalance_live_book

    async def probe(session, broker, *args, **kwargs):
        await original(session, broker, *args, **kwargs)
        with pytest.raises(TransmitNotArmedError):
            broker.place_share_order("SCHB", "BUY", 1, 30.0, "basis:B36:probe:share")
        probed.append(True)

    monkeypatch.setattr(live, "_rebalance_live_book", probe)
    summary = await run_live_executor(
        _config(transmit=False),
        session_maker=maker,
        broker_factory=live.default_broker_factory,
        today=SIGNAL_DAY,
        now=_evening(SIGNAL_DAY),
        gateway_probe=lambda host, port: True,
    )
    assert probed == [True]
    assert fake.placed == []
    assert summary.would_place and summary.placed == []
    assert len(await _events(maker, live.LIVE_DRY_RUN_ORDER)) == len(summary.would_place)
    assert await _orders(maker) == []


# ---------------------------------------------------------------------------
# Policy: operator cash credits are not P&L
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_cash_credits_never_raise_live_sizing(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG, cash=15_000.0)
        (await session.get(BookMtmHistoryModel, ("B36", "2026-10-19"))).mtm = 10_000.0
        for run_at, event, payload in (
            ("2026-10-21T15:00:00+00:00", "RESOLUTION_CASH_ADJUSTED", {"delta": 5_000.0}),
            ("2026-10-10T15:00:00+00:00", "RESOLUTION_CASH_ADJUSTED", {"delta": 900.0}),  # before the window
            ("2026-10-22T15:00:00+00:00", "RESOLUTION_SHARE_HOLDING_CORRECTED", {"cash_delta": -500.0}),
            ("2026-10-22T16:00:00+00:00", "RESOLUTION_CASH_ADJUSTED", {"delta": "junk"}),
        ):
            session.add(AuditEventModel(run_at=run_at, book_id="B36", event_type=event, actor="t", payload=payload))
        await session.commit()
        book = await session.get(BookModel, "B36")
        assert await live.operator_cash_credits(session, "B36", "2026-10-20") == 5_000.0
        # equity 14,500 after the debit: stake + (14,500 - 10,000) - the 5,000 credit.
        investable = await live._live_investable(session, book, STAKE, 14_500.0)
    assert investable == STAKE - 500.0
