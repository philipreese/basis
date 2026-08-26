"""Tests for the afternoon preflight rehearsal (backend/preflight.py, #827).

All broker/gateway I/O is faked — no network, no processes. The charter
assertions matter most here: preflight must never place an order, never
write a reconciliation_runs row, never mutate control state, and never
touch the executor heartbeat, while still reporting every failure class.
"""

import datetime
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import preflight
from backend.broker import ConnectionFailedError, MarginPreview, PreviewRejectedError
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    PositionModel,
    ReconciliationRunModel,
    TradingControlModel,
)
from backend.preflight import PreflightReport, compose_preflight_push, probe_leg_symbols, run_preflight
from backend.run_lock import acquire_run_lock

TODAY = datetime.date(2026, 8, 24)  # a Monday, ordinary trading day
DISCLAIMER_10141 = (10141, "Paper trading disclaimer must be accepted")


class FakeBroker:
    """Implements exactly the BrokerSession surface preflight consumes.
    Any order-path method is recorded as a forbidden call."""

    def __init__(self, open_exc: BaseException | None = None, preview_exc: BaseException | None = None) -> None:
        self.open_exc = open_exc
        self.preview_exc = preview_exc
        self.opened = False
        self.closed = False
        self.broker_positions: list = []
        self.previews: list = []
        self.forbidden_calls: list[str] = []

    def open(self) -> None:
        if self.open_exc is not None:
            raise self.open_exc
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def positions(self) -> list:
        return list(self.broker_positions)

    def executions(self, since: str | None = None) -> list:
        return []

    def open_orders(self) -> list:
        return []

    def preview_spread(self, spread) -> MarginPreview:
        self.previews.append(spread)
        if self.preview_exc is not None:
            raise self.preview_exc
        return MarginPreview(
            init_margin_change=500.0, maint_margin_change=450.0, commission_min=1.0, commission_max=2.0
        )

    # -- the order path: forbidden for preflight, recorded if ever touched --

    def place_spread(self, *args, **kwargs):
        self.forbidden_calls.append("place_spread")

    def close_spread(self, *args, **kwargs):
        self.forbidden_calls.append("close_spread")

    def cancel_by_ref(self, *args, **kwargs):
        self.forbidden_calls.append("cancel_by_ref")

    def cancel(self, *args, **kwargs):
        self.forbidden_calls.append("cancel")

    def reconcile(self, *args, **kwargs):
        self.forbidden_calls.append("reconcile")


def _open_position() -> PositionModel:
    return PositionModel(
        id="p1",
        underlying="XSP",
        strategy_type="BULL_PUT_SPREAD",
        execution_mode="PAPER",
        legs=[
            {"option_type": "PUT", "direction": "SHORT", "strike": 610.0, "expiration": "2026-12-18"},
            {"option_type": "PUT", "direction": "LONG", "strike": 605.0, "expiration": "2026-12-18"},
        ],
        entry_date="2026-08-10",
        expiration_date="2026-12-18",
        entry_premium=1.25,
        premium_direction="CREDIT",
        current_value_per_share=1.10,
        contracts=1,
        max_profit=1.25,
        max_loss=3.75,
        notes="",
        rolls=0,
        status="OPEN",
        journal={},
        book_id="B01",
    )


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
        await session.commit()
    yield maker
    await engine.dispose()


@pytest.fixture
def pushes(monkeypatch):
    sent: list[tuple[str, str, str]] = []

    def _record(title: str, body: str, priority: str = "default", **kwargs) -> bool:
        sent.append((title, body, priority))
        return True

    monkeypatch.setattr(preflight, "send_ntfy_with_retry", _record)
    return sent


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    """Fake Gateway plumbing: script exists, port opens, launch/stop recorded."""
    start_script = tmp_path / "StartGateway.bat"
    start_script.write_text("rem fake", encoding="utf-8")
    monkeypatch.setenv("IBC_START_SCRIPT", str(start_script))
    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path / "locks"))
    (tmp_path / "locks").mkdir()
    monkeypatch.setenv("HALT_FILE", str(tmp_path / "no-such-HALT"))

    heartbeat = tmp_path / "executor_heartbeat.json"
    heartbeat.write_text(
        json.dumps({"at": datetime.datetime.now(datetime.UTC).isoformat()}),
        encoding="utf-8",
    )
    monkeypatch.setenv("EXECUTOR_HEARTBEAT_FILE", str(heartbeat))

    proc = SimpleNamespace(pid=4242)
    stopped: list = []
    monkeypatch.setattr(preflight, "launch_gateway", lambda script: proc)
    monkeypatch.setattr(preflight, "wait_for_port", lambda host, port: True)
    monkeypatch.setattr(preflight, "stop_gateway_tree_only", lambda p: stopped.append(p))
    monkeypatch.setattr(preflight, "fetch_index_daily_closes", lambda symbol, days: [("2026-08-24", 645.0)])
    monkeypatch.setattr(preflight, "fetch_options_latest_quotes", lambda symbols: {symbols[0]: 1.0, symbols[1]: 0.4})
    return SimpleNamespace(proc=proc, stopped=stopped, heartbeat=heartbeat, tmp_path=tmp_path)


async def _run(session_maker, broker, gateway) -> int:
    return await run_preflight(
        today=TODAY, broker_factory=lambda: broker, session_maker=session_maker, sleep=lambda s: None
    )


async def _audit_payloads(session_maker) -> list[dict]:
    async with session_maker() as session:
        rows = (await session.execute(select(AuditEventModel).filter_by(event_type="PREFLIGHT_RUN"))).scalars().all()
        return [r.payload for r in rows]


class TestAllClear:
    @pytest.mark.asyncio
    async def test_composes_the_all_clear_push(self, session_maker, pushes, gateway):
        broker = FakeBroker()
        code = await _run(session_maker, broker, gateway)
        assert code == 0
        title, body, priority = pushes[-1]
        assert title == "basis preflight: all clear"
        assert priority == "default"
        assert "gateway+session" in body
        assert "reconciliation comparison" in body
        assert "preview probe" in body
        assert broker.previews  # the probe actually reached preview_spread

    @pytest.mark.asyncio
    async def test_never_places_orders_or_writes_forbidden_state(self, session_maker, pushes, gateway):
        broker = FakeBroker()
        heartbeat_before = gateway.heartbeat.read_text(encoding="utf-8")
        await _run(session_maker, broker, gateway)
        assert broker.forbidden_calls == []
        async with session_maker() as session:
            recon_rows = (await session.execute(select(ReconciliationRunModel))).scalars().all()
            control = await session.get(TradingControlModel, "GLOBAL")
        assert recon_rows == []
        assert control.state == "ACTIVE"
        # The watchdog must never be pacified by a rehearsal.
        assert gateway.heartbeat.read_text(encoding="utf-8") == heartbeat_before

    @pytest.mark.asyncio
    async def test_writes_the_preflight_run_audit_event(self, session_maker, pushes, gateway):
        await _run(session_maker, FakeBroker(), gateway)
        payloads = await _audit_payloads(session_maker)
        assert len(payloads) == 1
        assert payloads[0]["findings"] == []
        assert "gateway+session" in payloads[0]["rehearsed"]

    @pytest.mark.asyncio
    async def test_teardown_kills_the_gateway_tree(self, session_maker, pushes, gateway):
        await _run(session_maker, FakeBroker(), gateway)
        assert gateway.stopped == [gateway.proc]

    @pytest.mark.asyncio
    async def test_teardown_skipped_when_a_tenant_appears_in_the_recheck_window(
        self, session_maker, pushes, gateway, monkeypatch
    ):
        # #838: other_gateway_tenant_active is re-checked immediately before
        # the kill, not just at the top of the run — a tenant that shows up
        # mid-rehearsal (simulated here as appearing right after the port
        # opens) must still stop this teardown from killing it.
        import json

        def _port_opens_then_another_tenant_arrives(host, port):
            (gateway.tmp_path / "locks" / "executor.lock").write_text(
                json.dumps({"pid": 99, "token": "live"}), encoding="utf-8"
            )
            return True

        monkeypatch.setattr(preflight, "wait_for_port", _port_opens_then_another_tenant_arrives)
        code = await _run(session_maker, FakeBroker(), gateway)
        assert code == 0
        assert gateway.stopped == []  # never torn down — the late tenant owns it
        assert not (gateway.tmp_path / "locks" / "preflight.lock").exists()  # own lock still released


class TestFailureClasses:
    @pytest.mark.asyncio
    async def test_classified_connect_failure_reports_the_needs_human_instruction(self, session_maker, pushes, gateway):
        broker = FakeBroker(
            open_exc=ConnectionFailedError("Could not open IB Gateway session", api_errors=(DISCLAIMER_10141,))
        )
        await _run(session_maker, broker, gateway)
        title, body, priority = pushes[-1]
        assert title == "basis preflight: 1 problem(s)"
        assert priority == "high"
        assert "paper-trading disclaimer" in body  # the classified instruction (#823)
        assert "broker API error 10141" in body
        # Broker-dependent steps are named as skipped, never silently absent.
        assert "Not rehearsed: reconciliation comparison" in body
        assert "Not rehearsed: preview probe" in body
        # Broker-independent steps still ran.
        assert "control state" in body

    @pytest.mark.asyncio
    async def test_drift_is_listed_without_halting_or_persisting(self, session_maker, pushes, gateway):
        async with session_maker() as session:
            session.add(_open_position())
            await session.commit()
        broker = FakeBroker()  # broker reports NO positions -> EXTERNAL_CLOSE on both legs
        await _run(session_maker, broker, gateway)
        _, body, priority = pushes[-1]
        assert "EXTERNAL_CLOSE" in body
        assert "reconciliation panel" in body
        assert priority == "high"
        async with session_maker() as session:
            assert (await session.execute(select(ReconciliationRunModel))).scalars().all() == []
            control = await session.get(TradingControlModel, "GLOBAL")
        assert control.state == "ACTIVE"  # report-only: no halt latched

    @pytest.mark.asyncio
    async def test_preview_refusal_is_a_distinct_finding(self, session_maker, pushes, gateway):
        broker = FakeBroker(preview_exc=PreviewRejectedError("whatIfOrder warning: margin check failed"))
        await _run(session_maker, broker, gateway)
        _, body, _ = pushes[-1]
        assert "preview gate refused the probe: whatIfOrder warning: margin check failed" in body
        assert "unexpected" not in body

    @pytest.mark.asyncio
    async def test_non_active_control_scope_is_listed(self, session_maker, pushes, gateway):
        async with session_maker() as session:
            control = await session.get(TradingControlModel, "GLOBAL")
            control.state = "HALT_ENTRIES"
            control.reason = "drift last night"
            await session.commit()
        await _run(session_maker, FakeBroker(), gateway)
        _, body, _ = pushes[-1]
        assert "GLOBAL is HALT_ENTRIES (drift last night)" in body
        assert "resume from the console" in body

    @pytest.mark.asyncio
    async def test_stale_heartbeat_is_listed(self, session_maker, pushes, gateway):
        gateway.heartbeat.write_text(json.dumps({"at": "2026-01-05T23:30:00+00:00"}), encoding="utf-8")
        await _run(session_maker, FakeBroker(), gateway)
        _, body, _ = pushes[-1]
        assert "executor heartbeat stale or missing" in body


class TestFindingsIsolation:
    @pytest.mark.asyncio
    async def test_a_crash_inside_one_step_still_reports_the_others(self, session_maker, pushes, gateway, monkeypatch):
        def _boom(symbols):
            raise RuntimeError("quote fetch exploded")

        monkeypatch.setattr(preflight, "fetch_options_latest_quotes", _boom)
        broker = FakeBroker()
        code = await _run(session_maker, broker, gateway)
        assert code == 0  # never a crash
        _, body, _ = pushes[-1]
        assert "[preview] unexpected RuntimeError: quote fetch exploded" in body
        assert "reconciliation comparison" in body  # the other steps still ran
        assert "control state" in body
        assert gateway.stopped == [gateway.proc]  # teardown still happened


class TestLocking:
    @pytest.mark.asyncio
    async def test_executor_lock_held_skips_cleanly(self, session_maker, pushes, gateway, monkeypatch):
        launched: list = []
        monkeypatch.setattr(preflight, "launch_gateway", lambda script: launched.append(script))
        assert acquire_run_lock("executor") is not None  # the 18:45 run is live
        code = await _run(session_maker, FakeBroker(), gateway)
        assert code == 0
        assert pushes == [("basis preflight: skipped", "executor running, preflight skipped", "default")]
        assert launched == []  # never launched a second Gateway

    @pytest.mark.asyncio
    async def test_own_lock_held_aborts_without_pushing(self, session_maker, pushes, gateway):
        assert acquire_run_lock("preflight") is not None
        code = await _run(session_maker, FakeBroker(), gateway)
        assert code == 4
        assert pushes == []

    @pytest.mark.asyncio
    async def test_lock_released_and_gateway_killed_even_when_the_push_itself_crashes(
        self, session_maker, gateway, monkeypatch
    ):
        def _push_boom(*args, **kwargs):
            raise RuntimeError("ntfy machinery exploded")

        monkeypatch.setattr(preflight, "send_ntfy_with_retry", _push_boom)
        broker = FakeBroker()
        with pytest.raises(RuntimeError):
            await _run(session_maker, broker, gateway)
        assert gateway.stopped == [gateway.proc]  # teardown on failure too
        assert broker.closed
        assert acquire_run_lock("preflight") is not None  # lock was released

    @pytest.mark.asyncio
    async def test_holiday_skips_without_locking_or_launching(self, session_maker, pushes, gateway):
        code = await run_preflight(
            today=datetime.date(2026, 12, 25),
            broker_factory=FakeBroker,
            session_maker=session_maker,
            sleep=lambda s: None,
        )
        assert code == 0
        assert pushes == []
        assert gateway.stopped == []


class TestProbeGeometry:
    def test_probe_is_a_next_day_otm_xsp_put_vertical(self):
        short_occ, long_occ = probe_leg_symbols(TODAY, spot=645.7)
        assert short_occ == "XSP260825P00640000"  # int(645.7) - 5, next trading day
        assert long_occ == "XSP260825P00635000"

    def test_probe_expiry_skips_the_weekend(self):
        friday = datetime.date(2026, 8, 21)
        short_occ, _ = probe_leg_symbols(friday, spot=645.0)
        assert short_occ.startswith("XSP260824")  # Monday, not Saturday

    def test_missing_start_script_is_a_finding_with_the_setup_action(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IBC_START_SCRIPT", str(tmp_path / "missing.bat"))
        report = PreflightReport()
        assert preflight._launch(report, sleep=lambda s: None) is None
        assert report.findings[0].step == "gateway"
        assert "setup-ibc.ps1" in (report.findings[0].action or "")


class TestCompose:
    def test_all_clear_title_and_priority(self):
        report = PreflightReport(rehearsed=["gateway+session"])
        title, body, priority = compose_preflight_push(report)
        assert title == "basis preflight: all clear"
        assert priority == "default"
        assert "clear runway" in body

    def test_titles_are_ascii_only(self):
        report = PreflightReport()
        report.unexpected("gateway", RuntimeError("boom"))
        for r in (report, PreflightReport()):
            title, _, _ = compose_preflight_push(r)
            title.encode("ascii")  # raises if #598 regresses
