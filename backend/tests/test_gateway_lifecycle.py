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
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))  # gateway tenancy lock (#471)
        proc = MagicMock()
        with (
            patch.object(gl, "launch_gateway", return_value=proc) as _,
            patch.object(gl, "wait_for_port", return_value=False),
            patch.object(gl, "stop_gateway") as mock_stop,
            patch.object(gl, "_urgent") as mock_urgent,
            patch.object(gl.time, "sleep"),
            patch("backend.executor.main") as mock_exec,
            patch.object(gl, "_backup_after_run") as mock_backup,
        ):
            code = gl.run_nightly(today=MONDAY)
        assert code == 3
        mock_urgent.assert_called_once()
        mock_stop.assert_called_once_with(proc)
        mock_exec.assert_not_called()
        # #548 LOW-2: the backup runs in finally regardless of exit reason.
        mock_backup.assert_called_once()

    def test_happy_path_runs_executor_then_stops_gateway(self, monkeypatch, tmp_path):
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))  # gateway tenancy lock (#471)
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

    def test_executor_crash_alerts_urgently_and_returns_nonzero(self, monkeypatch, tmp_path):
        # Audit II (#341): a crash used to exit silently with a healthy
        # heartbeat — the one night the watchdog exists for.
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))  # gateway tenancy lock (#471)
        proc = MagicMock()
        with (
            patch.object(gl, "launch_gateway", return_value=proc),
            patch.object(gl, "wait_for_port", return_value=True),
            patch.object(gl, "stop_gateway") as mock_stop,
            patch.object(gl, "_urgent") as mock_urgent,
            patch.object(gl.time, "sleep"),
            patch("backend.executor.main", side_effect=RuntimeError("boom")),
            patch.object(gl, "_backup_after_run") as mock_backup,
        ):
            code = gl.run_nightly(today=MONDAY)
        assert code == 4
        title, body = mock_urgent.call_args[0]
        assert "CRASHED" in title and "boom" in body
        # #548 LOW-2: backup moved into finally — a crash night must not be
        # the one night that also takes no snapshot; repeated crash nights
        # left the newest restore point arbitrarily old otherwise.
        mock_backup.assert_called_once()
        mock_stop.assert_called_once_with(proc)  # Gateway still torn down

    def test_gateway_lock_held_aborts_before_launching_a_second_gateway(self, monkeypatch, tmp_path):
        # Audit II R3 (#471): the tenancy lock is taken BEFORE launch — a
        # second nightly run mid-window must not start a second IBC Gateway
        # (they fight over the same login) or share the first one's teardown.
        import json

        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
        (tmp_path / "gateway.lock").write_text(json.dumps({"pid": 1, "token": "live"}), encoding="utf-8")
        with (
            patch.object(gl, "launch_gateway") as mock_launch,
            patch.object(gl, "_urgent") as mock_urgent,
            patch("backend.executor.main") as mock_exec,
        ):
            code = gl.run_nightly(today=MONDAY)
        assert code == 5
        mock_launch.assert_not_called()
        mock_exec.assert_not_called()
        mock_urgent.assert_called_once()
        # #546 F9: a scheduler/tenancy condition, not a code crash — every
        # other "NOT RUN" alert in this function passes SCHEDULER_ALERT;
        # this one used the CRASH_ALERT default.
        assert mock_urgent.call_args.kwargs.get("event_type") == "SCHEDULER_ALERT"

    def test_gateway_lock_brackets_the_run_and_teardown_defers_to_fill_check(self, monkeypatch, tmp_path):
        # Audit II R3 (#471): symmetric guard — a fill check mid-fetch on the
        # shared Gateway must not be killed by this run's teardown sweep.
        # The tenancy lock itself is released either way.
        import json

        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
        (tmp_path / "fill_check.lock").write_text(json.dumps({"pid": 2, "token": "live"}), encoding="utf-8")
        proc = MagicMock()
        with (
            patch.object(gl, "launch_gateway", return_value=proc),
            patch.object(gl, "wait_for_port", return_value=True),
            patch.object(gl, "stop_gateway") as mock_stop,
            patch.object(gl.time, "sleep"),
            patch("backend.executor.main"),
            patch.object(gl, "_backup_after_run"),
        ):
            code = gl.run_nightly(today=MONDAY)
        assert code == 0
        mock_stop.assert_not_called()  # the live fill check owns the Gateway
        assert not (tmp_path / "gateway.lock").exists()  # tenancy released

    def test_gateway_teardown_also_defers_to_a_restore_drill(self, monkeypatch, tmp_path):
        # #681: this teardown only ever knew about "fill_check" — restore_drill
        # (#641) is a fourth Gateway tenant, and a drill mid-query on the
        # shared Gateway is exactly as live as a fill check. Checked via
        # run_lock.GATEWAY_TENANT_LOCKS now, not a hand-spelled single name.
        import json

        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
        (tmp_path / "restore_drill.lock").write_text(json.dumps({"pid": 3, "token": "live"}), encoding="utf-8")
        proc = MagicMock()
        with (
            patch.object(gl, "launch_gateway", return_value=proc),
            patch.object(gl, "wait_for_port", return_value=True),
            patch.object(gl, "stop_gateway") as mock_stop,
            patch.object(gl.time, "sleep"),
            patch("backend.executor.main"),
            patch.object(gl, "_backup_after_run"),
        ):
            code = gl.run_nightly(today=MONDAY)
        assert code == 0
        mock_stop.assert_not_called()  # the live restore drill owns the Gateway
        assert not (tmp_path / "gateway.lock").exists()  # tenancy released

    def test_popen_crash_before_launch_still_releases_the_gateway_lock(self, monkeypatch, tmp_path):
        # #547: launch_gateway used to sit OUTSIDE the try/finally — a Popen
        # raise (AV, permissions) leaked the gateway lock until the 2h
        # staleness break, aborting a same-window retry with "NOT RUN".
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
        with (
            patch.object(gl, "launch_gateway", side_effect=OSError("Access is denied")),
            patch.object(gl, "stop_gateway") as mock_stop,
            pytest.raises(OSError, match="Access is denied"),
        ):
            gl.run_nightly(today=MONDAY)
        assert not (tmp_path / "gateway.lock").exists()  # tenancy released, not leaked
        mock_stop.assert_not_called()  # no proc to tear down

    def test_holiday_executor_crash_also_alerts(self):
        with (
            patch.object(gl, "launch_gateway") as mock_launch,
            patch.object(gl, "_urgent") as mock_urgent,
            patch("backend.executor.main", side_effect=RuntimeError("boom")),
        ):
            code = gl.run_nightly(today=LABOR_DAY)
        assert code == 4
        mock_urgent.assert_called_once()
        mock_launch.assert_not_called()


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
