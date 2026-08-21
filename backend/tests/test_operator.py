"""Tests for the Operator nightly pipeline (backend/operator.py, #23)."""

from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import operator
from backend.database import SEED_PLAYBOOKS, SEED_PORTFOLIO_CONFIG, SEED_POSITIONS
from backend.models import (
    Base,
    MarketStateModel,
    PlaybookDefinitionModel,
    PortfolioConfigModel,
    PositionModel,
)
from backend.operator import compose_digest, run_evening_operation, send_ntfy

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
                spy_price=758.0,
                spy_sma20=750.0,
                vix_close=14.5,
                underlying_ivrs={"SPY": 25.0},
                spy_daily_return=0.005,
                catalyst_dates=[],
                regime_scores={
                    "CALM_BULL": 7.0,
                    "HIGH_VOL_NEUTRAL": 0.0,
                    "TRENDING_BEAR": -3.0,
                    "EVENT_CATALYST": -1.0,
                },
            )
        )
        for p_data in SEED_POSITIONS:
            session.add(
                PositionModel(
                    id=p_data["id"],
                    underlying=p_data["underlying"],
                    strategy_type=p_data["strategy_type"],
                    legs=p_data["legs"],
                    entry_date=p_data["entry_date"],
                    expiration_date=p_data["expiration_date"],
                    entry_premium=p_data["entry_premium"],
                    premium_direction=p_data["premium_direction"],
                    current_value_per_share=p_data["current_value_per_share"],
                    contracts=p_data["contracts"],
                    max_profit=p_data["max_profit"],
                    max_loss=p_data["max_loss"],
                    notes=p_data["notes"],
                    rolls=p_data["rolls"],
                    status=p_data["status"],
                    journal=p_data["journal"],
                )
            )
        for pb_data in SEED_PLAYBOOKS:
            session.add(
                PlaybookDefinitionModel(
                    id=pb_data["id"],
                    version=pb_data["version"],
                    name=pb_data["name"],
                    underlying_ticker=pb_data["underlying_ticker"],
                    strategy_type=pb_data["strategy_type"],
                    enabled=pb_data.get("enabled", True),
                    entry_filters=pb_data["entry_filters"],
                    execution_specs=pb_data["execution_specs"],
                    exit_rules=pb_data["exit_rules"],
                )
            )
        await session.commit()
    yield maker
    await engine.dispose()


TELEMETRY = {"spy_price": 760.0, "spy_sma20": 750.0, "vix_close": 16.0, "spy_daily_return": 0.004}


class TestRunEveningOperation:
    @pytest.mark.asyncio
    async def test_full_run_with_live_telemetry(self, session_maker):
        with (
            patch.object(operator, "fetch_market_telemetry", return_value=TELEMETRY),
            patch.object(operator, "fetch_options_latest_quotes", return_value={}),
            patch.object(operator, "fetch_index_daily_closes", return_value=None),
        ):
            title, body, _priority = await run_evening_operation(session_maker)
        assert title.startswith("basis evening:")
        assert "Regime" in body
        assert "SPY 760.00" in body
        assert "stored data" not in body  # telemetry was live

    @pytest.mark.asyncio
    async def test_degrades_to_stored_state_when_fetch_fails(self, session_maker):
        with (
            patch.object(operator, "fetch_market_telemetry", return_value=None),
            patch.object(operator, "fetch_options_latest_quotes", return_value={}),
            patch.object(operator, "fetch_index_daily_closes", return_value=None),
        ):
            _title, body, _priority = await run_evening_operation(session_maker)
        assert "stored data" in body
        assert "SPY 758.00" in body  # stored value, not live

    @pytest.mark.asyncio
    async def test_persists_recomputed_regime(self, session_maker):
        from sqlalchemy import select

        with (
            patch.object(operator, "fetch_market_telemetry", return_value=TELEMETRY),
            patch.object(operator, "fetch_options_latest_quotes", return_value={}),
            patch.object(operator, "fetch_index_daily_closes", return_value=None),
        ):
            await run_evening_operation(session_maker)
        async with session_maker() as session:
            state = (await session.execute(select(MarketStateModel).filter_by(id=1))).scalar_one()
            assert state.spy_price == 760.0
            assert state.vix_close == 16.0


class TestComposeDigest:
    def _lifecycle(self, priority: str) -> dict:
        return {
            "position_id": "p1",
            "underlying": "SPY",
            "strategy_type": "LONG_STRADDLE",
            "priority": priority,
            "reason": "test reason",
        }

    def test_quiet_night(self):
        title, _body, priority = compose_digest(
            regime="CALM_BULL",
            spy_price=758.0,
            vix_close=14.5,
            telemetry_live=True,
            positions_repriced=0,
            lifecycle=[],
            safeguards=[],
            scan_result=None,
        )
        assert "all quiet" in title
        assert priority == "default"

    def test_p1_escalates_priority(self):
        title, body, priority = compose_digest(
            regime="CALM_BULL",
            spy_price=758.0,
            vix_close=14.5,
            telemetry_live=True,
            positions_repriced=1,
            lifecycle=[self._lifecycle("P1 — CLOSE NOW")],
            safeguards=[],
            scan_result=None,
        )
        assert "1 CLOSE NOW" in title
        assert priority == "high"
        assert "test reason" in body

    def test_safeguard_escalates_priority(self):
        _title, body, priority = compose_digest(
            regime="CALM_BULL",
            spy_price=758.0,
            vix_close=14.5,
            telemetry_live=True,
            positions_repriced=0,
            lifecycle=[],
            safeguards=[{"type": "CAPITAL_DEPLOYED", "severity": "WARNING", "message": "over limit"}],
            scan_result=None,
        )
        assert priority == "high"
        assert "CAPITAL_DEPLOYED" in body

    def test_p1_with_close_in_flight_does_not_escalate_or_re_demand_a_close(self):
        # #602: a close already submitted/staged is being handled — must not
        # re-page the operator or count toward the urgent title/priority.
        lifecycle_item = self._lifecycle("P1 — CLOSE NOW")
        lifecycle_item["close_in_flight"] = True
        lifecycle_item["close_in_flight_since"] = "2026-08-21T21:45:00+00:00"
        title, body, priority = compose_digest(
            regime="CALM_BULL",
            spy_price=758.0,
            vix_close=14.5,
            telemetry_live=True,
            positions_repriced=0,
            lifecycle=[lifecycle_item],
            safeguards=[],
            scan_result=None,
        )
        assert "all quiet" in title  # not counted as an actionable CLOSE NOW
        assert priority == "default"  # not escalated
        assert "close already in flight" in body
        assert "2026-08-21T21:45:00+00:00" in body

    def test_p1_with_staged_close_and_no_submitted_at_still_shows_as_in_flight(self):
        lifecycle_item = self._lifecycle("P1 — CLOSE NOW")
        lifecycle_item["close_in_flight"] = True
        lifecycle_item["close_in_flight_since"] = None
        _title, body, priority = compose_digest(
            regime="CALM_BULL",
            spy_price=758.0,
            vix_close=14.5,
            telemetry_live=True,
            positions_repriced=0,
            lifecycle=[lifecycle_item],
            safeguards=[],
            scan_result=None,
        )
        assert priority == "default"
        assert "staged, awaiting the next submission attempt" in body

    def test_a_mix_of_actionable_and_in_flight_p1s_only_escalates_for_the_actionable_one(self):
        actionable = self._lifecycle("P1 — CLOSE NOW")
        actionable["position_id"] = "actionable"
        in_flight = self._lifecycle("P1 — CLOSE NOW")
        in_flight["position_id"] = "handled"
        in_flight["close_in_flight"] = True
        in_flight["close_in_flight_since"] = "t0"
        title, body, priority = compose_digest(
            regime="CALM_BULL",
            spy_price=758.0,
            vix_close=14.5,
            telemetry_live=True,
            positions_repriced=0,
            lifecycle=[actionable, in_flight],
            safeguards=[],
            scan_result=None,
        )
        assert "1 CLOSE NOW" in title
        assert priority == "high"
        assert "close already in flight" in body  # the in-flight one is still visible, not silently dropped


class TestPersistIndexHistory:
    """index_history ingestion (#62) — V1/V2 regime variants read this table."""

    _ROWS: ClassVar[list[tuple[str, float]]] = [("2026-08-14", 15.2), ("2026-08-15", 15.8), ("2026-08-17", 16.1)]

    @pytest.mark.asyncio
    async def test_backfills_both_symbols_on_empty_table(self, session_maker):
        from sqlalchemy import select

        from backend.models import IndexHistoryModel

        with patch.object(operator, "fetch_index_daily_closes", return_value=self._ROWS) as mock_fetch:
            async with session_maker() as session:
                written = await operator.persist_index_history(session)
        assert written == 3 * len(operator.INDEX_SYMBOLS)
        assert {c.args[0] for c in mock_fetch.call_args_list} == set(operator.INDEX_SYMBOLS)
        # Empty table → full backfill window requested
        assert all(c.args[1] == operator.INDEX_BACKFILL_DAYS for c in mock_fetch.call_args_list)
        async with session_maker() as session:
            rows = (await session.execute(select(IndexHistoryModel))).scalars().all()
        assert len(rows) == 3 * len(operator.INDEX_SYMBOLS)

    @pytest.mark.asyncio
    async def test_rerun_is_idempotent_and_uses_topup_window(self, session_maker):
        with patch.object(operator, "fetch_index_daily_closes", return_value=self._ROWS):
            async with session_maker() as session:
                await operator.persist_index_history(session)
        with patch.object(operator, "fetch_index_daily_closes", return_value=self._ROWS) as mock_fetch:
            async with session_maker() as session:
                written = await operator.persist_index_history(session)
        assert written == 0  # all dates already stored — no duplicates, no PK crash
        assert all(c.args[1] == operator.INDEX_TOPUP_DAYS for c in mock_fetch.call_args_list)

    @pytest.mark.asyncio
    async def test_fetch_failure_writes_nothing_and_does_not_raise(self, session_maker):
        with patch.object(operator, "fetch_index_daily_closes", return_value=None):
            async with session_maker() as session:
                written = await operator.persist_index_history(session)
        assert written == 0

    @pytest.mark.asyncio
    async def test_new_dates_appended_to_existing_history(self, session_maker):
        with patch.object(operator, "fetch_index_daily_closes", return_value=self._ROWS):
            async with session_maker() as session:
                await operator.persist_index_history(session)
        newer = [*self._ROWS[1:], ("2026-08-18", 16.4)]
        with patch.object(operator, "fetch_index_daily_closes", return_value=newer):
            async with session_maker() as session:
                written = await operator.persist_index_history(session)
        assert written == len(operator.INDEX_SYMBOLS)  # one new date per symbol


class TestSendNtfy:
    def test_skipped_without_topic(self, monkeypatch):
        monkeypatch.delenv("NTFY_TOPIC", raising=False)
        assert send_ntfy("t", "b") is False

    def test_posts_to_topic(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC", "basis-test-topic")
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        with patch.object(operator.httpx, "post", return_value=mock_resp) as mock_post:
            assert send_ntfy("Title", "Body", "high") is True
        url = mock_post.call_args[0][0]
        assert url.endswith("/basis-test-topic")
        assert mock_post.call_args.kwargs["headers"]["Priority"] == "high"

    def test_returns_false_on_network_error(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC", "basis-test-topic")
        with patch.object(operator.httpx, "post", side_effect=RuntimeError("connection refused")):
            assert send_ntfy("Title", "Body") is False

    def test_non_ascii_title_still_delivers(self, monkeypatch):
        # #560: a mocked `httpx.post` (the pattern the tests above use)
        # bypasses httpx's real request-construction entirely and can't
        # catch this class of bug. httpx raises UnicodeEncodeError while
        # BUILDING the request for any non-ASCII str header value — before
        # anything reaches the network — and the executor's urgent-push
        # title ("⛔ basis executor alerts", executor.py) is hardcoded with
        # an emoji. The broad `except Exception` in send_ntfy swallowed
        # that client-side error every single time and reported False,
        # permanently UNDELIVERED, with nothing ever attempted. This test
        # exercises httpx's REAL header-encoding path (via httpx.Request,
        # the same construction httpx.post performs internally) instead of
        # a shallow mock, so a regression here fails loudly again.
        import httpx

        monkeypatch.setenv("NTFY_TOPIC", "basis-test-topic")

        def real_header_encoding_post(url, content=None, headers=None, timeout=None):
            httpx.Request("POST", url, content=content, headers=headers)  # raises exactly like httpx.post would
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            return resp

        with patch.object(operator.httpx, "post", side_effect=real_header_encoding_post):
            assert send_ntfy("⛔ basis executor alerts", "Body", "urgent") is True


class TestSendNtfyWithRetry:
    def test_transient_failure_is_retried_to_success(self, monkeypatch):
        # H2 (#277): one blip must not silence the nightly digest.
        monkeypatch.setenv("NTFY_TOPIC", "basis-test-topic")
        ok = MagicMock()
        ok.raise_for_status.return_value = None
        with (
            patch.object(operator.httpx, "post", side_effect=[RuntimeError("blip"), ok]) as mock_post,
            patch.object(operator.time, "sleep") as mock_sleep,
        ):
            assert operator.send_ntfy_with_retry("Title", "Body") is True
        assert mock_post.call_count == 2
        mock_sleep.assert_called_once()

    def test_exhausted_retries_return_false(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC", "basis-test-topic")
        with (
            patch.object(operator.httpx, "post", side_effect=RuntimeError("down")) as mock_post,
            patch.object(operator.time, "sleep"),
        ):
            assert operator.send_ntfy_with_retry("Title", "Body", attempts=3) is False
        assert mock_post.call_count == 3

    def test_missing_topic_fails_immediately_without_retry(self, monkeypatch):
        monkeypatch.delenv("NTFY_TOPIC", raising=False)
        with patch.object(operator.time, "sleep") as mock_sleep:
            assert operator.send_ntfy_with_retry("Title", "Body") is False
        mock_sleep.assert_not_called()


class TestAlertCrash:
    def test_writes_audit_row_and_retries_ntfy(self, tmp_path, monkeypatch):
        # Audit II R2 (#417): crash alerts were bare send_ntfy — silent when
        # ntfy was down, which the crash itself may have caused. The audit
        # row is the durable half; ntfy goes through the retry path.
        import backend.database as db_mod
        from backend.models import Base

        url = f"sqlite:///{(tmp_path / 'crash.db').as_posix()}"
        from sqlalchemy import create_engine, text

        engine = create_engine(url)
        Base.metadata.create_all(engine)
        engine.dispose()
        monkeypatch.setattr(db_mod, "DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'crash.db').as_posix()}")
        with patch.object(operator, "send_ntfy_with_retry") as mock_retry:
            operator.alert_crash("basis executor CRASHED", "RuntimeError: boom", "urgent")
        mock_retry.assert_called_once_with("basis executor CRASHED", "RuntimeError: boom", "urgent")
        engine = create_engine(url)
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT event_type, actor, payload FROM audit_events")).fetchall()
        engine.dispose()
        assert len(rows) == 1
        assert rows[0][0] == "CRASH_ALERT" and rows[0][1] == "system"
        assert "boom" in rows[0][2]

    def test_audit_failure_never_blocks_the_ntfy_half(self, monkeypatch):
        import backend.database as db_mod

        monkeypatch.setattr(db_mod, "DATABASE_URL", "sqlite+aiosqlite:///Z:/nope/nope.db")
        with patch.object(operator, "send_ntfy_with_retry") as mock_retry:
            operator.alert_crash("t", "b")
        mock_retry.assert_called_once()

    def test_event_type_distinguishes_scheduler_alerts_from_crashes(self, tmp_path, monkeypatch):
        # #472: every _urgent/alert_crash call used to land as CRASH_ALERT
        # regardless — a scheduler/config condition (Gateway never came up,
        # a backup step failing) is not the same audit signal as an
        # unhandled exception.
        import backend.database as db_mod
        from backend.models import Base

        db_path = tmp_path / "crash.db"
        url = f"sqlite:///{db_path.as_posix()}"
        from sqlalchemy import create_engine, text

        engine = create_engine(url)
        Base.metadata.create_all(engine)
        engine.dispose()
        monkeypatch.setattr(db_mod, "DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
        with patch.object(operator, "send_ntfy_with_retry"):
            operator.alert_crash(
                "basis executor NOT RUN", "gateway never opened", "urgent", event_type="SCHEDULER_ALERT"
            )
        engine = create_engine(url)
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT event_type FROM audit_events")).fetchall()
        engine.dispose()
        assert rows == [("SCHEDULER_ALERT",)]

    def test_skips_the_audit_row_for_an_in_memory_database(self, monkeypatch):
        # #472: DATABASE_URL.replace(...) maps ":memory:" to a brand-new,
        # empty in-memory database distinct from the process's real one —
        # writing there is worse than useless, it just silently vanishes.
        import backend.database as db_mod

        monkeypatch.setattr(db_mod, "DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        with patch.object(operator, "send_ntfy_with_retry") as mock_retry:
            operator.alert_crash("t", "b")
        mock_retry.assert_called_once()  # the ntfy half still runs

    def test_skips_the_audit_row_for_a_non_sqlite_url(self, monkeypatch):
        # #472: .replace("sqlite+aiosqlite://", "sqlite://") is a silent
        # no-op on any other scheme — create_engine would then try to open a
        # Postgres URL with sqlite-only connect_args and fail unpredictably.
        import backend.database as db_mod

        monkeypatch.setattr(db_mod, "DATABASE_URL", "postgresql+asyncpg://user:pw@host/db")
        with patch.object(operator, "send_ntfy_with_retry") as mock_retry:
            operator.alert_crash("t", "b")
        mock_retry.assert_called_once()

    def test_installs_wal_and_busy_timeout_pragmas(self, tmp_path, monkeypatch):
        # #472: under DB contention — plausibly the crash's own cause — a
        # zero-timeout connection fails immediately instead of waiting,
        # losing exactly the audit row that matters most.
        import backend.database as db_mod
        from backend.models import Base

        db_path = tmp_path / "crash.db"
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{db_path.as_posix()}")
        Base.metadata.create_all(engine)
        engine.dispose()
        monkeypatch.setattr(db_mod, "DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
        with patch.object(operator, "send_ntfy_with_retry"):
            operator.alert_crash("t", "b")
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode == "wal"
