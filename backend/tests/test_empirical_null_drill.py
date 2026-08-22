"""Tests for the empirical-null bootstrap drill (backend/empirical_null_drill.py, #657).

Two things matter most: (1) the bootstrap under a fixed seed is deterministic
and its percentiles behave sensibly against a known pooled distribution, and
(2) the drill's DB access is read-only exactly like restore_drill.py's — a
write attempt raises at the driver, never merely by convention.
"""

import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import empirical_null_drill as end
from backend.models import AuditEventModel, Base, BookModel, PositionModel

NOW_ISO = "2026-08-22T00:00:00+00:00"


def _book(book_id: str = "B01", **overrides) -> BookModel:
    defaults: dict = {
        "id": book_id,
        "name": f"lab {book_id}",
        "config": {"engine_variant": "V1", "underlying": "XSP", "envelope": {}},
        "config_version": 1,
        "config_hash": "cafe1234",
        "starting_capital": 10000.0,
        "cash_balance": 10000.0,
        "status": "ACTIVE",
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_mtm": None,
    }
    defaults.update(overrides)
    return BookModel(**defaults)


_POS_SEQ = iter(range(10_000))


def _position(book_id: str, status: str, entry: float, exit_value: float, **overrides) -> PositionModel:
    defaults: dict = {
        "id": f"p{next(_POS_SEQ)}",
        "underlying": "XSP",
        "strategy_type": "BULL_PUT_SPREAD",
        "execution_mode": "PAPER",
        "legs": [],
        "entry_date": "2026-08-01",
        "expiration_date": "2026-09-18",
        "entry_premium": entry,
        "premium_direction": "CREDIT",
        "current_value_per_share": exit_value,
        "contracts": 1,
        "max_profit": entry,
        "max_loss": 3.0 - entry,
        "notes": "",
        "rolls": 0,
        "status": status,
        "journal": {},
        "book_id": book_id,
        "config_hash": "cafe1234",
    }
    defaults.update(overrides)
    return PositionModel(**defaults)


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield maker
    await engine.dispose()


class TestSampleSe:
    def test_undefined_below_n_2(self):
        assert end._sample_se([5.0]) is None
        assert end._sample_se([]) is None

    def test_matches_hand_computed_value(self):
        # mean 25, sample stdev (n-1) of [45, 25, 5] is ~20, SE = 20/sqrt(3) ~= 11.547
        se = end._sample_se([45.0, 25.0, 5.0])
        assert se == pytest.approx(11.547, abs=1e-3)


class TestPercentileHelpers:
    def test_percentile_nearest_rank(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert end._percentile(values, 0.0) == 1.0
        assert end._percentile(values, 100.0) == 5.0
        assert end._percentile(values, 50.0) == 3.0

    def test_percentile_rank_counts_at_or_below(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert end._percentile_rank(values, 3.0) == 60.0  # 3 of 5 values <= 3.0
        assert end._percentile_rank(values, 0.0) == 0.0
        assert end._percentile_rank(values, 10.0) == 100.0

    def test_empty_inputs_are_nan_not_a_crash(self):
        import math

        assert math.isnan(end._percentile([], 50.0))
        assert math.isnan(end._percentile_rank([], 1.0))


class TestRunBootstrap:
    def test_deterministic_under_a_fixed_seed(self):
        pooled = [10.0, -5.0, 20.0, -15.0, 8.0, -2.0]
        per_arm_n = [3, 3]
        first = end.run_bootstrap(pooled, per_arm_n, n_iterations=200, seed=42)
        second = end.run_bootstrap(pooled, per_arm_n, n_iterations=200, seed=42)
        assert first == second

    def test_different_seeds_diverge(self):
        pooled = [10.0, -5.0, 20.0, -15.0, 8.0, -2.0]
        per_arm_n = [3, 3]
        a, _ = end.run_bootstrap(pooled, per_arm_n, n_iterations=200, seed=1)
        b, _ = end.run_bootstrap(pooled, per_arm_n, n_iterations=200, seed=2)
        assert a != b

    def test_a_symmetric_zero_mean_pool_still_yields_a_positive_max_book_median(self):
        # #657's null-construction framing: pooled resampling preserves
        # whatever the pool contains, but "max across several arms" is a
        # biased-upward statistic even when every arm's TRUE mean is zero —
        # this must not look like "the drill found edge that isn't there."
        pooled = [100.0, -100.0, 50.0, -50.0, 20.0, -20.0, 10.0, -10.0]
        per_arm_n = [4, 4, 4, 4, 4]  # 5 synthetic arms, matching a 5-book matrix
        max_expectancy, _ = end.run_bootstrap(pooled, per_arm_n, n_iterations=2000, seed=7)
        median = sorted(max_expectancy)[len(max_expectancy) // 2]
        assert median > 0.0

    def test_arms_with_n_below_2_are_excluded_from_the_se_metric_not_zeroed(self):
        pooled = [10.0, -5.0, 20.0]
        per_arm_n = [1, 1]  # every arm has n=1 -> SE always undefined
        _, max_expectancy_minus_se = end.run_bootstrap(pooled, per_arm_n, n_iterations=50, seed=3)
        assert all(v == float("-inf") for v in max_expectancy_minus_se)

    def test_zero_n_arms_are_skipped(self):
        pooled = [10.0, -5.0, 20.0]
        max_expectancy, _ = end.run_bootstrap(pooled, per_arm_n=[3, 0], n_iterations=20, seed=5)
        assert len(max_expectancy) == 20


class TestLoadHaircutPnlsByBook:
    async def _run(self, maker):
        async with maker() as session:
            return await end.load_haircut_pnls_by_book(session)

    @pytest.mark.asyncio
    async def test_pools_current_era_closed_trades_per_book(self, session_maker):
        async with session_maker() as session:
            session.add_all(
                [
                    _book("B01"),
                    _book("B02"),
                    _position("B01", "CLOSED", entry=1.0, exit_value=0.4),  # +60 - 5 haircut = 55
                    _position("B01", "CLOSED", entry=1.0, exit_value=1.4),  # -40 - 5 haircut = -45
                    _position("B01", "OPEN", entry=1.0, exit_value=0.5),  # excluded: not closed
                    _position("B02", "CLOSED", entry=2.0, exit_value=1.0),  # +100 - 5 = 95
                ]
            )
            await session.commit()

        by_book = await self._run(session_maker)
        assert sorted(by_book["B01"]) == [-45.0, 55.0]
        assert by_book["B02"] == [95.0]

    @pytest.mark.asyncio
    async def test_b00_and_b32_are_excluded(self, session_maker):
        async with session_maker() as session:
            session.add_all(
                [
                    _book("B00"),
                    _book("B32"),
                    _position("B00", "CLOSED", entry=1.0, exit_value=0.4),
                    _position("B32", "CLOSED", entry=1.0, exit_value=0.4),
                ]
            )
            await session.commit()

        by_book = await self._run(session_maker)
        assert by_book == {}

    @pytest.mark.asyncio
    async def test_retired_era_trades_are_excluded_after_a_config_sync(self, session_maker):
        # #534: a config sync starts a new evidence era; a trade stamped
        # with the OLD config_hash belongs to a retired era and must not be
        # pooled as if it represents the book's current arm.
        async with session_maker() as session:
            session.add_all(
                [
                    _book("B01", config_hash="new_hash"),
                    _position("B01", "CLOSED", entry=1.0, exit_value=0.4, config_hash="old_hash"),
                    _position("B01", "CLOSED", entry=1.0, exit_value=0.3, config_hash="new_hash"),
                    AuditEventModel(
                        run_at=NOW_ISO, book_id="B01", event_type="BOOK_CONFIG_SYNCED", actor="system", payload={}
                    ),
                ]
            )
            await session.commit()

        by_book = await self._run(session_maker)
        assert len(by_book["B01"]) == 1  # only the new-era trade


class TestRunEmpiricalNullDrillEndToEnd:
    @pytest.mark.asyncio
    async def test_report_shape_and_real_book_ranking(self, session_maker):
        async with session_maker() as session:
            session.add_all(
                [
                    _book("B01"),
                    _book("B02"),
                    *[_position("B01", "CLOSED", entry=1.0, exit_value=0.4) for _ in range(5)],  # winners
                    *[_position("B02", "CLOSED", entry=1.0, exit_value=1.5) for _ in range(5)],  # losers
                ]
            )
            await session.commit()

        report = await end.run_empirical_null_drill(session_maker, n_iterations=100, seed=11)
        assert report.n_books == 2
        assert report.n_pooled_trades == 10
        assert {b.book_id for b in report.books} == {"B01", "B02"}
        b01 = next(b for b in report.books if b.book_id == "B01")
        b02 = next(b for b in report.books if b.book_id == "B02")
        assert b01.expectancy > b02.expectancy
        text = end.format_report(report)
        assert "selection null, pooled-ledger bootstrap, arm-independent" in text
        assert "MEASUREMENT, not yet a threshold" in text
        assert "expected under this construction, not a broken drill" in text

    @pytest.mark.asyncio
    async def test_report_notes_undefined_se_metric_when_every_book_has_n_below_2(self, session_maker):
        async with session_maker() as session:
            session.add_all(
                [
                    _book("B01"),
                    _position("B01", "CLOSED", entry=1.0, exit_value=0.4),  # only one closed trade
                ]
            )
            await session.commit()

        report = await end.run_empirical_null_drill(session_maker, n_iterations=20, seed=2)
        text = end.format_report(report)
        assert "no iteration had an arm with n>=2 — undefined" in text
        assert "(SE undefined, n<2)" in text

    @pytest.mark.asyncio
    async def test_multiple_fills_on_one_position_are_summed(self, session_maker):
        from backend.models import FillModel, OrderModel

        async with session_maker() as session:
            session.add_all(
                [
                    _book("B01"),
                    _position("B01", "CLOSED", entry=1.0, exit_value=0.4, id="pos-multi-fill"),
                    OrderModel(
                        id="o_c1",
                        book_id="B01",
                        position_id="pos-multi-fill",
                        order_ref="basis:B01:o_c1:open",
                        action="OPEN",
                        combo_legs={},
                        order_type="LIMIT",
                        limit_price=-1.0,
                        decision_midpoint=-1.0,
                        status="FILLED",
                        submitted_at="t0",
                        completed_at="t1",
                    ),
                    FillModel(
                        exec_id="e_c1",
                        order_id="o_c1",
                        book_id="B01",
                        con_id=1,
                        side="SLD",
                        quantity=1.0,
                        price=1.0,
                        commission=1.0,
                        fill_time="t1",
                        raw={},
                    ),
                    FillModel(
                        exec_id="e_c2",
                        order_id="o_c1",
                        book_id="B01",
                        con_id=2,
                        side="BOT",
                        quantity=1.0,
                        price=0.4,
                        commission=1.5,
                        fill_time="t1",
                        raw={},
                    ),
                ]
            )
            await session.commit()

        async with session_maker() as session:
            by_book = await end.load_haircut_pnls_by_book(session)
        # realized_pnl(entry=1.0, exit=0.4, 1 contract) = 60.0; haircut 5.0; commissions 1.0+1.5=2.5
        assert by_book["B01"] == [60.0 - 5.0 - 2.5]


class TestReadOnlyGuarantee:
    def _seed_sqlite_file(self, path: Path) -> None:
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{path}")
        Base.metadata.create_all(engine)
        engine.dispose()
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(
                "INSERT INTO books (id, name, config, config_version, config_hash, starting_capital, cash_balance, status, created_at) "
                "VALUES ('B01', 'lab', '{}', 1, 'cafe1234', 10000.0, 10000.0, 'ACTIVE', 't0')"
            )
            conn.commit()
        finally:
            conn.close()

    def test_drill_runs_read_only_against_a_real_db_file(self, tmp_path):
        from backend.restore_drill import readonly_session_maker

        db_path = tmp_path / "sandbox.db"
        self._seed_sqlite_file(db_path)

        import asyncio

        with readonly_session_maker(db_path) as maker:
            report = asyncio.run(end.run_empirical_null_drill(maker, n_iterations=10, seed=1))
        assert report.n_books == 0  # no closed positions seeded — just proving the read path works

    def test_a_write_through_the_same_maker_is_structurally_refused(self, tmp_path):
        from sqlalchemy import text
        from sqlalchemy.exc import OperationalError

        from backend.restore_drill import readonly_session_maker

        db_path = tmp_path / "sandbox.db"
        self._seed_sqlite_file(db_path)

        import asyncio

        with readonly_session_maker(db_path) as maker:

            async def _write():
                async with maker() as session:
                    await session.execute(text("INSERT INTO books (id, name) VALUES ('B99', 'x')"))
                    await session.commit()

            with pytest.raises(OperationalError):
                asyncio.run(_write())
