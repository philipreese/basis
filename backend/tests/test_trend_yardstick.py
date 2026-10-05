"""The monthly ETF trend book's own yardstick (#1054, ADR-0010 amendment):
≥ 6 months, a stress episode after the first fill, Sharpe beating a 60/40
VTI/IEF mix over the same intervals, worst drawdown no deeper than 20%.
Every row fails closed."""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.console import book_summaries, trend_yardstick
from backend.models import (
    Base,
    BookModel,
    BookMtmHistoryModel,
    IndexHistoryModel,
    ShareHoldingModel,
    ShareOrderModel,
    TotalReturnHistoryModel,
    TradingControlModel,
)
from backend.seeds import LAB_BOOKS

DATES = [f"2026-11-{d:02d}" for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13)]


def _benchmark(vti: list[float], ief: list[float]) -> dict[str, dict[str, float]]:
    return {"VTI": dict(zip(DATES, vti, strict=True)), "IEF": dict(zip(DATES, ief, strict=True))}


# A choppy 60/40 (Sharpe low) and a steadily rising book (Sharpe high).
CHOPPY = _benchmark([100, 103, 99, 104, 98, 105, 99, 104, 100, 103], [100, 99, 101, 98, 102, 99, 101, 98, 101, 100])
STEADY_MARKS = [(d, 10_000.0 + 10.0 * i + (3.0 if i % 2 else 0.0)) for i, d in enumerate(DATES)]


def _yardstick(**overrides):
    args = {
        "marks": STEADY_MARKS,
        "closes": CHOPPY,
        "vix_by_date": {"2026-11-05": 27.0},
        "spy_by_date": {},
        "era_start": "2026-11-01",
        "window_end": "2027-05-14",  # 6.3 months after the first fill
        "first_fill_date": "2026-11-02",
    }
    args.update(overrides)
    return trend_yardstick(**args)


def _row(check, key):
    return next(c for c in check.conditions if c.key == key)


class TestRows:
    def test_everything_passing_is_ok(self):
        check = _yardstick()
        assert check.ok
        assert {c.status for c in check.conditions} == {"ok"}

    def test_under_six_months_fails(self):
        check = _yardstick(window_end="2027-04-30")  # 5.9 months after the first fill
        assert _row(check, "trend_months").status == "fail" and not check.ok

    # Operator review of #1073: the window opens at the first fill, never at
    # the era start — halted, never-traded months must not count.
    def test_seven_halted_months_with_no_fill_fail_closed(self):
        check = _yardstick(era_start="2026-04-01", window_end="2026-11-13", first_fill_date=None)
        months = _row(check, "trend_months")
        assert months.status == "fail" and "no fill yet" in months.detail
        assert check.months_elapsed == 0.0
        assert check.window_start == "2026-04-01"
        assert {c.status for c in check.conditions} == {"fail"}
        assert not check.ok

    def test_halted_months_before_the_first_fill_do_not_count_toward_six(self):
        # Era opened seven months before the first fill; 5.9 months since it.
        check = _yardstick(era_start="2026-04-01", window_end="2027-04-30")
        assert _row(check, "trend_months").status == "fail"
        assert check.window_start == "2026-11-02"
        assert check.months_elapsed < 6.0

    def test_first_fill_then_six_months_passes(self):
        check = _yardstick(era_start="2026-04-01", window_end="2027-05-14")
        assert _row(check, "trend_months").status == "ok"
        assert check.window_start == "2026-11-02"
        assert check.ok

    def test_a_fill_from_an_earlier_era_opens_no_window(self):
        check = _yardstick(era_start="2026-11-05", first_fill_date="2026-11-02")
        assert check.first_fill_date is None
        assert not check.ok and _row(check, "trend_months").status == "fail"

    def test_marks_before_the_first_fill_are_not_judged(self):
        # A pre-fill crash in the marks is outside the window.
        marks = [("2026-10-20", 20_000.0), ("2026-10-21", 9_000.0), *STEADY_MARKS]
        check = _yardstick(marks=marks)
        assert _row(check, "trend_max_drawdown").status == "ok"
        assert check.sharpe_intervals == len(DATES) - 1

    def test_stress_before_the_first_fill_does_not_count(self):
        check = _yardstick(first_fill_date="2026-11-06")
        assert _row(check, "trend_stress_episode").status == "fail"
        assert check.stress_episode_dates == 0

    def test_no_fill_yet_means_no_stress_test_taken(self):
        check = _yardstick(first_fill_date=None)
        row = _row(check, "trend_stress_episode")
        assert row.status == "fail" and "no fill yet" in row.detail

    def test_spy_drawdown_trigger_counts(self):
        spy = {"2026-11-02": 100.0, "2026-11-03": 94.0}
        check = _yardstick(vix_by_date={}, spy_by_date=spy)
        assert _row(check, "trend_stress_episode").status == "ok"

    def test_calm_window_fails(self):
        check = _yardstick(vix_by_date={"2026-11-05": 18.0})
        assert _row(check, "trend_stress_episode").status == "fail"

    def test_book_sharpe_must_beat_the_60_40(self):
        check = _yardstick()
        assert check.book_sharpe is not None and check.benchmark_sharpe is not None
        assert check.book_sharpe > check.benchmark_sharpe
        assert check.sharpe_intervals == len(DATES) - 1

    def test_book_worse_than_the_60_40_fails(self):
        smooth = _benchmark([100 + i for i in range(10)], [100 + 0.5 * i for i in range(10)])
        choppy_marks = [(d, 10_000.0 + (300.0 if i % 2 else -200.0)) for i, d in enumerate(DATES)]
        check = _yardstick(marks=choppy_marks, closes=smooth)
        assert _row(check, "trend_sharpe_vs_60_40").status == "fail"

    def test_sharpe_with_too_few_intervals_fails_closed(self):
        check = _yardstick(marks=STEADY_MARKS[:2])
        row = _row(check, "trend_sharpe_vs_60_40")
        assert row.status == "fail" and "not computable" in row.detail

    def test_flat_cash_book_has_no_sharpe(self):
        flat = [(d, 10_000.0) for d in DATES]
        assert _row(_yardstick(marks=flat), "trend_sharpe_vs_60_40").status == "fail"

    def test_intervals_missing_benchmark_closes_are_skipped_on_both_sides(self):
        closes = {"VTI": dict(CHOPPY["VTI"]), "IEF": dict(CHOPPY["IEF"])}
        del closes["VTI"]["2026-11-05"]
        check = _yardstick(closes=closes)
        assert check.sharpe_intervals_skipped == 2
        assert check.sharpe_intervals == len(DATES) - 3
        assert "skipped" in _row(check, "trend_sharpe_vs_60_40").detail

    def test_drawdown_beyond_twenty_percent_fails(self):
        marks = [*STEADY_MARKS[:5], ("2026-11-09", 7_900.0), *STEADY_MARKS[6:]]
        check = _yardstick(marks=marks)
        assert _row(check, "trend_max_drawdown").status == "fail"
        assert check.max_drawdown_pct is not None and check.max_drawdown_pct > 20.0

    def test_drawdown_exactly_at_the_limit_passes(self):
        marks = [("2026-11-02", 10_000.0), ("2026-11-03", 8_000.0), ("2026-11-04", 9_000.0)]
        assert _row(_yardstick(marks=marks), "trend_max_drawdown").status == "ok"

    def test_no_marks_fails_every_mark_row(self):
        check = _yardstick(marks=[])
        assert _row(check, "trend_max_drawdown").status == "fail"
        assert _row(check, "trend_sharpe_vs_60_40").status == "fail"
        assert not check.ok


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    b36 = next(b for b in LAB_BOOKS if b["id"] == "B36")
    b38 = next(b for b in LAB_BOOKS if b["id"] == "B38")
    async with m() as session:
        for book_id, config in (
            ("B01", {"engine_variant": "V0", "underlying": "XSP", "envelope": {}}),
            ("B36", b36["config"]),
            ("B38", b38["config"]),
        ):
            session.add(
                BookModel(
                    id=book_id,
                    name=book_id,
                    config=config,
                    config_version=1,
                    config_hash=f"hash-{book_id}",
                    starting_capital=10000.0,
                    cash_balance=10000.0,
                    status="ACTIVE",
                    created_at="2026-05-01T00:00:00+00:00",
                )
            )
            session.add(TradingControlModel(scope=book_id, state="ACTIVE", reason="", actor="t", changed_at="t0"))
        await session.commit()
    yield m
    await engine.dispose()


class TestBookSummaries:
    @pytest.mark.asyncio
    async def test_book_kind_comes_from_config_not_from_the_yardstick(self, maker):
        # #1132: B38 is a share book WITHOUT a yardstick of its own — the
        # console must still know it is a share book, or it falls back to the
        # options Live Gate cells ("0/30 trades").
        async with maker() as session:
            summaries = {s.id: s for s in await book_summaries(session, now=datetime(2026, 10, 5, 22, 0, tzinfo=UTC))}
        assert summaries["B01"].book_kind == "options"
        assert summaries["B36"].book_kind == "share"
        assert summaries["B36"].trend_yardstick is not None
        assert summaries["B36"].trend_yardstick.first_fill_date is None  # the "waiting for first fill" state
        assert summaries["B38"].book_kind == "share"
        assert summaries["B38"].trend_yardstick is None

    @pytest.mark.asyncio
    async def test_share_book_carries_its_yardstick_and_is_never_live_gate_eligible(self, maker):
        async with maker() as session:
            session.add(ShareHoldingModel(book_id="B36", symbol="SGOV", quantity=90.0, updated_at="t0"))
            session.add(IndexHistoryModel(date="2026-11-12", symbol="SGOV", close=100.0))
            # #1074: the benchmark is read from the total-return series.
            for d, c in CHOPPY["VTI"].items():
                session.add(TotalReturnHistoryModel(date=d, symbol="VTI", close=float(c), fetched_at="t"))
            for d, c in CHOPPY["IEF"].items():
                session.add(TotalReturnHistoryModel(date=d, symbol="IEF", close=float(c), fetched_at="t"))
            for d, mtm in STEADY_MARKS:
                session.add(BookMtmHistoryModel(book_id="B36", date=d, mtm=mtm))
            session.add(
                ShareOrderModel(
                    id="o1",
                    book_id="B36",
                    order_ref="basis:B36:o1:share",
                    symbol="SGOV",
                    side="BUY",
                    quantity=90,
                    limit_price=102.0,
                    decision_close=100.0,
                    signal_date="2026-10-30",
                    status="FILLED",
                    config_hash="hash-B36",
                    created_at="t0",
                    completed_at="2026-11-02T23:00:00+00:00",
                    fills=[],
                    filled_quantity=90.0,
                )
            )
            await session.commit()
            summaries = {s.id: s for s in await book_summaries(session, now=datetime(2026, 11, 13, 22, 0, tzinfo=UTC))}
        b36, b01 = summaries["B36"], summaries["B01"]
        assert b36.trend_yardstick is not None
        assert b36.trend_yardstick.first_fill_date == "2026-11-02"
        assert b36.trend_yardstick.sharpe_intervals == len(DATES) - 1
        # Created 2026-05-01, first fill 2026-11-02: the six months that sat
        # unfilled do not count, so the months row fails eleven days in.
        assert b36.trend_yardstick.window_start == "2026-11-02"
        assert b36.trend_yardstick.months_elapsed < 1.0
        assert not b36.trend_yardstick.ok
        assert not b36.live_gate.eligible
        assert [(h.symbol, h.quantity, h.mark) for h in b36.share_holdings] == [("SGOV", 90.0, 100.0)]
        assert b01.trend_yardstick is None and b01.share_holdings == []

    @pytest.mark.asyncio
    async def test_benchmark_is_scored_on_total_return_never_price_closes(self, maker):
        # #1074: the price-only index_history series and the adjusted
        # (dividends-reinvested) series disagree; the yardstick must read the
        # adjusted one. `rising` is a steadily climbing total-return 60/40.
        rising = _benchmark(
            [100 + 1.0 * i + (0.4 if i % 2 else 0.0) for i in range(len(DATES))],
            [100 + 0.2 * i for i in range(len(DATES))],
        )
        async with maker() as session:
            for symbol in ("VTI", "IEF"):
                for d, c in CHOPPY[symbol].items():
                    session.add(IndexHistoryModel(date=d, symbol=symbol, close=float(c)))
                for d, c in rising[symbol].items():
                    session.add(TotalReturnHistoryModel(date=d, symbol=symbol, close=float(c), fetched_at="t"))
            for d, mtm in STEADY_MARKS:
                session.add(BookMtmHistoryModel(book_id="B36", date=d, mtm=mtm))
            session.add(_filled_share_order())
            await session.commit()
            summaries = {s.id: s for s in await book_summaries(session, now=datetime(2026, 11, 13, 22, 0, tzinfo=UTC))}
        got = summaries["B36"].trend_yardstick
        expected = _yardstick(closes=rising, window_end="2026-11-13")
        assert got.benchmark_sharpe == expected.benchmark_sharpe
        assert got.benchmark_sharpe != _yardstick(window_end="2026-11-13").benchmark_sharpe
        assert "total return" in _row(got, "trend_sharpe_vs_60_40").detail

    @pytest.mark.asyncio
    async def test_no_total_return_series_fails_the_sharpe_row_closed(self, maker):
        # Price closes alone never stand in for the missing adjusted series.
        async with maker() as session:
            for symbol in ("VTI", "IEF"):
                for d, c in CHOPPY[symbol].items():
                    session.add(IndexHistoryModel(date=d, symbol=symbol, close=float(c)))
            for d, mtm in STEADY_MARKS:
                session.add(BookMtmHistoryModel(book_id="B36", date=d, mtm=mtm))
            session.add(_filled_share_order())
            await session.commit()
            summaries = {s.id: s for s in await book_summaries(session, now=datetime(2026, 11, 13, 22, 0, tzinfo=UTC))}
        got = summaries["B36"].trend_yardstick
        assert got.sharpe_intervals == 0
        assert got.benchmark_sharpe is None
        row = _row(got, "trend_sharpe_vs_60_40")
        assert row.status == "fail"
        assert "total-return closes" in row.detail


def _filled_share_order() -> ShareOrderModel:
    return ShareOrderModel(
        id="o1",
        book_id="B36",
        order_ref="basis:B36:o1:share",
        symbol="SGOV",
        side="BUY",
        quantity=90,
        limit_price=102.0,
        decision_close=100.0,
        signal_date="2026-10-30",
        status="FILLED",
        config_hash="hash-B36",
        created_at="t0",
        completed_at="2026-11-02T23:00:00+00:00",
        fills=[],
        filled_quantity=90.0,
    )
