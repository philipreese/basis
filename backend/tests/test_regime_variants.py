"""Tests for the regime-engine variants (backend/regime_variants.py, #69).

The classification tables in spec/design/executor-paper.md §5 are exact —
each row gets a test, plus the hysteresis latch, the trading-day catalyst
window, and the INSUFFICIENT_DATA path (missing inputs must never be a
silent skip).
"""

import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import LAB_BOOKS, _config_hash
from backend.models import Base, IndexHistoryModel, MarketStateModel, RegimeReadingModel
from backend.regime_variants import (
    INSUFFICIENT_DATA,
    catalysts_within_trading_days,
    classify_v1,
    classify_v2,
    classify_v3,
    major_catalyst_within,
    persist_regime_readings,
    realized_vol_20d,
    sma,
)

TODAY = datetime.date(2026, 8, 18)  # a Tuesday


def _v1(**overrides):
    kwargs = {
        "vix": 15.0,
        "vix3m": 17.0,  # R ≈ 0.882 — healthy contango
        "spy_close": 760.0,
        "spy_sma200": 720.0,  # trend up
        "major_catalyst_soon": False,
    }
    kwargs.update(overrides)
    return classify_v1(**kwargs)


def _v2(**overrides):
    kwargs = {
        "vix": 15.0,
        "vix3m": 17.0,
        "spy_close": 760.0,
        "spy_sma200": 720.0,
        "rv20": 11.0,  # VRP = 4.0 — fat seller's edge
        "major_catalyst_soon": False,
    }
    kwargs.update(overrides)
    return classify_v2(**kwargs)


class TestV1Table:
    def test_backwardation_is_event(self):
        regime, inputs = _v1(vix=20.0, vix3m=19.0)  # R > 1
        assert regime == "EVENT_CATALYST"
        assert inputs["backwardation_event"] is True

    def test_major_catalyst_is_event(self):
        regime, inputs = _v1(major_catalyst_soon=True)
        assert regime == "EVENT_CATALYST"
        assert inputs["backwardation_event"] is False  # catalyst-fired, no latch

    def test_contango_downtrend_is_trending_bear(self):
        regime, _ = _v1(spy_close=700.0)  # below SMA200
        assert regime == "TRENDING_BEAR"

    def test_flattening_curve_uptrend_is_high_vol_neutral(self):
        regime, _ = _v1(vix=16.5, vix3m=17.0)  # R ≈ 0.971 — within [0.95, 1.00)
        assert regime == "HIGH_VOL_NEUTRAL"

    def test_steep_contango_uptrend_is_calm_bull(self):
        regime, _ = _v1()  # R ≈ 0.882
        assert regime == "CALM_BULL"


class TestV1Hysteresis:
    def test_event_latch_holds_until_two_closes_below_exit(self):
        # Yesterday: backwardation event. Today R back to 0.98 — still held.
        _, prior = _v1(vix=20.0, vix3m=19.0)
        regime, inputs = _v1(vix=16.7, vix3m=17.0, prior_inputs=prior)  # R ≈ 0.982
        assert regime == "EVENT_CATALYST"
        assert inputs["hysteresis_hold"] is True

    def test_one_close_below_exit_is_not_enough(self):
        _, prior = _v1(vix=20.0, vix3m=19.0)  # prior R > 1 — not below 0.97
        regime, _ = _v1(vix=16.0, vix3m=17.0, prior_inputs=prior)  # today R ≈ 0.941 < 0.97
        assert regime == "EVENT_CATALYST"  # needs TWO consecutive closes below

    def test_two_closes_below_exit_releases(self):
        _, day1 = _v1(vix=20.0, vix3m=19.0)  # event fires
        _, day2 = _v1(vix=16.0, vix3m=17.0, prior_inputs=day1)  # first close below 0.97, still held
        regime, _ = _v1(vix=15.8, vix3m=17.0, prior_inputs=day2)  # second close below — release
        assert regime == "CALM_BULL"

    def test_no_latch_after_catalyst_fired_event(self):
        _, prior = _v1(major_catalyst_soon=True)  # event, but not backwardation
        regime, _ = _v1(prior_inputs=prior)
        assert regime == "CALM_BULL"  # no hold — sell the relief only after panic


class TestV2Table:
    def test_absent_edge_is_event(self):
        regime, _ = _v2(rv20=16.0)  # VRP = -1.0
        assert regime == "EVENT_CATALYST"

    def test_backwardation_is_event(self):
        regime, _ = _v2(vix=20.0, vix3m=19.0)
        assert regime == "EVENT_CATALYST"

    def test_major_catalyst_is_event(self):
        regime, _ = _v2(major_catalyst_soon=True)
        assert regime == "EVENT_CATALYST"

    def test_fat_edge_uptrend_is_calm_bull(self):
        regime, inputs = _v2()
        assert regime == "CALM_BULL"
        assert inputs["VRP"] == 4.0

    def test_fat_edge_downtrend_is_high_vol_neutral(self):
        regime, _ = _v2(spy_close=700.0)
        assert regime == "HIGH_VOL_NEUTRAL"

    def test_thin_edge_is_neutral_never_directional(self):
        # 0 < VRP < 2.0: the edge exists but is skinny — a size statement,
        # not a bear call (#245). Trend must not change the answer.
        regime, _ = _v2(rv20=14.0)  # VRP = 1.0 — thin, uptrend
        assert regime == "HIGH_VOL_NEUTRAL"
        regime, _ = _v2(rv20=14.0, spy_close=700.0)  # thin, downtrend
        assert regime == "HIGH_VOL_NEUTRAL"


class TestCatalystWindow:
    def test_major_within_three_trading_days(self):
        assert major_catalyst_within(["FOMC:2026-08-20"], TODAY)  # Thu, 2 trading days out

    def test_major_over_weekend_counts_trading_days(self):
        # Friday the 21st → Monday the 24th is 4 trading days from Tuesday the 18th
        assert not major_catalyst_within(["FOMC:2026-08-24"], TODAY)

    def test_minor_catalyst_never_triggers(self):
        assert not major_catalyst_within(["EARNINGS:2026-08-19"], TODAY)

    def test_past_catalyst_ignored(self):
        assert not major_catalyst_within(["FOMC:2026-08-17"], TODAY)


class TestMath:
    def test_rv20_needs_21_closes(self):
        assert realized_vol_20d([100.0] * 20) is None

    def test_rv20_of_constant_prices_is_zero(self):
        assert realized_vol_20d([100.0] * 21) == 0.0

    def test_rv20_annualizes(self):
        closes = [100.0 * (1.01 ** (i % 2)) for i in range(21)]  # alternating ±1%
        rv = realized_vol_20d(closes)
        assert rv is not None
        assert rv > 10.0  # clearly nonzero, annualized

    def test_sma_needs_enough_closes(self):
        assert sma([1.0] * 199, 200) is None
        assert sma([2.0] * 200, 200) == 2.0


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        session.add(
            MarketStateModel(
                id=1,
                current_regime="CALM_BULL",
                spy_price=760.0,
                spy_sma20=755.0,
                vix_close=15.0,
                underlying_ivrs={},
                spy_daily_return=0.002,
                catalyst_dates=[],
                regime_scores={"CALM_BULL": 6.0},
            )
        )
        await session.commit()
    yield maker
    await engine.dispose()


async def _seed_history(maker, symbol: str, closes: list[float], start: datetime.date) -> None:
    async with maker() as session:
        d = start
        for close in closes:
            while d.weekday() >= 5:
                d += datetime.timedelta(days=1)
            session.add(IndexHistoryModel(date=d.isoformat(), symbol=symbol, close=close))
            d += datetime.timedelta(days=1)
        await session.commit()


class TestObservationEngines:
    def test_v4_short_end_inversion_is_event(self):
        from backend.regime_variants import classify_v4

        regime, inputs = classify_v4(vix9d=16.0, vix=15.0, spy_close=760.0, spy_sma200=720.0, major_catalyst_soon=False)
        assert regime == "EVENT_CATALYST"
        assert inputs["S"] > 1.0

    def test_v4_elevated_short_end_is_neutral(self):
        from backend.regime_variants import classify_v4

        regime, _ = classify_v4(vix9d=14.0, vix=15.0, spy_close=760.0, spy_sma200=720.0, major_catalyst_soon=False)
        assert regime == "HIGH_VOL_NEUTRAL"  # S ≈ 0.93

    def test_v4_calm_short_end_follows_trend(self):
        from backend.regime_variants import classify_v4

        regime, _ = classify_v4(vix9d=12.0, vix=15.0, spy_close=760.0, spy_sma200=720.0, major_catalyst_soon=False)
        assert regime == "CALM_BULL"
        regime, _ = classify_v4(vix9d=12.0, vix=15.0, spy_close=700.0, spy_sma200=720.0, major_catalyst_soon=False)
        assert regime == "TRENDING_BEAR"

    def test_ratio_engine_sick_ratio_in_uptrend_is_the_dissent_signal(self):
        from backend.regime_variants import _ratio_regime

        # 70 days of a healthy flat ratio, then the numerator rolls over.
        num = [80.0] * 65 + [76.0] * 5
        den = [100.0] * 70
        outcome = _ratio_regime("HYG/LQD", num, den, spy_close=760.0, spy_sma200=720.0)
        assert outcome is not None
        regime, inputs = outcome
        assert regime == "HIGH_VOL_NEUTRAL"
        assert inputs["healthy"] is False

    def test_ratio_engine_healthy_uptrend_is_calm_bull(self):
        from backend.regime_variants import _ratio_regime

        regime, _ = _ratio_regime("RSP/SPY", [80.0] * 70, [100.0] * 70, spy_close=760.0, spy_sma200=720.0)
        assert regime == "CALM_BULL"

    def test_ratio_engine_short_history_is_none(self):
        from backend.regime_variants import _ratio_regime

        assert _ratio_regime("RSP/SPY", [80.0] * 10, [100.0] * 10, spy_close=760.0, spy_sma200=720.0) is None


class TestPersistReadings:
    @pytest.mark.asyncio
    async def test_all_variants_persisted_with_full_history(self, session_maker):
        start = datetime.date(2025, 8, 1)
        await _seed_history(session_maker, "SPY", [700.0 + i * 0.3 for i in range(210)], start)
        await _seed_history(session_maker, "VIX", [15.0] * 210, start)
        await _seed_history(session_maker, "VIX3M", [17.0] * 210, start)
        async with session_maker() as session:
            results = await persist_regime_readings(session, today=TODAY)
        assert results["V0"] == "CALM_BULL"
        assert results["V1"] == "CALM_BULL"
        assert results["V2"] in ("CALM_BULL", "EVENT_CATALYST")  # depends on RV vs VIX
        async with session_maker() as session:
            rows = (await session.execute(select(RegimeReadingModel))).scalars().all()
        assert {(r.date, r.book_id, r.engine_variant) for r in rows} == {
            (TODAY.isoformat(), "ALL", v) for v in ("V0", "V1", "V2", "V3", "V4", "V5", "V6")
        }

    @pytest.mark.asyncio
    async def test_missing_history_persists_insufficient_data(self, session_maker):
        async with session_maker() as session:
            results = await persist_regime_readings(session, today=TODAY)
        assert results["V0"] == "CALM_BULL"  # control always available
        assert results["V1"] == INSUFFICIENT_DATA
        assert results["V2"] == INSUFFICIENT_DATA
        assert results["V3"] == INSUFFICIENT_DATA
        assert results["V4"] == INSUFFICIENT_DATA
        assert results["V5"] == INSUFFICIENT_DATA
        assert results["V6"] == INSUFFICIENT_DATA
        async with session_maker() as session:
            rows = (await session.execute(select(RegimeReadingModel))).scalars().all()
        assert len(rows) == 7  # never a silent skip

    @pytest.mark.asyncio
    async def test_rerun_same_date_updates_in_place(self, session_maker):
        async with session_maker() as session:
            await persist_regime_readings(session, today=TODAY)
        async with session_maker() as session:
            await persist_regime_readings(session, today=TODAY)
            rows = (await session.execute(select(RegimeReadingModel))).scalars().all()
        assert len(rows) == 7  # PK-stable upsert, no duplicates


class TestClassifyV3:
    """Repaired matrix (design §5 V3): V0's weights, fixed dimensions."""

    def test_calm_contango_uptrend_low_percentile_is_calm_bull(self):
        regime, inputs = classify_v3(
            vix=14.0,
            vix3m=17.0,
            spy_close=760.0,
            spy_sma200=700.0,
            vix_percentile=20.0,
            major_catalyst_soon=False,
            minor_catalyst_soon=False,
        )
        assert regime == "CALM_BULL"
        assert inputs["r_label"] == "R_CALM"

    def test_backwardation_downtrend_is_trending_bear(self):
        regime, inputs = classify_v3(
            vix=35.0,
            vix3m=30.0,
            spy_close=650.0,
            spy_sma200=700.0,
            vix_percentile=95.0,
            major_catalyst_soon=False,
            minor_catalyst_soon=False,
        )
        assert regime == "TRENDING_BEAR"
        assert inputs["r_label"] == "R_BACKWARDATION"

    def test_major_catalyst_within_5td_wins(self):
        regime, _ = classify_v3(
            vix=18.0,
            vix3m=18.5,
            spy_close=705.0,
            spy_sma200=700.0,
            vix_percentile=20.0,
            major_catalyst_soon=True,
            minor_catalyst_soon=False,
        )
        assert regime == "EVENT_CATALYST"

    def test_five_trading_day_window_is_tighter_than_v0s_14_calendar(self):
        today = datetime.date(2026, 8, 18)  # a Tuesday
        # 2026-08-27 is 7 trading days out — inside V0's 14-calendar window,
        # outside V3's 5-trading-day window.
        assert catalysts_within_trading_days(["FOMC:2026-08-27"], today, 5) == (False, False)
        assert catalysts_within_trading_days(["FOMC:2026-08-21"], today, 5) == (True, False)
        assert catalysts_within_trading_days(["EARNINGS:2026-08-20"], today, 5) == (False, True)
        assert catalysts_within_trading_days(["watch jackson hole"], today, 5) == (False, False)


class TestLabBookAllocation:
    def test_matrix_has_unique_ids_and_the_full_core_grid(self):
        ids = [spec["id"] for spec in LAB_BOOKS]
        assert len(ids) == len(set(ids)) == 32
        core = {
            (spec["config"]["engine_variant"], spec["config"]["underlying"])
            for spec in LAB_BOOKS
            if spec["id"] in {"B01", "B02", "B03", "B04", "B05", "B06"}
        }
        assert core == {(v, u) for v in ("V0", "V1", "V2") for u in ("XSP", "SPY")}

    def test_every_book_asks_a_distinct_question(self):
        # One question per book (ADR-0009): identical configs would make two
        # books the same experiment and split its sample.
        hashes = {_config_hash(spec["config"]) for spec in LAB_BOOKS}
        assert len(hashes) == len(LAB_BOOKS)

    def test_experiment_arms_stay_on_xsp_where_assignment_matters(self):
        # B17 holds to 7 DTE — only safe cash-settled (No-Stock Mandate).
        b17 = next(spec for spec in LAB_BOOKS if spec["id"] == "B17")
        assert b17["config"]["underlying"] == "XSP"

    @pytest.mark.asyncio
    async def test_init_db_seeds_books_with_control_rows(self, tmp_path, monkeypatch):
        import backend.database as db_mod
        from backend.models import BookModel, TradingControlModel

        url = f"sqlite+aiosqlite:///{(tmp_path / 'seed.db').as_posix()}"
        monkeypatch.setattr(db_mod, "DATABASE_URL", url)
        engine = create_async_engine(url)
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(db_mod, "async_session_maker", maker)
        try:
            await db_mod.init_db()
            async with maker() as session:
                books = (await session.execute(select(BookModel))).scalars().all()
                controls = (await session.execute(select(TradingControlModel))).scalars().all()
            expected = {spec["id"] for spec in LAB_BOOKS}
            book_ids = {b.id for b in books}
            assert {"B00"} | expected <= book_ids
            control_scopes = {c.scope for c in controls}
            assert {"GLOBAL"} | expected <= control_scopes
            b02 = next(b for b in books if b.id == "B02")
            assert b02.config["engine_variant"] == "V1"
            assert b02.config["underlying"] == "XSP"
            assert b02.config_hash  # Live Gate attaches to (book_id, config_hash)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_init_db_syncs_changed_seed_configs_into_existing_books(self, tmp_path, monkeypatch):
        # Audit II R2 (#436): books were only inserted when missing, so a
        # seeds.py config fix (#351's two slots, #411's dedup) silently never
        # reached an existing database. A changed seed hash must update the
        # stored config with a version bump (the hash split keeps old and new
        # evidence unpooled, #284).
        import backend.database as db_mod
        from backend.models import AuditEventModel, BookModel

        url = f"sqlite+aiosqlite:///{(tmp_path / 'sync.db').as_posix()}"
        monkeypatch.setattr(db_mod, "DATABASE_URL", url)
        engine = create_async_engine(url)
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(db_mod, "async_session_maker", maker)
        try:
            await db_mod.init_db()
            # Simulate a DB created under an older seeds.py: stale config.
            async with maker() as session:
                b32 = await session.get(BookModel, "B32")
                stale = dict(b32.config)
                stale["envelope"] = {**stale["envelope"], "max_positions": 1}
                b32.config = stale
                b32.config_hash = _config_hash(stale)
                b32.config_version = 1
                await session.commit()
            await db_mod.init_db()
            async with maker() as session:
                b32 = await session.get(BookModel, "B32")
                audits = (
                    (await session.execute(select(AuditEventModel).filter_by(event_type="BOOK_CONFIG_SYNCED")))
                    .scalars()
                    .all()
                )
            b32_spec = next(spec for spec in LAB_BOOKS if spec["id"] == "B32")
            assert b32.config == b32_spec["config"]
            assert b32.config_hash == _config_hash(b32_spec["config"])
            assert b32.config_version == 2
            assert any(a.book_id == "B32" for a in audits)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_sync_audit_payload_carries_a_diagnosable_diff(self, tmp_path, monkeypatch):
        # #482: a bare hash change made an unexpected revert (an operator's
        # out-of-band DB edit getting clobbered) undiagnosable from the audit
        # row alone — the diff makes it self-contained.
        import backend.database as db_mod
        from backend.models import AuditEventModel, BookModel

        url = f"sqlite+aiosqlite:///{(tmp_path / 'sync.db').as_posix()}"
        monkeypatch.setattr(db_mod, "DATABASE_URL", url)
        engine = create_async_engine(url)
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(db_mod, "async_session_maker", maker)
        try:
            await db_mod.init_db()
            async with maker() as session:
                b32 = await session.get(BookModel, "B32")
                stale = dict(b32.config)
                stale["envelope"] = {**stale["envelope"], "max_positions": 1}
                b32.config = stale
                b32.config_hash = _config_hash(stale)
                await session.commit()
            await db_mod.init_db()
            async with maker() as session:
                (audit,) = (
                    (
                        await session.execute(
                            select(AuditEventModel).filter_by(event_type="BOOK_CONFIG_SYNCED", book_id="B32")
                        )
                    )
                    .scalars()
                    .all()
                )
            b32_spec = next(spec for spec in LAB_BOOKS if spec["id"] == "B32")
            assert audit.payload["diff"] == {
                "envelope": {"from": stale["envelope"], "to": b32_spec["config"]["envelope"]}
            }
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_sync_alerts_when_a_book_with_trade_history_reverts(self, tmp_path, monkeypatch):
        # #482: a fresh book has no orders yet to distinguish "expected seed
        # rollout" from "someone hand-edited a live book" — the alert is
        # scoped to the case that actually matters: a book that has already
        # traded reverting out from under an out-of-band edit.
        from unittest.mock import patch

        import backend.database as db_mod
        from backend.models import BookModel, OrderModel

        url = f"sqlite+aiosqlite:///{(tmp_path / 'sync.db').as_posix()}"
        monkeypatch.setattr(db_mod, "DATABASE_URL", url)
        engine = create_async_engine(url)
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(db_mod, "async_session_maker", maker)
        try:
            await db_mod.init_db()
            async with maker() as session:
                b32 = await session.get(BookModel, "B32")
                stale = dict(b32.config)
                stale["envelope"] = {**stale["envelope"], "max_positions": 1}
                b32.config = stale
                b32.config_hash = _config_hash(stale)
                session.add(
                    OrderModel(
                        id="o_hist",
                        book_id="B32",
                        position_id=None,
                        order_ref="basis:B32:o_hist:open",
                        ib_order_id=1,
                        ib_perm_id=1,
                        action="OPEN",
                        combo_legs={},
                        order_type="LIMIT",
                        limit_price=-1.0,
                        decision_midpoint=-1.0,
                        status="FILLED",
                        submitted_at="t0",
                        completed_at="t1",
                        encumbered_risk=0.0,
                    )
                )
                await session.commit()
            with patch("backend.operator.send_ntfy") as mock_ntfy:
                await db_mod.init_db()
            mock_ntfy.assert_called_once()
            assert "B32" in mock_ntfy.call_args.args[0]
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_sync_does_not_alert_a_book_with_no_trade_history(self, tmp_path, monkeypatch):
        # #482: the common case (a genuine seeds.py rollout hitting brand-new
        # or never-traded books) must stay quiet — not every sync is a
        # suspicious revert.
        from unittest.mock import patch

        import backend.database as db_mod
        from backend.models import BookModel

        url = f"sqlite+aiosqlite:///{(tmp_path / 'sync.db').as_posix()}"
        monkeypatch.setattr(db_mod, "DATABASE_URL", url)
        engine = create_async_engine(url)
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(db_mod, "async_session_maker", maker)
        try:
            await db_mod.init_db()
            async with maker() as session:
                b32 = await session.get(BookModel, "B32")
                stale = dict(b32.config)
                stale["envelope"] = {**stale["envelope"], "max_positions": 1}
                b32.config = stale
                b32.config_hash = _config_hash(stale)
                await session.commit()
            with patch("backend.operator.send_ntfy") as mock_ntfy:
                await db_mod.init_db()
            mock_ntfy.assert_not_called()
        finally:
            await engine.dispose()
