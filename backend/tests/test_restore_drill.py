"""Tests for the automated restore drill (backend/restore_drill.py, #640).

Everything here is mock-broker / real-in-memory-or-tmp-sqlite — no real
Gateway, no real network. Two things matter most: (1) the mutation-proof
wrapper raises on every mutating broker method, known or not, and (2) the
sandboxed drill never opens the production DB path.
"""

import datetime
import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import restore_drill as rd
from backend.broker import LegPosition, OpenOrderInfo, ReconcileReport, RefState
from backend.dates import MARKET_TZ
from backend.models import Base, BookModel, OrderModel, PositionModel, ReconciliationRunModel


class FakeInnerBroker:
    """Stands in for BrokerSession: read methods return canned data, every
    mutating method records a call so a test can prove it was NEVER reached
    through the wrapper."""

    def __init__(self):
        self.calls: list[str] = []
        self._report = ReconcileReport(states={})
        self._positions: list[LegPosition] = []
        self._open_orders: list[OpenOrderInfo] = []
        self._executions: list = []

    def open(self):
        self.calls.append("open")

    def close(self):
        self.calls.append("close")

    def reconcile(self, refs, since=None):
        self.calls.append("reconcile")
        return self._report

    def positions(self):
        self.calls.append("positions")
        return self._positions

    def open_orders(self):
        self.calls.append("open_orders")
        return self._open_orders

    def executions(self, since=None):
        self.calls.append("executions")
        return self._executions

    def place_spread(self, *a, **k):
        self.calls.append("place_spread")

    def close_spread(self, *a, **k):
        self.calls.append("close_spread")

    def cancel_by_ref(self, *a, **k):
        self.calls.append("cancel_by_ref")

    def cancel(self, *a, **k):
        self.calls.append("cancel")

    def preview_spread(self, *a, **k):
        self.calls.append("preview_spread")

    def wait_for_terminal(self, *a, **k):
        self.calls.append("wait_for_terminal")


class TestReadOnlyBroker:
    def test_read_methods_delegate(self):
        inner = FakeInnerBroker()
        broker = rd.ReadOnlyBroker(inner)
        broker.open()
        assert broker.reconcile(["ref1"]) is inner._report
        assert broker.positions() == []
        assert broker.open_orders() == []
        assert broker.executions() == []
        broker.close()
        assert inner.calls == ["open", "reconcile", "positions", "open_orders", "executions", "close"]

    @pytest.mark.parametrize(
        "method",
        ["preview_spread", "place_spread", "close_spread", "cancel_by_ref", "cancel", "wait_for_terminal"],
    )
    def test_every_known_mutating_method_is_blocked(self, method):
        inner = FakeInnerBroker()
        broker = rd.ReadOnlyBroker(inner)
        with pytest.raises(rd.MutatingBrokerCallBlockedError, match=method):
            getattr(broker, method)("arg", kw="val")
        # The block happens BEFORE the inner call — the real broker never sees it.
        assert method not in inner.calls
        assert broker.mutation_attempts == [method]

    def test_an_undefined_future_mutating_method_is_blocked_too(self):
        # #640: the structural guarantee is "unknown = blocked", not "blocked
        # = everything on today's list" — a method this wrapper was never
        # taught about (e.g. added to BrokerSession later) must still raise,
        # not silently forward through some __getattr__ passthrough.
        inner = FakeInnerBroker()
        inner.roll_spread = lambda *a, **k: inner.calls.append("roll_spread")
        broker = rd.ReadOnlyBroker(inner)
        with pytest.raises(rd.MutatingBrokerCallBlockedError, match="roll_spread"):
            broker.roll_spread()
        assert "roll_spread" not in inner.calls

    def test_context_manager_opens_and_closes(self):
        inner = FakeInnerBroker()
        with rd.ReadOnlyBroker(inner) as broker:
            assert isinstance(broker, rd.ReadOnlyBroker)
        assert inner.calls == ["open", "close"]


class TestBackupSelection:
    def test_find_oldest_backup_picks_the_earliest_date(self, tmp_path):
        for name in ("basis.2026-08-15.db", "basis.2026-08-01.db", "basis.2026-08-10.db"):
            (tmp_path / name).write_text("x")
        chosen = rd.find_oldest_backup(tmp_path)
        assert chosen.name == "basis.2026-08-01.db"

    def test_find_oldest_backup_returns_none_when_empty(self, tmp_path):
        assert rd.find_oldest_backup(tmp_path) is None

    def test_find_oldest_backup_ignores_non_dated_files(self, tmp_path):
        (tmp_path / "basis.db").write_text("x")
        (tmp_path / "basis.2026-08-05.db").write_text("x")
        chosen = rd.find_oldest_backup(tmp_path)
        assert chosen.name == "basis.2026-08-05.db"

    def test_stage_sandbox_copy_never_modifies_the_original(self, tmp_path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        backup = backup_dir / "basis.2026-08-01.db"
        backup.write_text("original content")
        (backup_dir / "basis.2026-08-01.db-wal").write_text("wal content")
        original_stat = backup.stat()

        scratch = tmp_path / "scratch"
        scratch.mkdir()
        staged = rd.stage_sandbox_copy(backup, scratch)

        assert staged.parent == scratch
        assert staged != backup
        assert staged.read_text() == "original content"
        assert (scratch / "basis.2026-08-01.db-wal").read_text() == "wal content"
        # The original is untouched.
        assert backup.read_text() == "original content"
        assert backup.stat().st_mtime == original_stat.st_mtime


class TestReadonlySessionMaker:
    def _seed_sqlite_file(self, path: Path) -> None:
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{path}")
        Base.metadata.create_all(engine)
        engine.dispose()
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(
                "INSERT INTO books (id, name, config, config_version, config_hash, starting_capital, cash_balance, status, created_at) VALUES ('B01', 'lab', '{}', 1, '', 10000.0, 10000.0, 'ACTIVE', 't0')"
            )
            conn.commit()
        finally:
            conn.close()

    def test_reads_succeed(self, tmp_path):
        db_path = tmp_path / "sandbox.db"
        self._seed_sqlite_file(db_path)
        with rd.readonly_session_maker(db_path) as maker:
            import asyncio

            from sqlalchemy import text

            async def _read():
                async with maker() as session:
                    result = await session.execute(text("SELECT name FROM books WHERE id='B01'"))
                    return result.scalar_one()

            assert asyncio.run(_read()) == "lab"

    def test_writes_are_structurally_refused(self, tmp_path):
        # #640: the read-only guarantee is a literal SQLite mode=ro
        # connection, not a convention the pipeline is trusted to honor — a
        # write attempt must raise at the driver.
        db_path = tmp_path / "sandbox.db"
        self._seed_sqlite_file(db_path)
        with rd.readonly_session_maker(db_path) as maker:
            import asyncio

            from sqlalchemy import text
            from sqlalchemy.exc import OperationalError

            async def _write():
                async with maker() as session:
                    await session.execute(text("INSERT INTO books (id, name) VALUES ('B99', 'x')"))
                    await session.commit()

            with pytest.raises(OperationalError):
                asyncio.run(_write())

    def test_sandbox_drill_never_points_at_the_production_db_path(self, tmp_path, monkeypatch):
        # Simulates a "production" file sitting right next to the backup
        # dir, then runs the sandbox drill (with the gateway machinery
        # stubbed out — no real IB Gateway in a unit test) and asserts the
        # session_maker handed to the analysis is bound to a scratch copy,
        # never to the production path.
        production_db = tmp_path / "basis.db"
        self._seed_sqlite_file(production_db)
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        backup = backup_dir / "basis.2026-08-01.db"
        self._seed_sqlite_file(backup)

        opened_paths: list[Path] = []
        real_readonly_session_maker = rd.readonly_session_maker

        def _spy(db_path):
            opened_paths.append(Path(db_path))
            return real_readonly_session_maker(db_path)

        monkeypatch.setattr(rd, "readonly_session_maker", _spy)

        def _fake_run_with_gateway(work):
            fake_broker = rd.ReadOnlyBroker(FakeInnerBroker())
            return 0, work(fake_broker)

        monkeypatch.setattr(rd, "_run_with_gateway", _fake_run_with_gateway)

        code = rd.run_sandbox_drill(backup_dir=backup_dir)
        assert code == 0
        assert len(opened_paths) == 1
        assert opened_paths[0] != production_db
        assert opened_paths[0].parent != backup_dir  # a scratch dir, not the backups dir either
        assert production_db.exists()  # untouched, still there


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
                name="lab",
                config={},
                config_version=1,
                config_hash="",
                starting_capital=10000.0,
                cash_balance=10000.0,
                status="ACTIVE",
                created_at="t0",
            )
        )
        await session.commit()
    yield maker
    await engine.dispose()


def _order(order_id, ref, status, action="OPEN"):
    return OrderModel(
        id=order_id,
        book_id="B01",
        position_id=None,
        order_ref=ref,
        ib_order_id=1,
        ib_perm_id=100,
        action=action,
        combo_legs={"legs": [], "quantity": 1},
        order_type="LIMIT",
        limit_price=-1.0,
        decision_midpoint=-1.0,
        status=status,
        encumbered_risk=0.0,
    )


def _position(pos_id: str, expiry_iso: str) -> PositionModel:
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
        current_value_per_share=0.10,
        contracts=1,
        max_profit=1.20,
        max_loss=3.80,
        notes="",
        rolls=0,
        status="OPEN",
        book_id="B01",
    )


class TestRunRecoAnalysis:
    @pytest.mark.asyncio
    async def test_unknown_verdict_with_gap_is_restore_gap_held_not_terminalized(self, session_maker):
        # #542: an UNKNOWN verdict with a >1-trading-day gap since the last
        # reconciliation must be HELD, never silently cancelled/expired —
        # exactly the class of bug a stale sandboxed backup should surface.
        async with session_maker() as session:
            session.add(_order("o1", "basis:B01:o1:open", "STAGED"))
            session.add(
                ReconciliationRunModel(
                    run_at="2026-08-10T22:00:00+00:00",
                    broker_snapshot={},
                    books_expected={},
                    result="CLEAN",
                )
            )
            await session.commit()

        inner = FakeInnerBroker()
        inner._report = ReconcileReport(states={"basis:B01:o1:open": RefState.UNKNOWN})
        broker = rd.ReadOnlyBroker(inner)

        report = await rd.run_recon_analysis(
            session_maker,
            broker,
            today=__import__("datetime").date(2026, 8, 22),
            mode="sandbox",
            source_db="x",
            sandbox_db="y",
        )
        assert report.gap_trading_days > 1
        assert "basis:B01:o1:open" in report.restore_gap_held
        assert any(v.verdict == "RESTORE_GAP_UNKNOWN_HELD" for v in report.order_verdicts)
        assert not report.clean
        assert report.mutation_attempts == []

    @pytest.mark.asyncio
    async def test_no_reconciliation_baseline_holds_unknowns_not_zero_gap(self, session_maker):
        # #650: an empty reconciliation_runs table (no prior run at all —
        # a brand-new database, or a restore of a pre-reconciliation
        # backup) used to compute gap 0, "no gap" — the most-dangerous-
        # possible default. It must read as maximal, holding UNKNOWN
        # verdicts exactly like a genuine multi-day gap does.
        async with session_maker() as session:
            session.add(_order("o1", "basis:B01:o1:open", "STAGED"))
            await session.commit()

        inner = FakeInnerBroker()
        inner._report = ReconcileReport(states={"basis:B01:o1:open": RefState.UNKNOWN})
        broker = rd.ReadOnlyBroker(inner)

        report = await rd.run_recon_analysis(
            session_maker,
            broker,
            today=__import__("datetime").date(2026, 8, 22),
            mode="sandbox",
            source_db="x",
            sandbox_db="y",
        )
        assert report.gap_trading_days is None
        assert "basis:B01:o1:open" in report.restore_gap_held
        assert any(v.verdict == "RESTORE_GAP_UNKNOWN_HELD" for v in report.order_verdicts)
        assert not report.clean

    @pytest.mark.asyncio
    async def test_ghost_order_at_the_broker_is_a_drift_finding(self, session_maker):
        inner = FakeInnerBroker()
        inner._open_orders = [
            OpenOrderInfo(order_ref="basis:B01:ghost:open", order_id=5, perm_id=None, status="Submitted")
        ]
        broker = rd.ReadOnlyBroker(inner)

        report = await rd.run_recon_analysis(
            session_maker,
            broker,
            today=__import__("datetime").date(2026, 8, 22),
            mode="sandbox",
            source_db="x",
            sandbox_db="y",
        )
        assert any(d.kind == "GHOST_ORDER" and d.key == "basis:B01:ghost:open" for d in report.drifts)
        assert not report.clean

    @pytest.mark.asyncio
    async def test_rejected_and_filled_verdicts_are_classified(self, session_maker):
        async with session_maker() as session:
            session.add(_order("o1", "basis:B01:o1:open", "STAGED"))
            session.add(_order("o2", "basis:B01:o2:open", "SUBMITTED"))
            await session.commit()

        inner = FakeInnerBroker()
        inner._report = ReconcileReport(
            states={"basis:B01:o1:open": RefState.FILLED, "basis:B01:o2:open": RefState.CANCELLED},
            rejections={"basis:B01:o2:open": "Rejected by System: reason"},
        )
        broker = rd.ReadOnlyBroker(inner)

        report = await rd.run_recon_analysis(
            session_maker,
            broker,
            today=__import__("datetime").date(2026, 8, 22),
            mode="sandbox",
            source_db="x",
            sandbox_db="y",
        )
        verdicts = {v.order_ref: v.verdict for v in report.order_verdicts}
        assert verdicts["basis:B01:o1:open"] == "FILLED"
        assert verdicts["basis:B01:o2:open"] == "ORDER_REJECTED"

    @pytest.mark.asyncio
    async def test_clean_run_reports_clean_and_never_attempts_a_mutation(self, session_maker):
        inner = FakeInnerBroker()
        broker = rd.ReadOnlyBroker(inner)
        report = await rd.run_recon_analysis(
            session_maker,
            broker,
            today=__import__("datetime").date(2026, 8, 22),
            mode="sandbox",
            source_db="x",
            sandbox_db="y",
        )
        assert report.clean
        assert report.mutation_attempts == []
        assert "place_spread" not in inner.calls
        assert "cancel" not in inner.calls

    @pytest.mark.asyncio
    async def test_analysis_never_writes_to_the_database(self, session_maker):
        async with session_maker() as session:
            session.add(_order("o1", "basis:B01:o1:open", "STAGED"))
            await session.commit()
            before = len((await session.execute(select(OrderModel))).scalars().all())
            before_recon = len((await session.execute(select(ReconciliationRunModel))).scalars().all())

        inner = FakeInnerBroker()
        inner._report = ReconcileReport(states={"basis:B01:o1:open": RefState.UNKNOWN})
        broker = rd.ReadOnlyBroker(inner)
        await rd.run_recon_analysis(
            session_maker,
            broker,
            today=__import__("datetime").date(2026, 8, 22),
            mode="sandbox",
            source_db="x",
            sandbox_db="y",
        )

        async with session_maker() as session:
            after = len((await session.execute(select(OrderModel))).scalars().all())
            after_recon = len((await session.execute(select(ReconciliationRunModel))).scalars().all())
        assert after == before
        assert after_recon == before_recon

    @pytest.mark.asyncio
    async def test_mid_session_absence_reads_lost_not_day_expired(self, session_maker):
        # #965 (F1): the drill's own documented "safe to run any time"
        # use case. A DAY close submitted 09:50 ET THIS SAME morning is
        # absent at 11:00 ET — the session obviously hasn't closed yet, so
        # a genuinely-vanished order must read ORDER_LOST_AT_BROKER, not be
        # quieted to ORDER_DAY_EXPIRED just because it shares today's
        # calendar date with the (not-yet-happened) session close.
        today = datetime.date(2026, 8, 24)  # a Monday, a trading day
        ref = "basis:B01:o_midsession:close"
        async with session_maker() as session:
            session.add(
                ReconciliationRunModel(
                    run_at=f"{today.isoformat()}T13:00:00+00:00",
                    broker_snapshot={},
                    books_expected={},
                    result="CLEAN",
                )
            )
            order = _order("o_midsession", ref, "SUBMITTED", action="CLOSE")
            order.submitted_at = (
                datetime.datetime.combine(today, datetime.time(9, 50), tzinfo=MARKET_TZ)
                .astimezone(datetime.UTC)
                .isoformat()
            )
            session.add(order)
            await session.commit()

        inner = FakeInnerBroker()
        inner._report = ReconcileReport(states={ref: RefState.UNKNOWN})
        broker = rd.ReadOnlyBroker(inner)

        now_11am_et = datetime.datetime.combine(today, datetime.time(11, 0), tzinfo=MARKET_TZ)
        report = await rd.run_recon_analysis(
            session_maker, broker, today=today, mode="sandbox", source_db="x", sandbox_db="y", now=now_11am_et
        )
        verdicts = {v.order_ref: v.verdict for v in report.order_verdicts}
        assert verdicts[ref] == "ORDER_LOST_AT_BROKER"

    @pytest.mark.asyncio
    async def test_post_close_absence_reads_day_expired(self, session_maker):
        # #965 (F1): the same order as above, same calendar day — but the
        # drill runs at 18:45 ET, after the session's own close has
        # genuinely passed. This is the routine, expected case.
        today = datetime.date(2026, 8, 24)  # a Monday, a trading day
        ref = "basis:B01:o_postclose:close"
        async with session_maker() as session:
            session.add(
                ReconciliationRunModel(
                    run_at=f"{today.isoformat()}T13:00:00+00:00",
                    broker_snapshot={},
                    books_expected={},
                    result="CLEAN",
                )
            )
            order = _order("o_postclose", ref, "SUBMITTED", action="CLOSE")
            order.submitted_at = (
                datetime.datetime.combine(today, datetime.time(9, 50), tzinfo=MARKET_TZ)
                .astimezone(datetime.UTC)
                .isoformat()
            )
            session.add(order)
            await session.commit()

        inner = FakeInnerBroker()
        inner._report = ReconcileReport(states={ref: RefState.UNKNOWN})
        broker = rd.ReadOnlyBroker(inner)

        now_evening_et = datetime.datetime.combine(today, datetime.time(18, 45), tzinfo=MARKET_TZ)
        report = await rd.run_recon_analysis(
            session_maker, broker, today=today, mode="sandbox", source_db="x", sandbox_db="y", now=now_evening_et
        )
        verdicts = {v.order_ref: v.verdict for v in report.order_verdicts}
        assert verdicts[ref] == "ORDER_DAY_EXPIRED"

    @pytest.mark.asyncio
    async def test_position_expired_reads_expired_at_broker(self, session_maker):
        # #965 (F4): the drill's three-way split had zero dedicated
        # coverage; this pins the remaining arm — a resting order vanished
        # WITH its position (IB purges both together) reads
        # ORDER_EXPIRED_AT_BROKER, taking priority even though the order is
        # also past its own DAY session.
        today = datetime.date(2026, 8, 24)
        ref = "basis:B01:o_pos_exp:close"
        async with session_maker() as session:
            session.add(
                ReconciliationRunModel(
                    run_at=f"{today.isoformat()}T13:00:00+00:00",
                    broker_snapshot={},
                    books_expected={},
                    result="CLEAN",
                )
            )
            session.add(_position("pos1", today.isoformat()))
            order = _order("o_pos_exp", ref, "SUBMITTED", action="CLOSE")
            order.position_id = "pos1"
            order.submitted_at = (
                datetime.datetime.combine(today - datetime.timedelta(days=1), datetime.time(18, 45), tzinfo=MARKET_TZ)
                .astimezone(datetime.UTC)
                .isoformat()
            )
            session.add(order)
            await session.commit()

        inner = FakeInnerBroker()
        inner._report = ReconcileReport(states={ref: RefState.UNKNOWN})
        broker = rd.ReadOnlyBroker(inner)

        now_evening_et = datetime.datetime.combine(today, datetime.time(18, 45), tzinfo=MARKET_TZ)
        report = await rd.run_recon_analysis(
            session_maker, broker, today=today, mode="sandbox", source_db="x", sandbox_db="y", now=now_evening_et
        )
        verdicts = {v.order_ref: v.verdict for v in report.order_verdicts}
        assert verdicts[ref] == "ORDER_EXPIRED_AT_BROKER"


def test_format_report_flags_mutation_attempts_prominently():
    report = rd.DrillReport(
        mode="sandbox",
        source_db="a",
        sandbox_db="b",
        run_at="t0",
        gap_trading_days=0,
        mutation_attempts=["cancel"],
    )
    text = rd.format_report(report)
    assert "mutating broker calls attempted: 1" in text
    assert "cancel" in text


def test_format_report_clean_run():
    report = rd.DrillReport(mode="production", source_db="a", sandbox_db=None, run_at="t0", gap_trading_days=0)
    text = rd.format_report(report)
    assert "CLEAN — nothing to report" in text
    assert "sandbox db:" not in text


def test_format_report_surfaces_a_run_error_without_crashing():
    report = rd.DrillReport(
        mode="sandbox",
        source_db="a",
        sandbox_db="b",
        run_at="t0",
        gap_trading_days=0,
        error="Gateway port never opened",
    )
    text = rd.format_report(report)
    assert "RUN ERROR: Gateway port never opened" in text


class TestCliDispatch:
    def test_main_dispatches_to_sandbox_by_default(self, monkeypatch):
        called = {}
        monkeypatch.setattr(
            rd,
            "run_sandbox_drill",
            lambda backup=None, backup_dir=None: called.update(backup=backup, backup_dir=backup_dir) or 0,
        )
        assert rd.main([]) == 0
        assert called == {"backup": None, "backup_dir": None}

    def test_main_dispatches_to_production_recon(self, monkeypatch, tmp_path):
        db = tmp_path / "basis.db"
        db.write_text("x")
        called = {}
        monkeypatch.setattr(rd, "run_production_recon", lambda database_path: called.update(path=database_path) or 0)
        assert rd.main(["--against-production", "--database", str(db)]) == 0
        assert called["path"] == db

    def test_production_recon_reports_missing_database(self, tmp_path):
        missing = tmp_path / "nope.db"
        assert rd.run_production_recon(missing) == 2

    def test_default_production_db_path_rejects_non_sqlite_urls(self, monkeypatch):
        monkeypatch.setattr(rd, "_default_production_db_path", rd._default_production_db_path)
        import backend.database as db_module

        monkeypatch.setattr(db_module, "DATABASE_URL", "postgresql://x")
        with pytest.raises(SystemExit):
            rd._default_production_db_path()


def _make_full_schema_db(path: Path) -> None:
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()


class TestSandboxMigration:
    """#646: the first live drill crashed on 'no such table: reconciliation_runs'
    — the sandbox copy was never migrated before the read-only analysis
    phase, unlike a real restore (init_db runs on next startup, THEN the
    pipeline reconciles)."""

    def test_migrate_sandbox_copy_creates_a_table_missing_from_an_old_schema(self, tmp_path):
        db_path = tmp_path / "old_schema.db"
        _make_full_schema_db(db_path)
        # Simulate a genuinely old backup: drop a table create_all already made.
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE reconciliation_runs")
        conn.commit()
        conn.close()

        outcome = rd.migrate_sandbox_copy(db_path)

        assert outcome.ok, outcome.error
        assert "reconciliation_runs" in outcome.tables_added

    def test_migrate_sandbox_copy_on_an_already_current_schema_adds_nothing(self, tmp_path):
        db_path = tmp_path / "current.db"
        _make_full_schema_db(db_path)

        outcome = rd.migrate_sandbox_copy(db_path)

        assert outcome.ok, outcome.error
        assert outcome.tables_added == []
        assert outcome.columns_added == {}

    def test_migrate_sandbox_copy_reports_a_failed_init_db_as_an_error(self, tmp_path, monkeypatch):
        db_path = tmp_path / "any.db"
        _make_full_schema_db(db_path)

        class _FailedProc:
            returncode = 1
            stdout = ""
            stderr = "RuntimeError: Database schema is stale"

        monkeypatch.setattr(rd.subprocess, "run", lambda *a, **k: _FailedProc())

        outcome = rd.migrate_sandbox_copy(db_path)
        assert not outcome.ok
        assert "stale" in outcome.error

    def test_run_sandbox_drill_with_an_old_schema_backup_migrates_then_analyzes(self, tmp_path, monkeypatch):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        backup = backup_dir / "basis.2026-08-01.db"
        _make_full_schema_db(backup)
        conn = sqlite3.connect(str(backup))
        conn.execute("DROP TABLE reconciliation_runs")
        conn.commit()
        conn.close()

        captured: dict = {}

        def _fake_run_with_gateway(work):
            fake_broker = rd.ReadOnlyBroker(FakeInnerBroker())
            report = work(fake_broker)
            captured["report"] = report
            return 0, report

        monkeypatch.setattr(rd, "_run_with_gateway", _fake_run_with_gateway)

        code = rd.run_sandbox_drill(backup_dir=backup_dir)

        assert code == 0
        report = captured["report"]
        assert report.migration is not None
        assert report.migration.ok
        assert "reconciliation_runs" in report.migration.tables_added
        # And the analysis phase ran successfully against the now-migrated
        # copy — the table that used to crash it is queried without error.
        # #650: freshly re-created via migration, this table has no rows —
        # a genuinely missing baseline, correctly reported as None/maximal,
        # not 0/"no gap".
        assert report.gap_trading_days is None

    def test_a_failed_migration_bails_before_ever_launching_gateway(self, tmp_path, monkeypatch):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        backup = backup_dir / "basis.2026-08-01.db"
        _make_full_schema_db(backup)

        monkeypatch.setattr(
            rd, "migrate_sandbox_copy", lambda sandbox_db, repo_root=None: rd.MigrationOutcome(ok=False, error="boom")
        )

        def _must_not_be_called(work):
            raise AssertionError("Gateway must never be launched after a failed migration")

        monkeypatch.setattr(rd, "_run_with_gateway", _must_not_be_called)

        code = rd.run_sandbox_drill(backup_dir=backup_dir)
        assert code == 4

    def test_against_production_never_migrates(self, tmp_path, monkeypatch):
        db_path = tmp_path / "basis.db"
        _make_full_schema_db(db_path)

        def _explode(*a, **k):
            raise AssertionError("production mode must never migrate — read-only guarantees stay absolute")

        monkeypatch.setattr(rd, "migrate_sandbox_copy", _explode)

        def _fake_run_with_gateway(work):
            fake_broker = rd.ReadOnlyBroker(FakeInnerBroker())
            return 0, work(fake_broker)

        monkeypatch.setattr(rd, "_run_with_gateway", _fake_run_with_gateway)

        code = rd.run_production_recon(db_path)
        assert code == 0

    def test_against_production_still_never_writes_even_after_646(self, tmp_path, monkeypatch):
        # A pinned regression, not just a repeat of TestReadonlySessionMaker's
        # generic write-refusal test: this one goes through the real
        # run_production_recon entry point end to end.
        db_path = tmp_path / "basis.db"
        _make_full_schema_db(db_path)

        def _fake_run_with_gateway(work):
            fake_broker = rd.ReadOnlyBroker(FakeInnerBroker())
            return 0, work(fake_broker)

        monkeypatch.setattr(rd, "_run_with_gateway", _fake_run_with_gateway)

        conn = sqlite3.connect(str(db_path))
        before_count = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        conn.close()

        code = rd.run_production_recon(db_path)
        assert code == 0

        conn = sqlite3.connect(str(db_path))
        after_count = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        conn.close()
        assert after_count == before_count


class TestProductionSchemaPreflight:
    """#739: --against-production must pre-flight schema drift and refuse
    cleanly BEFORE the tenancy lock / Gateway launch, instead of crashing
    with an uncaught OperationalError deep inside the analysis after
    Gateway is already up — mirrors #646's sandbox pre-Gateway migration
    bail, applied to the production path (which must never migrate)."""

    def test_current_schema_reports_no_drift(self, tmp_path):
        db_path = tmp_path / "current.db"
        _make_full_schema_db(db_path)

        drift = rd.check_production_schema_drift(db_path)

        assert drift.ok
        assert drift.missing_tables == []
        assert drift.missing_columns == {}
        assert drift.gap_count == 0

    def test_missing_table_is_detected_read_only(self, tmp_path):
        db_path = tmp_path / "old.db"
        _make_full_schema_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE reconciliation_runs")
        conn.commit()
        conn.close()

        drift = rd.check_production_schema_drift(db_path)

        assert not drift.ok
        assert "reconciliation_runs" in drift.missing_tables
        assert drift.gap_count >= 1

    def test_missing_column_is_detected(self, tmp_path):
        db_path = tmp_path / "old.db"
        _make_full_schema_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("ALTER TABLE orders DROP COLUMN quote_snapshot")
        conn.commit()
        conn.close()

        drift = rd.check_production_schema_drift(db_path)

        assert not drift.ok
        assert drift.missing_columns.get("orders") == ["quote_snapshot"]
        assert drift.gap_count == 1

    def test_the_preflight_check_itself_never_writes(self, tmp_path):
        # readonly=True opens the same mode=ro URI style
        # readonly_session_maker uses — a write attempt must raise at the
        # driver, not merely be avoided by convention.
        db_path = tmp_path / "current.db"
        _make_full_schema_db(db_path)
        conn = sqlite3.connect(str(db_path))
        before = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        conn.close()

        rd.check_production_schema_drift(db_path)

        conn = sqlite3.connect(str(db_path))
        after = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        conn.close()
        assert after == before

    def test_run_production_recon_refuses_cleanly_on_drift_before_gateway(self, tmp_path, monkeypatch):
        db_path = tmp_path / "old.db"
        _make_full_schema_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("ALTER TABLE orders DROP COLUMN quote_snapshot")
        conn.commit()
        conn.close()

        def _must_not_be_called(work):
            raise AssertionError("Gateway must never be launched when the schema is behind")

        monkeypatch.setattr(rd, "_run_with_gateway", _must_not_be_called)

        code = rd.run_production_recon(db_path)

        assert code == 6

    def test_run_production_recon_proceeds_normally_when_schema_is_current(self, tmp_path, monkeypatch):
        db_path = tmp_path / "current.db"
        _make_full_schema_db(db_path)

        def _fake_run_with_gateway(work):
            fake_broker = rd.ReadOnlyBroker(FakeInnerBroker())
            return 0, work(fake_broker)

        monkeypatch.setattr(rd, "_run_with_gateway", _fake_run_with_gateway)

        code = rd.run_production_recon(db_path)

        assert code == 0

    def test_message_names_the_normal_entry_points_and_the_sandbox_fallback(self, tmp_path, capsys):
        db_path = tmp_path / "old.db"
        _make_full_schema_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("ALTER TABLE orders DROP COLUMN quote_snapshot")
        conn.commit()
        conn.close()

        rd.run_production_recon(db_path)

        err = capsys.readouterr().err
        assert "migration(s) behind" in err
        assert "init_db" in err
        assert "sandbox mode" in err
        assert "quote_snapshot" in err
