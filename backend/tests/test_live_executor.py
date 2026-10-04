"""The live executor (#1065) end to end against a temp live-stamped
database and a fake live broker. Fail-closed paths first: every refusal
places nothing, and a dry run never calls place_share_order."""

import datetime
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import database
from backend import live_executor as live
from backend.broker import (
    ConnectionFailedError,
    FillInfo,
    LegPosition,
    LiveAccountRequiredError,
    PlacedOrder,
    PreviewRejectedError,
    ReconcileReport,
    RefState,
    SharePreview,
)
from backend.dates import MARKET_TZ
from backend.etf_trend import month_end_dates
from backend.live_executor import LiveConfig, LiveRefusal, resolve_live_config, run_live_executor
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    BookMtmHistoryModel,
    DbMetaModel,
    IndexHistoryModel,
    LiveGrantModel,
    ShareHoldingModel,
    ShareOrderModel,
    TradingControlModel,
)

SIGNAL_DAY = datetime.date(2026, 10, 30)
NEXT_DAY = datetime.date(2026, 11, 2)
LATER_DAY = datetime.date(2026, 11, 3)
MENU = ["SCHB", "SCHF", "UTEN", "IAUM", "SCHH", "DBMF"]
CLOSES = {"SCHB": 30.0, "SCHF": 28.0, "UTEN": 40.0, "IAUM": 41.0, "SCHH": 22.0, "DBMF": 33.0, "TBIL": 50.0}
STAKE = 3000.0  # synthetic; in live mode it arrives through BASIS_LIVE_STAKE_<id> (#1098)
B36_CONFIG = {
    "envelope": {},
    "share_symbols": [*MENU, "TBIL"],
    "etf_trend": {"menu": MENU, "cash_symbol": "TBIL", "trend_months": 10},
}
OPTIONS_CONFIG = {"envelope": {}, "underlying": "XSP"}
LIVE_ID = "U0000000"  # synthetic


LIVE_INI = "C:/IBC/live/config.ini"


def _resolve(env, base_env, **kwargs):
    """resolve_live_config with the paper processes reading the same overlay
    the live process loaded (the normal case: both read `.env.live`)."""
    env = {"IBC_LIVE_INI": LIVE_INI, **env}
    kwargs.setdefault("paper_view_of_overlay", env)
    return resolve_live_config(env, base_env, **kwargs)


def _evening(day: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(19, 30), tzinfo=MARKET_TZ)


def _config(transmit: bool = True) -> LiveConfig:
    return LiveConfig(LIVE_ID, "127.0.0.1", 4001, 17, "live.bat", armed=transmit, dry_run_requested=False)


class LiveFakeBroker:
    def __init__(self, cash: float = 100_000.0):
        self.cash = cash
        self.ref_states: dict[str, RefState] = {}
        self.execution_rows: list[FillInfo] = []
        self.position_rows: list[LegPosition] = []
        self.previews: list[tuple] = []
        self.placed: list[tuple] = []
        self.preview_errors: dict[str, Exception] = {}
        self.preview_result = SharePreview(10.0, 10.0, 50_000.0, 1_000.0, 1.0, 1.0)
        self.open_error: Exception | None = None
        self.opened = False
        self._next = 500

    def open(self):
        if self.open_error:
            raise self.open_error
        self.opened = True

    def close(self):
        pass

    def reconcile(self, refs, since=None):
        return ReconcileReport(states={r: self.ref_states.get(r, RefState.UNKNOWN) for r in refs})

    def executions(self, since=None):
        return list(self.execution_rows)

    def positions(self):
        return list(self.position_rows)

    def open_orders(self):
        return []

    def account_cash(self):
        if isinstance(self.cash, Exception):
            raise self.cash
        return self.cash

    def preview_share_order(self, symbol, side, quantity, limit_price):
        self.previews.append((symbol, side, quantity, limit_price))
        if symbol in self.preview_errors:
            raise self.preview_errors[symbol]
        return self.preview_result

    def place_share_order(self, symbol, side, quantity, limit_price, ref):
        self._next += 1
        self.placed.append((symbol, side, quantity, limit_price, ref))
        return PlacedOrder(order_id=self._next, perm_id=None, ref=ref, status="PreSubmitted")


async def _add_book(session, book_id, config, *, authority="LIVE", cash=10_000.0, grant=True, hash_=None):
    config_hash = hash_ or f"hash-{book_id}"
    session.add(
        BookModel(
            id=book_id,
            name=book_id,
            config=config,
            config_version=1,
            config_hash=config_hash,
            starting_capital=10_000.0,
            cash_balance=cash,
            status="ACTIVE",
            created_at="2026-09-01T00:00:00+00:00",
            live_authority=authority,
            promoted_at="2026-10-20T23:00:00+00:00" if authority == "LIVE" else None,
            demotion_policy_version=1 if authority == "LIVE" else None,
        )
    )
    session.add(TradingControlModel(scope=book_id, state="ACTIVE", reason="", actor="t", changed_at="t0"))
    session.add(BookMtmHistoryModel(book_id=book_id, date="2026-10-19", mtm=cash))
    if grant:
        session.add(
            LiveGrantModel(
                book_id=book_id,
                kind="STAGE1",
                granted_at="2026-10-20T23:00:00+00:00",
                as_raced_config_hash=f"hash-{book_id}",
                config_snapshot=config,
                stake=STAKE,
                demotion_policy_version=1,
                attestation="operator signed off on the stage-1 bar",
            )
        )


@pytest_asyncio.fixture
async def maker(tmp_path, monkeypatch):
    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(live, "TRADING_MODE", "live")
    monkeypatch.setattr(database, "TRADING_MODE", "live")  # resolve_for_book reads the private stake
    monkeypatch.setenv("BASIS_LIVE_STAKE_B36", str(STAKE))
    monkeypatch.setenv("BASIS_LIVE_STAKE_B01", str(STAKE))
    monkeypatch.setattr(live, "persist_index_history", AsyncMock(return_value=0))
    monkeypatch.setattr(live, "apply_ntfy_commands", AsyncMock(return_value=0))
    monkeypatch.setattr(live, "run_post_session_anomalies", AsyncMock(return_value=[]))
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'live.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with m() as session:
        session.add(DbMetaModel(key="trading_mode", value="live"))
        session.add(TradingControlModel(scope="GLOBAL", state="ACTIVE", reason="", actor="t", changed_at="t0"))
        for symbol, close in CLOSES.items():
            dates = month_end_dates(SIGNAL_DAY, 10)
            for i, d in enumerate(dates):
                # SCHB and IAUM rise into the signal; the rest fall.
                step = close * 0.01 * (1 if symbol in ("SCHB", "IAUM") else -1)
                session.add(IndexHistoryModel(date=d.isoformat(), symbol=symbol, close=close - step * (9 - i)))
            for d in (NEXT_DAY, LATER_DAY):
                session.add(IndexHistoryModel(date=d.isoformat(), symbol=symbol, close=close))
        await session.commit()
    yield m
    await engine.dispose()


async def _run(m, broker, day=SIGNAL_DAY, config=None, rehearse=False, gateway_up=True):
    return await run_live_executor(
        config or _config(),
        session_maker=m,
        broker_factory=lambda c: broker,
        today=day,
        now=_evening(day),
        rehearse=rehearse,
        gateway_probe=lambda host, port: gateway_up,
    )


async def _events(m, event_type):
    async with m() as session:
        return list((await session.execute(select(AuditEventModel).filter_by(event_type=event_type))).scalars().all())


async def _orders(m):
    async with m() as session:
        return list((await session.execute(select(ShareOrderModel))).scalars().all())


# ---------------------------------------------------------------------------
# Run-level refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refuses_in_paper_mode(maker, monkeypatch):
    monkeypatch.setattr(live, "TRADING_MODE", "paper")
    broker = LiveFakeBroker()
    with pytest.raises(LiveRefusal, match="live mode"):
        await _run(maker, broker)
    assert broker.opened is False


@pytest.mark.asyncio
async def test_refuses_a_database_stamped_paper(maker):
    async with maker() as session:
        (await session.get(DbMetaModel, "trading_mode")).value = "paper"
        await session.commit()
    broker = LiveFakeBroker()
    with pytest.raises(LiveRefusal, match="not stamped live"):
        await _run(maker, broker)
    assert broker.opened is False


@pytest.mark.asyncio
async def test_refuses_an_unstamped_database(maker):
    async with maker() as session:
        await session.delete(await session.get(DbMetaModel, "trading_mode"))
        await session.commit()
    with pytest.raises(LiveRefusal):
        await _run(maker, LiveFakeBroker())


@pytest.mark.asyncio
async def test_refuses_during_the_market_session(maker):
    broker = LiveFakeBroker()
    with pytest.raises(LiveRefusal, match="session is in progress"):
        await run_live_executor(
            _config(),
            session_maker=maker,
            broker_factory=lambda c: broker,
            today=SIGNAL_DAY,
            now=datetime.datetime.combine(SIGNAL_DAY, datetime.time(11, 0), tzinfo=MARKET_TZ),
        )
    assert broker.opened is False


@pytest.mark.asyncio
async def test_rehearsal_refuses_when_armed(maker):
    with pytest.raises(LiveRefusal, match="dry-run only"):
        await _run(maker, LiveFakeBroker(), rehearse=True)


@pytest.mark.asyncio
async def test_holiday_does_nothing(maker):
    broker = LiveFakeBroker()
    summary = await _run(maker, broker, day=datetime.date(2026, 10, 31))
    assert broker.opened is False
    assert any("MARKET HOLIDAY" in n for n in summary.notes)


@pytest.mark.asyncio
async def test_account_guard_refusal_is_audited_and_nothing_runs(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await session.commit()
    broker = LiveFakeBroker()
    broker.open_error = LiveAccountRequiredError("the connected account is a paper (D-prefixed) account")
    summary = await _run(maker, broker)
    assert summary.broker_ok is False
    assert broker.placed == [] and broker.previews == []
    assert len(await _events(maker, live.LIVE_BROKER_UNAVAILABLE)) == 1


# ---------------------------------------------------------------------------
# Book eligibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_book_with_live_authority_is_refused_never_traded(maker):
    async with maker() as session:
        await _add_book(session, "B01", OPTIONS_CONFIG)
        await session.commit()
    broker = LiveFakeBroker()
    summary = await _run(maker, broker)
    assert broker.placed == [] and broker.previews == []
    assert any("B01" in u and "options book" in u for u in summary.urgent)
    assert len(await _events(maker, live.LIVE_BOOK_REFUSED)) == 1


@pytest.mark.asyncio
async def test_share_book_without_private_stake_is_refused(maker, monkeypatch):
    monkeypatch.delenv("BASIS_LIVE_STAKE_B36")
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await session.commit()
    broker = LiveFakeBroker()
    summary = await _run(maker, broker)
    assert broker.placed == [] and broker.previews == []
    assert any("no private live stake (BASIS_LIVE_STAKE_B36" in u for u in summary.urgent)


@pytest.mark.asyncio
async def test_seeded_stake_in_live_mode_is_refused(maker):
    # #1098: the live stake must be private; a seeded one is a config bug.
    async with maker() as session:
        await _add_book(session, "B36", {**B36_CONFIG, "stage1_stake": STAKE})
        await session.commit()
    broker = LiveFakeBroker()
    with pytest.raises(LiveRefusal, match="B36 carry a seeded stage1_stake"):
        await _run(maker, broker)
    assert broker.opened is False


@pytest.mark.asyncio
async def test_changed_private_stake_diverges_and_halts_the_book(maker, monkeypatch):
    # #1098 / ADR-0014 point 4: the grant row pins the stake. A different
    # overlay stake is caught like a config-hash divergence.
    monkeypatch.setenv("BASIS_LIVE_STAKE_B36", str(STAKE * 2))
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await session.commit()
    broker = LiveFakeBroker()
    summary = await _run(maker, broker)
    assert broker.placed == [] and broker.previews == []
    refusal = next(u for u in summary.urgent if "differs from the grant's stake" in u)
    assert str(STAKE) not in refusal and str(int(STAKE)) not in refusal  # never the value
    async with maker() as session:
        assert (await session.get(TradingControlModel, "B36")).state == "HALT_ENTRIES"
    assert len(await _events(maker, live.LIVE_HASH_DIVERGENCE)) == 1


@pytest.mark.asyncio
async def test_book_without_live_authority_is_ignored(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG, authority="PAPER", grant=False)
        await session.commit()
    broker = LiveFakeBroker()
    summary = await _run(maker, broker)
    assert broker.placed == [] and broker.previews == []
    assert summary.urgent == []
    assert any("no live book is eligible" in n for n in summary.notes)


@pytest.mark.asyncio
async def test_revoked_book_is_ignored(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG, authority="REVOKED")
        await session.commit()
    broker = LiveFakeBroker()
    await _run(maker, broker)
    assert broker.placed == []


@pytest.mark.asyncio
async def test_live_book_without_a_grant_is_refused(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG, grant=False)
        await session.commit()
    broker = LiveFakeBroker()
    summary = await _run(maker, broker)
    assert broker.placed == []
    assert any("no recorded live grant" in u for u in summary.urgent)


@pytest.mark.asyncio
async def test_hash_divergence_refuses_and_halts_the_book(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG, hash_="hash-edited")
        await session.commit()
    broker = LiveFakeBroker()
    summary = await _run(maker, broker)
    assert broker.placed == []
    assert any("as-raced" in u for u in summary.urgent)
    async with maker() as session:
        assert (await session.get(TradingControlModel, "B36")).state == "HALT_ENTRIES"
    assert len(await _events(maker, live.LIVE_HASH_DIVERGENCE)) == 1


@pytest.mark.asyncio
async def test_hash_divergence_never_downgrades_a_flatten(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG, hash_="hash-edited")
        (await session.get(TradingControlModel, "B36")).state = "FLATTEN_REQUESTED"
        await session.commit()
    await _run(maker, LiveFakeBroker())
    async with maker() as session:
        assert (await session.get(TradingControlModel, "B36")).state == "FLATTEN_REQUESTED"


def test_judge_rejects_broken_records(monkeypatch):
    monkeypatch.setattr(database, "TRADING_MODE", "live")
    monkeypatch.setenv("BASIS_LIVE_STAKE_B36", str(STAKE))
    book = BookModel(
        id="B36",
        config={"envelope": {"nope": 1}},
        config_hash="h",
        status="ACTIVE",
        live_authority="LIVE",
        promoted_at="x",
        demotion_policy_version=1,
    )
    assert "does not resolve" in live.judge_live_book(book, None).reason
    book.config = B36_CONFIG
    book.status = "RETIRED"
    assert "not ACTIVE" in live.judge_live_book(book, None).reason
    book.status = "ACTIVE"
    book.promoted_at = None
    assert "incomplete" in live.judge_live_book(book, None).reason
    book.promoted_at = "x"
    grant = LiveGrantModel(as_raced_config_hash="h", stake=STAKE + 1)
    verdict = live.judge_live_book(book, grant)
    assert "differs from the grant's stake" in verdict.reason and verdict.diverged
    grant.stake = STAKE
    assert live.judge_live_book(book, grant).eligible


# ---------------------------------------------------------------------------
# Dry run and the arm flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_previews_and_audits_but_never_places(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await session.commit()
    broker = LiveFakeBroker()
    summary = await _run(maker, broker, config=_config(transmit=False))
    assert broker.placed == []
    assert broker.previews, "a dry run previews against the live Gateway"
    assert summary.would_place
    assert await _orders(maker) == []  # no share_orders rows at all
    assert await _events(maker, "ETF_TREND_SIGNAL") == []  # a dry run is not a real signal
    assert len(await _events(maker, live.LIVE_DRY_RUN_SIGNAL)) == 1
    assert len(await _events(maker, live.LIVE_DRY_RUN_ORDER)) == len(broker.previews)


@pytest.mark.asyncio
async def test_arm_flag_absent_means_no_transmission_even_with_everything_valid(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await session.commit()
    env = {
        "IBKR_TRADING_MODE": "live",
        "IBKR_LIVE_ACCOUNT_ID": LIVE_ID,
        "IBKR_LIVE_GATEWAY_PORT": "4001",
        "IBKR_GATEWAY_PORT": "4001",
        "IBC_LIVE_START_SCRIPT": "C:/IBC/live.bat",
    }
    config = _resolve(env, {"IBKR_GATEWAY_PORT": "4002"}, overlay_in_use=True, dry_run=False)
    assert config.transmit is False
    broker = LiveFakeBroker()
    await _run(maker, broker, config=config)
    assert broker.placed == []


@pytest.mark.asyncio
async def test_rehearsal_previews_the_latest_month_end_on_any_day(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await session.commit()
    broker = LiveFakeBroker()
    summary = await _run(maker, broker, day=datetime.date(2026, 10, 31), config=_config(False), rehearse=True)
    assert broker.placed == [] and broker.previews
    assert any("rehearsal" in n for n in summary.notes)
    live.run_post_session_anomalies.assert_not_awaited()


# ---------------------------------------------------------------------------
# Armed: sells before buys, sizing, previews, caps, no debit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_cash_month_places_previewed_buys_the_same_evening(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await session.commit()
    broker = LiveFakeBroker()
    summary = await _run(maker, broker)
    placed = {(s, side) for s, side, *_ in broker.placed}
    assert placed == {("SCHB", "BUY"), ("IAUM", "BUY"), ("TBIL", "BUY")}
    # Each order previewed once, in the batch gate, before anything is placed
    # (no preview between the control read and placeOrder, ADR-0008 pt 7).
    assert len(broker.previews) == len(broker.placed)
    assert sum(q * lim for _, _, q, lim, _ in broker.placed) <= STAKE
    assert summary.placed == [ref for *_, ref in broker.placed]
    signal = (await _events(maker, "ETF_TREND_SIGNAL"))[0]
    assert signal.payload["live"] is True and signal.payload["live_buys_deferred"] is False
    live.run_post_session_anomalies.assert_awaited_once()


async def _held_book(m, cash=100.0, holding=100):
    async with m() as session:
        await _add_book(session, "B36", B36_CONFIG, cash=cash)
        session.add(ShareHoldingModel(book_id="B36", symbol="SCHF", quantity=float(holding), updated_at="t0"))
        # Baseline: the equity carried into the window (cash + SCHF at 28).
        (await session.get(BookMtmHistoryModel, ("B36", "2026-10-19"))).mtm = cash + holding * 28.0
        await session.commit()


@pytest.mark.asyncio
async def test_sells_go_first_and_buys_wait_for_their_fills(maker):
    await _held_book(maker)
    broker = LiveFakeBroker()
    broker.position_rows = [LegPosition(9, "SCHF", "STK", 100.0, 28.0)]
    night1 = await _run(maker, broker)
    assert [(s, side, q) for s, side, q, _, _ in broker.placed] == [("SCHF", "SELL", 100)]
    assert len(await _events(maker, live.LIVE_BUYS_DEFERRED)) == 1

    # Next evening the sell is still working (OPEN): no buys yet.
    sell_ref = night1.placed[0]
    broker.placed.clear()
    broker.ref_states = {sell_ref: RefState.OPEN}
    await _run(maker, broker, day=NEXT_DAY)
    assert broker.placed == []


@pytest.mark.asyncio
async def test_buys_are_sized_from_filled_proceeds(maker):
    await _held_book(maker)
    broker = LiveFakeBroker()
    broker.position_rows = [LegPosition(9, "SCHF", "STK", 100.0, 28.0)]
    night1 = await _run(maker, broker)
    sell_ref = night1.placed[0]

    broker.placed.clear()
    broker.ref_states = {sell_ref: RefState.FILLED}
    broker.execution_rows = [FillInfo("x1", 9, "SLD", 100.0, 28.0, sell_ref, 1.0, "2026-11-02T14:31:00+00:00")]
    broker.position_rows = []
    night2 = await _run(maker, broker, day=NEXT_DAY)
    buys = [(s, q, lim) for s, side, q, lim, _ in broker.placed if side == "BUY"]
    assert buys and all(side == "BUY" for _, side, *_ in broker.placed)
    cost = sum(q * lim for _, q, lim in buys) + len(buys) * 1.0
    cash_after_fill = 100.0 + 100 * 28.0 - 1.0
    assert cost <= cash_after_fill
    assert cost > 100.0  # could only be funded by the sale proceeds
    assert any("buys placed" in n for n in night2.notes)
    orders = [o for o in await _orders(maker) if o.side == "BUY"]
    assert {o.signal_date for o in orders} == {SIGNAL_DAY.isoformat()}

    # The buy phase runs once.
    broker.placed.clear()
    broker.ref_states = {}
    await _run(maker, broker, day=LATER_DAY)
    assert broker.placed == []


@pytest.mark.asyncio
async def test_dry_run_with_sells_previews_sells_and_estimated_buys_within_the_stake(maker):
    await _held_book(maker)
    broker = LiveFakeBroker()
    broker.position_rows = [LegPosition(9, "SCHF", "STK", 100.0, 28.0)]
    summary = await _run(maker, broker, config=_config(False))
    assert broker.placed == []
    assert ("SCHF", "SELL", 100) in [p[:3] for p in broker.previews]
    estimated = [w for w in summary.would_place if "estimated" in w]
    assert estimated
    buys = [p for p in broker.previews if p[1] == "BUY"]
    assert sum(q * lim for _, _, q, lim in buys) <= 100.0 + 100 * 28.0  # never above stake + P&L room
    assert await _orders(maker) == []


@pytest.mark.asyncio
async def test_watch_notes_cover_live_books_only(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG, authority="PAPER", grant=False)
        await session.commit()
    summary = await _run(maker, LiveFakeBroker(), day=NEXT_DAY)
    assert not any("missed its month-end" in n for n in summary.notes)


@pytest.mark.asyncio
async def test_buy_phase_too_late_is_closed_and_loud(maker):
    await _held_book(maker)
    broker = LiveFakeBroker()
    broker.position_rows = [LegPosition(9, "SCHF", "STK", 100.0, 28.0)]
    night1 = await _run(maker, broker)
    broker.placed.clear()
    broker.ref_states = {night1.placed[0]: RefState.FILLED}
    broker.execution_rows = [FillInfo("x1", 9, "SLD", 100.0, 28.0, night1.placed[0], 1.0, "2026-11-02T14:31:00Z")]
    broker.position_rows = []
    summary = await _run(maker, broker, day=LATER_DAY)  # skipped NEXT_DAY
    assert broker.placed == []
    assert any("too late" in u for u in summary.urgent)
    assert len(await _events(maker, live.LIVE_BUY_PHASE_CLOSED)) == 1


@pytest.mark.asyncio
async def test_a_preview_error_refuses_the_whole_batch(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await session.commit()
    broker = LiveFakeBroker()
    broker.preview_errors["TBIL"] = PreviewRejectedError("whatIfOrder warning: no permissions")
    summary = await _run(maker, broker)
    assert broker.placed == []
    assert await _orders(maker) == []
    assert any("REFUSED, nothing placed" in u for u in summary.urgent)
    assert len(await _events(maker, live.LIVE_ORDERS_REFUSED)) == 1
    assert len(await _events(maker, "ETF_TREND_SKIPPED")) == 1


@pytest.mark.asyncio
async def test_a_buy_preview_showing_a_margin_debit_is_refused(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await session.commit()
    broker = LiveFakeBroker()
    broker.preview_result = SharePreview(10.0, 10.0, 100.0, 5_000.0, 1.0, 1.0)
    summary = await _run(maker, broker)
    assert broker.placed == []
    assert any("margin debit" in u for u in summary.urgent)


@pytest.mark.asyncio
async def test_no_debit_refuses_when_commissions_push_past_cash(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await session.commit()
    broker = LiveFakeBroker(cash=STAKE)
    broker.preview_result = SharePreview(10.0, 10.0, 50_000.0, 1_000.0, 900.0, 900.0)
    summary = await _run(maker, broker)
    assert broker.placed == []
    assert any("no borrowing" in u for u in summary.urgent)


@pytest.mark.asyncio
async def test_unknown_broker_cash_buys_nothing(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await session.commit()
    from backend.broker import AccountDataError

    broker = LiveFakeBroker()
    broker.cash = AccountDataError("no cash row")
    summary = await _run(maker, broker)
    assert broker.placed == []
    assert any("broker cash unavailable" in n for n in summary.notes)


@pytest.mark.asyncio
async def test_halted_book_places_nothing(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        (await session.get(TradingControlModel, "GLOBAL")).state = "HALT_ENTRIES"
        await session.commit()
    broker = LiveFakeBroker()
    summary = await _run(maker, broker)
    assert broker.placed == [] and broker.previews == []
    assert any("entries halted" in n for n in summary.notes)


# ---------------------------------------------------------------------------
# Flatten (ADR-0011 applies identically)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_armed_flatten_sells_through_the_previewing_broker(maker):
    await _held_book(maker)
    async with maker() as session:
        (await session.get(TradingControlModel, "GLOBAL")).state = "FLATTEN_REQUESTED"
        await session.commit()
    broker = LiveFakeBroker()
    broker.position_rows = [LegPosition(9, "SCHF", "STK", 100.0, 28.0)]
    await _run(maker, broker, day=NEXT_DAY)
    assert [(s, side, q) for s, side, q, _, _ in broker.placed] == [("SCHF", "SELL", 100)]
    assert broker.previews[0][:3] == ("SCHF", "SELL", 100)


@pytest.mark.asyncio
async def test_flatten_preview_refusal_rejects_the_order(maker):
    await _held_book(maker)
    async with maker() as session:
        (await session.get(TradingControlModel, "B36")).state = "FLATTEN_REQUESTED"
        await session.commit()
    broker = LiveFakeBroker()
    broker.position_rows = [LegPosition(9, "SCHF", "STK", 100.0, 28.0)]
    broker.preview_errors["SCHF"] = PreviewRejectedError("whatIfOrder warning: x")
    await _run(maker, broker, day=NEXT_DAY)
    assert broker.placed == []
    assert [o.status for o in await _orders(maker)] == ["REJECTED"]


@pytest.mark.asyncio
async def test_dry_run_flatten_previews_only(maker):
    await _held_book(maker)
    async with maker() as session:
        (await session.get(TradingControlModel, "GLOBAL")).state = "FLATTEN_REQUESTED"
        await session.commit()
    broker = LiveFakeBroker()
    broker.position_rows = [LegPosition(9, "SCHF", "STK", 100.0, 28.0)]
    summary = await _run(maker, broker, day=NEXT_DAY, config=_config(False))
    assert broker.placed == []
    assert any("flatten" in w for w in summary.would_place)
    assert await _orders(maker) == []


# ---------------------------------------------------------------------------
# Config resolution and the digest
# ---------------------------------------------------------------------------

GOOD_ENV = {
    "IBKR_TRADING_MODE": "live",
    "IBKR_LIVE_ACCOUNT_ID": LIVE_ID,
    "IBKR_LIVE_GATEWAY_PORT": "4001",
    "IBKR_GATEWAY_PORT": "4001",
    "IBC_LIVE_START_SCRIPT": "C:/IBC/live.bat",
    "IBKR_LIVE_ARM": "TRANSMIT",
}
PAPER_ENV = {"IBKR_GATEWAY_PORT": "4002", "IBC_START_SCRIPT": "C:/IBC/paper.bat"}


def test_config_both_keys_transmit_and_dry_run_beats_arm():
    assert _resolve(GOOD_ENV, PAPER_ENV, overlay_in_use=True, dry_run=False).transmit is True
    assert _resolve(GOOD_ENV, PAPER_ENV, overlay_in_use=True, dry_run=True).transmit is False


@pytest.mark.parametrize("token", ["1", "true", "transmit", " TRANSMIT", "yes"])
def test_arm_token_is_exact(token):
    env = {**GOOD_ENV, "IBKR_LIVE_ARM": token}
    assert _resolve(env, PAPER_ENV, overlay_in_use=True, dry_run=False).transmit is False


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"IBKR_TRADING_MODE": "paper"}, "not live"),
        ({"IBKR_LIVE_ACCOUNT_ID": ""}, "IBKR_LIVE_ACCOUNT_ID is not set"),
        ({"IBKR_LIVE_ACCOUNT_ID": "DU123"}, "paper"),
        ({"IBKR_LIVE_GATEWAY_PORT": ""}, "not a port"),
        ({"IBKR_LIVE_GATEWAY_PORT": "70000"}, "not a port"),
        ({"IBKR_GATEWAY_PORT": "4003"}, "must equal"),
        ({"IBKR_LIVE_GATEWAY_PORT": "4002", "IBKR_GATEWAY_PORT": "4002"}, "equals the paper one"),
        ({"IBC_LIVE_START_SCRIPT": ""}, "IBC_LIVE_START_SCRIPT is not set"),
        ({"IBC_LIVE_START_SCRIPT": "C:/IBC/paper.bat"}, "paper IBC start script"),
        ({"IBKR_CLIENT_ID": "x"}, "IBKR_CLIENT_ID"),
    ],
)
def test_config_refusals_never_name_the_account(change, fragment):
    env = {**GOOD_ENV, **change}
    with pytest.raises(LiveRefusal) as exc:
        _resolve(env, PAPER_ENV, overlay_in_use=True, dry_run=False)
    assert fragment in str(exc.value)
    assert LIVE_ID not in str(exc.value)


def test_config_refuses_without_the_overlay_and_with_missing_gateway_port():
    with pytest.raises(LiveRefusal, match="overlay"):
        _resolve(GOOD_ENV, PAPER_ENV, overlay_in_use=False, dry_run=False)
    env = {k: v for k, v in GOOD_ENV.items() if k != "IBKR_GATEWAY_PORT"}
    with pytest.raises(LiveRefusal, match="must equal"):
        _resolve(env, PAPER_ENV, overlay_in_use=True, dry_run=False)


def test_config_refuses_when_the_paper_processes_cannot_recognise_the_live_gateway():
    # #1098: paper teardowns spare the live Gateway only by its IBC paths in
    # `.env.live`. A live ini missing, or an overlay the paper side cannot
    # see, would let the next paper teardown kill it (and force a 2FA login).
    env = {k: v for k, v in GOOD_ENV.items()}
    with pytest.raises(LiveRefusal, match="IBC_LIVE_INI is not set"):
        resolve_live_config(env, PAPER_ENV, overlay_in_use=True, dry_run=False, paper_view_of_overlay=env)
    env["IBC_LIVE_INI"] = LIVE_INI
    with pytest.raises(LiveRefusal, match="paper processes cannot see"):
        resolve_live_config(env, PAPER_ENV, overlay_in_use=True, dry_run=False, paper_view_of_overlay={})
    # Same paths, written differently, still match.
    paper_view = {"IBC_LIVE_INI": '"c:\\ibc\\LIVE\\config.ini"', "IBC_LIVE_START_SCRIPT": "C:\\IBC\\live.bat"}
    assert resolve_live_config(env, PAPER_ENV, overlay_in_use=True, dry_run=False, paper_view_of_overlay=paper_view)


@pytest.mark.asyncio
async def test_gateway_port_closed_refuses_as_not_logged_in(maker):
    async with maker() as session:
        await _add_book(session, "B36", B36_CONFIG)
        await session.commit()
    broker = LiveFakeBroker()
    with pytest.raises(live.LiveGatewayNotLoggedIn, match="approve 2FA on your phone"):
        await _run(maker, broker, gateway_up=False)
    assert broker.opened is False and broker.placed == []
    assert len(await _events(maker, live.LIVE_BROKER_UNAVAILABLE)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ConnectionFailedError("Could not open IB Gateway session: TimeoutError()"),
        LiveAccountRequiredError("the Gateway reported no managed accounts — refusing to trade"),
    ],
)
async def test_no_logged_in_session_refuses_as_not_logged_in(maker, error):
    broker = LiveFakeBroker()
    broker.open_error = error
    with pytest.raises(live.LiveGatewayNotLoggedIn, match="approve 2FA on your phone"):
        await _run(maker, broker)
    assert broker.placed == [] and broker.previews == []
    assert len(await _events(maker, live.LIVE_BROKER_UNAVAILABLE)) == 1


def test_weekly_reauth_note_only_before_a_weekend():
    assert live.weekly_reauth_due_before_next_session(datetime.date(2026, 10, 30))  # a Friday
    assert not live.weekly_reauth_due_before_next_session(datetime.date(2026, 10, 28))  # a Wednesday
    # Thanksgiving Thursday: the next session is Friday, no Sunday between.
    assert not live.weekly_reauth_due_before_next_session(datetime.date(2026, 11, 26))


@pytest.mark.asyncio
async def test_friday_run_carries_the_weekly_reauth_note(maker):
    summary = await _run(maker, LiveFakeBroker(), day=SIGNAL_DAY)  # 2026-10-30 is a Friday
    assert any("Weekly re-login" in n for n in summary.notes)
    summary = await _run(maker, LiveFakeBroker(), day=LATER_DAY)  # a Tuesday
    assert not any("Weekly re-login" in n for n in summary.notes)


def test_paper_default_port_counts_when_the_base_env_omits_it():
    env = {**GOOD_ENV, "IBKR_LIVE_GATEWAY_PORT": "4002", "IBKR_GATEWAY_PORT": "4002"}
    with pytest.raises(LiveRefusal, match="equals the paper one"):
        _resolve(env, {}, overlay_in_use=True, dry_run=False)


def test_digest_titles_dry_run_and_armed_differently():
    summary = live.LiveRunSummary("t", "2026-10-30", transmit=False, would_place=["B36 buys: would BUY 1 SCHB"])
    title, body, priority = live.compose_live_digest(summary)
    assert "DRY RUN" in title and "would BUY" in body and priority == "default"
    armed = live.LiveRunSummary("t", "2026-10-30", transmit=True, urgent=["x"], placed=["r"], broker_ok=False)
    title, body, priority = live.compose_live_digest(armed)
    assert "ARMED" in title and priority == "urgent" and "Placed 1" in body and "not opened" in body
    empty = live.LiveRunSummary("t", "d", transmit=True)
    assert live.compose_live_digest(empty)[1] == "Nothing to do."


def test_live_mode_env_check(monkeypatch):
    monkeypatch.setattr(live, "TRADING_MODE", "live")
    monkeypatch.setenv("IBKR_TRADING_MODE", "live")
    assert live.live_mode_env_ok()
    monkeypatch.setenv("IBKR_TRADING_MODE", "paper")
    assert not live.live_mode_env_ok()
