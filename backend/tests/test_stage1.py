"""ADR-0006 stage 1 on paper (#1059): the stake-scaled envelope, the -30% stake
drawdown halt and the stage-1 entry-bar rows.

The halt's fail-closed paths are the priority: a staked book whose equity we
cannot see must read as halted, never as fine.
"""

import copy
import math
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import executor
from backend.anomaly import (
    _BOOK_HALTING_RULES,
    _SELF_CLEARABLE_RULES,
    LIVE_AUTHORITY_REVOKED_EVENT,
    STAKE_DRAWDOWN_HALT,
    run_post_session_anomalies,
)
from backend.book_fingerprint import book_config_hash
from backend.book_gates import CandidateOrder, Envelope, evaluate_book_gates, resolve_book_config
from backend.console import book_summaries
from backend.digest import is_urgent_event_type
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    BookMtmHistoryModel,
    OrderModel,
    PositionModel,
    TradingControlModel,
)
from backend.seeds import LAB_BOOKS, SEED_PLAYBOOKS
from backend.stage1 import (
    MARK_MAX_AGE_HOURS,
    STAGE1_DRAWDOWN_HALT_PCT,
    STAGE1_PAPER_TRADING_DAYS,
    evaluate_stake_drawdown,
    market_date_or_prefix,
    stage1_entry_bar,
)
from backend.states import LIVE_AUTHORITY_LIVE, LIVE_AUTHORITY_REVOKED
from backend.trading_control import ACTIVE, HALT_ENTRIES

STAKE = 2000.0
TODAY = "2026-10-14"  # a Wednesday
NOW = datetime(2026, 10, 14, 23, 0, tzinfo=UTC)
FRESH = (NOW - timedelta(hours=1)).isoformat()


# ---------------------------------------------------------------------------
# 1. The stake-scaled envelope
# ---------------------------------------------------------------------------


class TestStakeEnvelope:
    def test_no_stake_keeps_the_paper_basis(self):
        config = resolve_book_config({"envelope": {}})
        assert config.stage1_stake is None
        assert config.envelope.basis == Envelope().basis

    def test_stake_becomes_the_envelope_basis(self):
        config = resolve_book_config({"stage1_stake": STAKE, "envelope": {"max_loss_pct_per_trade": 2.5}})
        assert config.stage1_stake == STAKE
        assert config.envelope.basis == STAKE
        assert config.envelope.max_loss_pct_per_trade == 2.5

    def test_integer_stake_is_accepted(self):
        assert resolve_book_config({"stage1_stake": 2000}).envelope.basis == 2000.0

    @pytest.mark.parametrize("bad", [0, -5.0, math.nan, math.inf])
    def test_non_positive_or_non_finite_stake_raises(self, bad):
        with pytest.raises(ValueError, match="finite and positive"):
            resolve_book_config({"stage1_stake": bad})

    @pytest.mark.parametrize("bad", ["2000", True, [2000]])
    def test_non_numeric_stake_raises(self, bad):
        with pytest.raises(TypeError, match="must be a number"):
            resolve_book_config({"stage1_stake": bad})

    def test_stake_beside_an_explicit_basis_raises(self):
        with pytest.raises(ValueError, match="both set"):
            resolve_book_config({"stage1_stake": STAKE, "envelope": {"basis": 5000.0}})

    def test_no_seed_book_carries_a_stake(self):
        assert [b["id"] for b in LAB_BOOKS if "stage1_stake" in b["config"]] == []

    def test_setting_a_stake_moves_only_that_books_hash(self):
        before = {b["id"]: book_config_hash(b["config"], SEED_PLAYBOOKS) for b in LAB_BOOKS}
        books = copy.deepcopy(LAB_BOOKS)
        target = books[0]
        target["config"]["stage1_stake"] = STAKE
        after = {b["id"]: book_config_hash(b["config"], SEED_PLAYBOOKS) for b in books}
        assert {i for i in before if before[i] != after[i]} == {target["id"]}

    @pytest.mark.asyncio
    async def test_per_trade_cap_is_judged_against_the_stake(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with maker() as session:
            session.add(_book("B01"))
            session.add(_book("B02", config={"engine_variant": "V0", "envelope": {}, "stage1_stake": STAKE}))
            await session.commit()
            # $100 at risk: under 2.5% of the paper basis, over 2.5% of the stake.
            paper = await evaluate_book_gates(session, _candidate("B01"))
            staked = await evaluate_book_gates(session, _candidate("B02"))
        await engine.dispose()
        assert paper.allowed
        assert "MAX_LOSS_PER_TRADE" in staked.blocked_by()


def _candidate(book_id: str) -> CandidateOrder:
    return CandidateOrder(
        book_id=book_id,
        strategy_type="BULL_PUT_SPREAD",
        expiration_date="2026-11-20",
        legs=(("XSP   261120P00600000", "SHORT"),),
        max_loss_per_share=1.0,
        contracts=1,
    )


# ---------------------------------------------------------------------------
# 2. The drawdown verdict (pure) — fail-closed paths first
# ---------------------------------------------------------------------------


def _verdict(
    marks=(("2026-10-02", 10000.0), ("2026-10-14", 10000.0)),
    window_start="2026-10-05",
    fallback=None,
    priced=(),
    today=TODAY,
):
    return evaluate_stake_drawdown(
        stake=STAKE,
        marks=list(marks),
        window_start=window_start,
        fallback_baseline=fallback,
        today=today,
        position_priced_at=list(priced),
        now=NOW,
    )


class TestDrawdownFailsClosed:
    def test_no_marks_reads_halted(self):
        v = _verdict(marks=())
        assert v.halted and "no mark history" in v.detail

    def test_non_finite_latest_mark_reads_halted(self):
        v = _verdict(marks=(("2026-10-02", 10000.0), ("2026-10-14", math.nan)))
        assert v.halted and "not a finite number" in v.detail

    def test_unreadable_mark_date_reads_halted(self):
        v = _verdict(marks=(("garbage", 10000.0),))
        assert v.halted and "unreadable mark date" in v.detail

    def test_latest_mark_older_than_the_prior_trading_day_reads_halted(self):
        v = _verdict(marks=(("2026-10-02", 10000.0), ("2026-10-09", 10000.0)))
        assert v.halted and "stale equity" in v.detail

    def test_mark_from_the_prior_trading_day_is_fresh_enough(self):
        assert not _verdict(marks=(("2026-10-02", 10000.0), ("2026-10-13", 10000.0))).halted

    def test_never_priced_position_reads_halted(self):
        v = _verdict(priced=(FRESH, None))
        assert v.halted and "never been priced" in v.detail

    def test_stale_position_quote_reads_halted(self):
        stale = (NOW - timedelta(hours=MARK_MAX_AGE_HOURS + 1)).isoformat()
        v = _verdict(priced=(stale,))
        assert v.halted and "last priced" in v.detail

    def test_unreadable_position_timestamp_reads_halted(self):
        assert _verdict(priced=("not a time",)).halted

    def test_naive_position_timestamp_reads_halted(self):
        assert _verdict(priced=("2026-10-14T22:00:00",)).halted

    def test_no_baseline_and_no_fallback_reads_halted(self):
        v = _verdict(marks=(("2026-10-14", 10000.0),))
        assert v.halted and "no usable baseline" in v.detail

    def test_non_finite_fallback_reads_halted(self):
        assert _verdict(marks=(("2026-10-14", 10000.0),), fallback=math.inf).halted

    def test_stale_mark_limit_matches_the_executors(self):
        assert MARK_MAX_AGE_HOURS == executor.STALE_MARK_MAX_HOURS


class TestDrawdownMeasurement:
    def test_flat_book_is_not_halted(self):
        v = _verdict(priced=(FRESH,))
        assert not v.halted
        assert v.drawdown == 0.0
        assert v.threshold == STAKE * STAGE1_DRAWDOWN_HALT_PCT / 100.0

    def test_exactly_thirty_percent_of_the_stake_halts(self):
        v = _verdict(marks=(("2026-10-02", 10000.0), ("2026-10-14", 10000.0 - 0.3 * STAKE)))
        assert v.halted and "reached" in v.detail
        assert v.drawdown == 0.3 * STAKE

    def test_just_under_thirty_percent_does_not_halt(self):
        assert not _verdict(marks=(("2026-10-02", 10000.0), ("2026-10-14", 10000.0 - 0.3 * STAKE + 1))).halted

    def test_baseline_is_the_last_mark_before_the_window(self):
        # 10-01 then 10-02 precede the window: the 10-02 mark is the baseline,
        # and in-window marks (10-06's peak) do not move it (not peak-to-trough).
        marks = (("2026-10-01", 9000.0), ("2026-10-02", 10000.0), ("2026-10-06", 12000.0), ("2026-10-14", 9500.0))
        v = _verdict(marks=marks)
        assert not v.halted
        assert v.evidence["baseline"] == 10000.0
        assert v.drawdown == 500.0

    def test_fallback_baseline_used_when_no_mark_precedes_the_window(self):
        v = _verdict(marks=(("2026-10-14", 9300.0),), fallback=10000.0)
        assert v.halted and v.evidence["baseline"] == 10000.0


# ---------------------------------------------------------------------------
# 2b. The halt through the nightly sweep
# ---------------------------------------------------------------------------


def _book(book_id: str = "B01", **overrides) -> BookModel:
    defaults: dict = {
        "id": book_id,
        "name": book_id,
        "config": {"engine_variant": "V0", "underlying": "XSP", "envelope": {}},
        "config_version": 1,
        "config_hash": "h",
        "starting_capital": 10000.0,
        "cash_balance": 10000.0,
        "status": "ACTIVE",
        "created_at": "2026-09-01T00:00:00+00:00",
    }
    defaults.update(overrides)
    return BookModel(**defaults)


def _staked(**overrides) -> BookModel:
    return _book(
        config={"engine_variant": "V0", "underlying": "XSP", "envelope": {}, "stage1_stake": STAKE}, **overrides
    )


def _open_position(current: float, priced_at: str | None = FRESH) -> PositionModel:
    return PositionModel(
        id="p1",
        underlying="XSP",
        strategy_type="BULL_PUT_SPREAD",
        execution_mode="PAPER",
        legs=[],
        entry_date="2026-10-06",
        expiration_date="2026-11-20",
        entry_premium=1.0,
        premium_direction="CREDIT",
        current_value_per_share=current,
        contracts=1,
        max_profit=1.0,
        # Under 2.5% of the stake, so the (stake-scaled) post-hoc envelope
        # sweep stays quiet and these tests see only the drawdown rule.
        max_loss=0.4,
        notes="",
        rolls=0,
        status="OPEN",
        journal={},
        book_id="B01",
        last_priced_at=priced_at,
    )


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield m
    await engine.dispose()


async def _seed(m, book: BookModel, *extra: object) -> None:
    async with m() as session:
        session.add(book)
        session.add(TradingControlModel(scope="GLOBAL", state=ACTIVE, reason="", actor="t", changed_at="t0"))
        session.add(TradingControlModel(scope=book.id, state=ACTIVE, reason="", actor="t", changed_at="t0"))
        session.add_all(extra)
        await session.commit()


async def _sweep(m, today: str = TODAY):
    async with m() as session:
        return await run_post_session_anomalies(session, today)


async def _state(m, scope: str = "B01") -> str:
    async with m() as session:
        return (await session.get(TradingControlModel, scope)).state


async def _authority(m) -> str | None:
    async with m() as session:
        return (await session.get(BookModel, "B01")).live_authority


async def _events(m, event_type: str) -> list[AuditEventModel]:
    async with m() as session:
        return list((await session.execute(select(AuditEventModel).filter_by(event_type=event_type))).scalars().all())


def _sync(run_at: str = "2026-10-05T12:00:00+00:00") -> AuditEventModel:
    return AuditEventModel(run_at=run_at, book_id="B01", event_type="BOOK_CONFIG_SYNCED", actor="seed", payload={})


def _mark(day: str, mtm: float) -> BookMtmHistoryModel:
    return BookMtmHistoryModel(book_id="B01", date=day, mtm=mtm)


class TestDrawdownHaltSweep:
    @pytest.mark.asyncio
    async def test_drawdown_past_thirty_percent_demotes(self, maker):
        # Era began 10-05; baseline is the 10-02 mark. A credit position
        # marked at 7.0 (cash 10000 - 700 = 9300) is a 35% stake drawdown.
        await _seed(maker, _staked(live_authority=LIVE_AUTHORITY_LIVE), _sync(), _mark("2026-10-02", 10000.0))
        async with maker() as session:
            session.add(_open_position(current=7.0))
            await session.commit()
        findings = await _sweep(maker)
        assert [f.rule for f in findings] == [STAKE_DRAWDOWN_HALT]
        assert await _state(maker) == HALT_ENTRIES
        assert await _authority(maker) == LIVE_AUTHORITY_REVOKED
        revoked = await _events(maker, LIVE_AUTHORITY_REVOKED_EVENT)
        assert len(revoked) == 1 and revoked[0].payload["previous"] == LIVE_AUTHORITY_LIVE
        halt = await _events(maker, STAKE_DRAWDOWN_HALT)
        assert halt[0].payload["evidence"]["baseline"] == 10000.0
        assert is_urgent_event_type(STAKE_DRAWDOWN_HALT)

    @pytest.mark.asyncio
    async def test_small_drawdown_leaves_the_book_alone(self, maker):
        await _seed(maker, _staked(), _sync(), _mark("2026-10-02", 10000.0))
        async with maker() as session:
            session.add(_open_position(current=2.0))  # 10% of the stake
            await session.commit()
        assert await _sweep(maker) == []
        assert await _state(maker) == ACTIVE
        assert await _authority(maker) is None

    @pytest.mark.asyncio
    async def test_unstaked_book_is_never_judged(self, maker):
        # Same losses, no stake, no prior mark: an ordinary paper book.
        await _seed(maker, _book(), _sync())
        async with maker() as session:
            session.add(_open_position(current=7.0, priced_at=None))
            await session.commit()
        assert STAKE_DRAWDOWN_HALT not in [f.rule for f in await _sweep(maker)]
        assert await _authority(maker) is None

    @pytest.mark.asyncio
    async def test_missing_baseline_fails_closed(self, maker):
        # Synced into a new era with no mark before it: the drawdown cannot be
        # measured, so the staked book reads as halted.
        await _seed(maker, _staked(), _sync())
        findings = await _sweep(maker)
        assert [f.rule for f in findings] == [STAKE_DRAWDOWN_HALT]
        assert "no usable baseline" in findings[0].detail
        assert await _state(maker) == HALT_ENTRIES
        assert await _authority(maker) == LIVE_AUTHORITY_REVOKED

    @pytest.mark.asyncio
    async def test_stale_position_quote_fails_closed(self, maker):
        await _seed(maker, _staked(), _sync(), _mark("2026-10-02", 10000.0))
        async with maker() as session:
            session.add(_open_position(current=1.0, priced_at="2026-10-01T22:00:00+00:00"))
            await session.commit()
        findings = await _sweep(maker)
        assert [f.rule for f in findings] == [STAKE_DRAWDOWN_HALT]
        assert await _state(maker) == HALT_ENTRIES

    @pytest.mark.asyncio
    async def test_book_born_staked_uses_starting_capital(self, maker):
        # Never synced: the era is the book's whole life, so starting_capital
        # is the baseline and a flat book is fine.
        await _seed(maker, _staked())
        assert await _sweep(maker) == []

    @pytest.mark.asyncio
    async def test_live_grant_measures_from_promoted_at(self, maker):
        # Era began 10-05 at 10000, but the grant on 10-08 came in at 9000:
        # from the grant, 9000 -> 8700 is 15% of the stake, not a halt.
        book = _staked(live_authority=LIVE_AUTHORITY_LIVE, promoted_at="2026-10-08T15:00:00+00:00", cash_balance=8800.0)
        await _seed(maker, book, _sync(), _mark("2026-10-02", 10000.0), _mark("2026-10-07", 9000.0))
        async with maker() as session:
            session.add(_open_position(current=1.0))
            await session.commit()
        assert await _sweep(maker) == []
        assert await _authority(maker) == LIVE_AUTHORITY_LIVE

    @pytest.mark.asyncio
    async def test_revoked_book_is_not_rejudged_so_a_resume_can_stick(self, maker):
        await _seed(maker, _staked(live_authority=LIVE_AUTHORITY_REVOKED), _sync())
        assert await _sweep(maker) == []
        assert await _state(maker) == ACTIVE

    @pytest.mark.asyncio
    async def test_halt_never_self_clears(self, maker):
        # Night 1 demotes (missing baseline). Night 2: nothing fires at all
        # (the book is REVOKED, so not re-judged) — the halt must still stand.
        await _seed(maker, _staked(), _sync())
        await _sweep(maker)
        assert await _state(maker) == HALT_ENTRIES
        assert await _sweep(maker, "2026-10-15") == []
        assert await _state(maker) == HALT_ENTRIES

    def test_rule_is_book_halting_and_not_self_clearable(self):
        assert STAKE_DRAWDOWN_HALT in _BOOK_HALTING_RULES
        assert STAKE_DRAWDOWN_HALT not in _SELF_CLEARABLE_RULES


# ---------------------------------------------------------------------------
# 3. The stage-1 entry-bar rows
# ---------------------------------------------------------------------------


def _bar(book: BookModel | None = None, *, days: int = 15, fills: int = 1, breaches: int = 0, excluded=False):
    # 15 trading days from 2026-10-05 (Mon) are 10-05..10-23, no holidays.
    mark_dates = (
        ["2026-10-02"]
        + [d.isoformat() for d in (date(2026, 10, 5) + timedelta(days=i) for i in range(30)) if d.weekday() < 5][:days]
        + ["2026-10-10", "garbage"]
    )  # a Saturday or unreadable mark never counts
    return stage1_entry_bar(
        book=book or _book(),
        stake=None,
        era_start="2026-10-05",
        mark_dates=mark_dates,
        filled_orders=fills,
        breaches=breaches,
        excluded=excluded,
    )


def _status(bar, key: str) -> str:
    return next(c.status for c in bar.conditions if c.key == key)


class TestStage1EntryBar:
    def test_rows_and_keys(self):
        bar = _bar()
        assert [c.key for c in bar.conditions] == [
            "stage1_not_retired",
            "stage1_paper_days",
            "stage1_zero_breaches",
            "stage1_operator_sign_off",
        ]
        assert bar.trading_days == STAGE1_PAPER_TRADING_DAYS
        assert bar.trading_days_required == STAGE1_PAPER_TRADING_DAYS

    def test_everything_mechanical_passing_is_still_not_claimable_without_sign_off(self):
        bar = _bar()
        assert all(_status(bar, k) == "ok" for k in ("stage1_not_retired", "stage1_paper_days", "stage1_zero_breaches"))
        assert _status(bar, "stage1_operator_sign_off") == "not_yet_evaluated"
        assert not bar.claimable

    def test_retired_book_fails_the_retirement_row(self):
        assert _status(_bar(_book(status="RETIRED")), "stage1_not_retired") == "fail"

    def test_too_few_trading_days_fails(self):
        bar = _bar(days=14)
        assert bar.trading_days == 14
        assert _status(bar, "stage1_paper_days") == "fail"

    def test_no_fill_fails_even_with_enough_days(self):
        assert _status(_bar(fills=0), "stage1_paper_days") == "fail"

    def test_a_breach_fails(self):
        assert _status(_bar(breaches=1), "stage1_zero_breaches") == "fail"

    def test_window_start_date_parsing(self):
        assert market_date_or_prefix("2026-10-06T02:00:00+00:00") == "2026-10-05"  # evening ET
        assert market_date_or_prefix("2026-10-05") == "2026-10-05"
        assert market_date_or_prefix("t0-not-a-time") == "t0-not-a-t"

    def test_excluded_book_is_never_claimable(self):
        assert not _bar(excluded=True).claimable

    @pytest.mark.asyncio
    async def test_console_carries_the_bar(self, maker, monkeypatch, tmp_path):
        monkeypatch.setenv("HALT_FILE", str(tmp_path / "HALT"))
        await _seed(maker, _staked(), _sync(), _mark("2026-10-06", 10000.0), _mark("2026-10-02", 10000.0))
        async with maker() as session:
            session.add_all(
                [
                    _order("o_old", "2026-10-01T22:00:00+00:00"),  # before the era: not counted
                    _order("o_new", "2026-10-06T22:00:00+00:00"),
                    _order("o_open", None, status="SUBMITTED"),
                ]
            )
            await session.commit()
            summaries = await book_summaries(session, now=NOW)
        bar = summaries[0].stage1_entry_bar
        assert bar.stake == STAKE
        assert bar.era_start == "2026-10-05"
        assert bar.filled_orders == 1
        assert bar.trading_days == 1
        assert not bar.claimable


def _order(order_id: str, completed_at: str | None, status: str = "FILLED") -> OrderModel:
    return OrderModel(
        id=order_id,
        book_id="B01",
        order_ref=f"basis:B01:{order_id}:OPEN",
        action="OPEN",
        combo_legs={},
        limit_price=1.0,
        decision_midpoint=1.0,
        status=status,
        completed_at=completed_at,
    )
