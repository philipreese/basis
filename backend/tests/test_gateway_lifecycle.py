"""Tests for the Gateway start-on-demand lifecycle and holiday guard (#68).

The lifecycle wrapper must never run the executor half-connected, never
launch Gateway on a holiday, and always tear the Gateway process tree
down. The executor's own holiday guard writes the heartbeat so silent
non-operation is announced.
"""

import datetime
import json
from unittest.mock import MagicMock, patch

import pytest

from backend import gateway_lifecycle as gl
from backend.calendars import MARKET_HOLIDAYS, is_trading_day, stale_calendars

MONDAY = datetime.date(2026, 8, 17)
SATURDAY = datetime.date(2026, 8, 15)
LABOR_DAY = datetime.date(2026, 9, 7)


class TestMarketCalendar:
    def test_weekdays_trade_weekends_and_holidays_do_not(self):
        assert is_trading_day(MONDAY)
        assert not is_trading_day(SATURDAY)
        assert not is_trading_day(LABOR_DAY)

    def test_staleness_guard(self):
        assert "market holidays" not in stale_calendars(MONDAY)
        last = max(datetime.date.fromisoformat(d) for d in MARKET_HOLIDAYS)
        assert "market holidays" in stale_calendars(last - datetime.timedelta(days=30))


class TestWaitForPort:
    def test_returns_true_when_connect_succeeds(self):
        with patch.object(gl.socket, "create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = MagicMock()
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            assert gl.wait_for_port("127.0.0.1", 4002, timeout_seconds=1)

    def test_returns_false_after_deadline(self):
        clock = iter(range(100))
        with patch.object(gl.socket, "create_connection", side_effect=OSError):
            assert not gl.wait_for_port(
                "127.0.0.1", 4002, timeout_seconds=3, sleep=lambda _s: None, monotonic=lambda: next(clock)
            )


class TestRunNightly:
    def test_holiday_runs_executor_without_gateway(self):
        with (
            patch.object(gl, "launch_gateway") as mock_launch,
            patch("backend.executor.main") as mock_exec,
        ):
            code = gl.run_nightly(today=LABOR_DAY)
        assert code == 0
        mock_launch.assert_not_called()
        mock_exec.assert_called_once()

    def test_missing_start_script_alerts_and_aborts(self, monkeypatch):
        monkeypatch.delenv("IBC_START_SCRIPT", raising=False)
        with patch.object(gl, "_urgent") as mock_urgent, patch.object(gl, "launch_gateway") as mock_launch:
            code = gl.run_nightly(today=MONDAY)
        assert code == 2
        mock_urgent.assert_called_once()
        mock_launch.assert_not_called()

    def test_port_timeout_alerts_kills_gateway_and_never_runs_executor(self, monkeypatch, tmp_path):
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        proc = MagicMock()
        with (
            patch.object(gl, "launch_gateway", return_value=proc) as _,
            patch.object(gl, "wait_for_port", return_value=False),
            patch.object(gl, "stop_gateway") as mock_stop,
            patch.object(gl, "_urgent") as mock_urgent,
            patch.object(gl.time, "sleep"),
            patch("backend.executor.main") as mock_exec,
        ):
            code = gl.run_nightly(today=MONDAY)
        assert code == 3
        mock_urgent.assert_called_once()
        mock_stop.assert_called_once_with(proc)
        mock_exec.assert_not_called()

    def test_happy_path_runs_executor_then_stops_gateway(self, monkeypatch, tmp_path):
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        proc = MagicMock()
        with (
            patch.object(gl, "launch_gateway", return_value=proc),
            patch.object(gl, "wait_for_port", return_value=True),
            patch.object(gl, "stop_gateway") as mock_stop,
            patch.object(gl.time, "sleep"),
            patch("backend.executor.main") as mock_exec,
            patch.object(gl, "_backup_after_run") as mock_backup,
        ):
            code = gl.run_nightly(today=MONDAY)
        assert code == 0
        mock_exec.assert_called_once()
        mock_backup.assert_called_once()
        mock_stop.assert_called_once_with(proc)


class TestStopGateway:
    def test_kills_the_process_tree_and_sweeps_orphans(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 4242
        run = MagicMock()
        gl.stop_gateway(proc, run=run)
        assert run.call_args_list[0][0][0] == ["taskkill", "/PID", "4242", "/T", "/F"]
        sweep_cmd = run.call_args_list[1][0][0]
        assert sweep_cmd[0] == "powershell"
        assert "ibgateway" in sweep_cmd[-1]
        assert "java.exe" in sweep_cmd[-1]

    def test_kills_even_when_launcher_already_exited(self):
        # IBC's bat spawns java and exits, so the launcher is usually dead by
        # teardown — an early return here leaked the Gateway nightly (#224).
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.pid = 4242
        run = MagicMock()
        gl.stop_gateway(proc, run=run)
        assert run.call_count == 2
        assert run.call_args_list[0][0][0] == ["taskkill", "/PID", "4242", "/T", "/F"]


class TestExecutorHolidayGuard:
    @pytest.mark.asyncio
    async def test_holiday_writes_heartbeat_and_places_nothing(self, tmp_path, monkeypatch):
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from backend.executor import run_executor_evening
        from backend.models import Base

        monkeypatch.setenv("EXECUTOR_HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
        engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'hol.db').as_posix()}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        broker = MagicMock()
        try:
            summary = await run_executor_evening(session_maker=maker, broker_factory=lambda: broker, today=LABOR_DAY)
        finally:
            await engine.dispose()
        broker.open.assert_not_called()  # Gateway is never even contacted
        assert any("MARKET HOLIDAY" in n for n in summary.notes)
        beat = json.loads((tmp_path / "heartbeat.json").read_text())
        assert beat["entries_placed"] == 0
