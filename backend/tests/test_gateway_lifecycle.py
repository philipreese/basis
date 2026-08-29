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

    def test_zero_timeout_still_attempts_one_connect(self):
        # #884: run_nightly's connect_fn passes timeout_seconds=0 to mean
        # "single probe" — the deadline-first loop returned False without
        # ever touching the socket, so every nightly launch alerted
        # dead-or-stalled against a healthy, connectable Gateway.
        with patch.object(gl.socket, "create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = MagicMock()
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            assert gl.wait_for_port("127.0.0.1", 4002, timeout_seconds=0)
            assert mock_conn.call_count == 1

    def test_zero_timeout_fails_after_exactly_one_attempt(self):
        with patch.object(gl.socket, "create_connection", side_effect=OSError) as mock_conn:
            assert not gl.wait_for_port("127.0.0.1", 4002, timeout_seconds=0, sleep=lambda _s: None)
            assert mock_conn.call_count == 1


class FakeClock:
    def __init__(self, step: float = 10.0, initial: float = 0.0):
        self.current = initial
        self.step = step

    def __call__(self) -> float:
        val = self.current
        self.current += self.step
        return val


class TestMemorySampling:
    def test_get_free_memory_gb_windows(self):
        val = gl.get_free_memory_gb()
        assert isinstance(val, float)
        assert val > 0.0

    def test_get_free_memory_gb_fallback_when_not_windows(self, monkeypatch):
        monkeypatch.setattr(gl.sys, "platform", "linux")
        monkeypatch.setattr(
            gl.os, "sysconf", lambda name: 1024 * 1024 if name == "SC_AVPHYS_PAGES" else 4096, raising=False
        )
        val = gl.get_free_memory_gb()
        assert val == 4.0

    def test_get_free_memory_gb_is_none_when_unreadable(self, monkeypatch):
        # An unreadable platform yields None — never a fabricated number that
        # could suppress (or invent) a memory-pressure line in a finding.
        monkeypatch.setattr(gl.sys, "platform", "other_os")
        if hasattr(gl.os, "sysconf"):
            monkeypatch.delattr(gl.os, "sysconf")
        assert gl.get_free_memory_gb() is None


class TestIbcLogProgression:
    def test_get_latest_ibc_log_mtime_missing_dir(self, tmp_path):
        assert gl.get_latest_ibc_log_mtime(log_dir=tmp_path / "nonexistent") is None

    def test_get_latest_ibc_log_mtime_picks_newest(self, tmp_path):
        log1 = tmp_path / "IBC-Mon.txt"
        log2 = tmp_path / "IBC-Tue.txt"
        log1.write_text("log1", encoding="utf-8")
        log2.write_text("log2", encoding="utf-8")
        gl.os.utime(log1, (1000.0, 1000.0))
        gl.os.utime(log2, (2000.0, 2000.0))
        assert gl.get_latest_ibc_log_mtime(log_dir=tmp_path) == 2000.0

    def test_get_latest_ibc_log_mtime_resolves_from_start_script(self, tmp_path, monkeypatch):
        monkeypatch.delenv("IBC_LOG_DIR", raising=False)
        ibc_dir = tmp_path / "IBC"
        ibc_dir.mkdir()
        logs_dir = ibc_dir / "Logs"
        logs_dir.mkdir()
        script = ibc_dir / "StartGateway.bat"
        script.write_text("rem", encoding="utf-8")
        log = logs_dir / "gateway.log"
        log.write_text("content", encoding="utf-8")
        gl.os.utime(log, (1500.0, 1500.0))
        assert gl.get_latest_ibc_log_mtime(start_script=str(script)) == 1500.0

    def test_is_proc_alive(self):
        assert not gl.is_proc_alive(None)
        proc_live = MagicMock()
        proc_live.poll.return_value = None
        assert gl.is_proc_alive(proc_live)
        proc_dead = MagicMock()
        proc_dead.poll.return_value = 0
        assert not gl.is_proc_alive(proc_dead)

    def test_is_gateway_progressing(self):
        proc = MagicMock()
        proc.poll.return_value = None
        # mtime is 1000.0, current time is 1020.0 (within 30s) -> True
        assert gl.is_gateway_progressing(
            proc=proc,
            window_seconds=30.0,
            time_fn=lambda: 1020.0,
            latest_mtime_fn=lambda **kw: 1000.0,
        )
        # mtime is 1000.0, current time is 1040.0 (40s later, stale) -> False
        assert not gl.is_gateway_progressing(
            proc=proc,
            window_seconds=30.0,
            time_fn=lambda: 1040.0,
            latest_mtime_fn=lambda **kw: 1000.0,
        )
        # proc is dead -> False even if mtime is fresh
        proc.poll.return_value = 1
        assert not gl.is_gateway_progressing(
            proc=proc,
            window_seconds=30.0,
            time_fn=lambda: 1010.0,
            latest_mtime_fn=lambda **kw: 1000.0,
        )
        # No log mtime -> False
        proc.poll.return_value = None
        assert not gl.is_gateway_progressing(
            proc=proc,
            latest_mtime_fn=lambda **kw: None,
        )


class TestWaitForGatewayPort:
    def test_opens_immediately(self):
        res = gl.wait_for_gateway_port(
            "127.0.0.1",
            4002,
            timeout_seconds=180,
            connect_fn=lambda h, p: True,
            monotonic=lambda: 0.0,
            free_memory_gb=2.0,
        )
        assert res.status == gl.GatewayPortStatus.OPEN
        assert res.is_open
        assert not res.memory_under_pressure

    def test_dead_or_stalled_at_180s_produces_dead_or_stalled_timeout(self):
        clock = FakeClock(step=50.0, initial=0.0)
        res = gl.wait_for_gateway_port(
            "127.0.0.1",
            4002,
            timeout_seconds=180,
            interval_seconds=5,
            connect_fn=lambda h, p: False,
            is_progressing_fn=lambda **kw: False,
            sleep=lambda s: None,
            monotonic=clock,
            free_memory_gb=0.4,
        )
        assert res.status == gl.GatewayPortStatus.TIMEOUT_DEAD_OR_STALLED
        assert not res.is_open
        assert res.memory_under_pressure
        assert res.free_memory_gb == 0.4

    def test_alive_and_progressing_at_180s_reprobes_and_succeeds_in_grace_window(self):
        # Fails during initial 180s, then succeeds during grace window (at monotonic ~240s)
        clock = FakeClock(step=40.0, initial=0.0)

        # Connect returns False while time < 250, then True
        def _connect(h, p):
            return clock.current >= 250.0

        res = gl.wait_for_gateway_port(
            "127.0.0.1",
            4002,
            timeout_seconds=180,
            grace_timeout_seconds=120,
            interval_seconds=5,
            connect_fn=_connect,
            is_progressing_fn=lambda **kw: True,
            sleep=lambda s: None,
            monotonic=clock,
            free_memory_gb=1.0,
        )
        assert res.status == gl.GatewayPortStatus.OPEN_SLOW
        assert res.is_open
        assert res.memory_under_pressure

    def test_alive_and_progressing_fails_after_full_grace_window(self):
        clock = FakeClock(step=50.0, initial=0.0)
        res = gl.wait_for_gateway_port(
            "127.0.0.1",
            4002,
            timeout_seconds=180,
            grace_timeout_seconds=120,
            interval_seconds=5,
            connect_fn=lambda h, p: False,
            is_progressing_fn=lambda **kw: True,
            sleep=lambda s: None,
            monotonic=clock,
            free_memory_gb=8.0,
        )
        assert res.status == gl.GatewayPortStatus.TIMEOUT_PROGRESSING
        assert not res.is_open
        assert not res.memory_under_pressure


class TestWaitForTenantClear:
    def test_returns_true_immediately_when_no_other_tenant(self):
        sleeps: list[float] = []
        assert gl.wait_for_tenant_clear(
            "gateway", tenant_active=lambda caller: False, sleep=sleeps.append, monotonic=lambda: 0.0
        )
        assert sleeps == []  # no wait needed — never slept

    def test_polls_at_the_configured_interval_until_clear(self):
        # Active for the first two checks, clear on the third.
        calls = iter([True, True, False])
        sleeps: list[float] = []
        clock = iter([0.0, 0.0, 5.0, 10.0, 15.0])
        assert gl.wait_for_tenant_clear(
            "gateway",
            timeout_seconds=60,
            interval_seconds=5,
            tenant_active=lambda caller: next(calls),
            sleep=sleeps.append,
            monotonic=lambda: next(clock),
        )
        assert sleeps == [5, 5]

    def test_returns_false_when_tenant_never_clears_by_the_deadline(self):
        clock = iter(range(100))
        assert not gl.wait_for_tenant_clear(
            "gateway",
            timeout_seconds=3,
            interval_seconds=1,
            tenant_active=lambda caller: True,
            sleep=lambda _s: None,
            monotonic=lambda: next(clock),
        )

    def test_passes_caller_through_to_the_tenant_check(self):
        seen: list[str] = []

        def _tenant_active(caller: str) -> bool:
            seen.append(caller)
            return False

        gl.wait_for_tenant_clear("fill_check", tenant_active=_tenant_active, sleep=lambda _s: None)
        assert seen == ["fill_check"]


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
            patch.object(
                gl,
                "wait_for_gateway_port",
                return_value=gl.GatewayPortResult(
                    status=gl.GatewayPortStatus.TIMEOUT_DEAD_OR_STALLED,
                    elapsed_seconds=180.0,
                    free_memory_gb=4.0,
                    memory_under_pressure=False,
                ),
            ),
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

    def test_port_timeout_with_memory_pressure_alerts_memory_cause(self, monkeypatch, tmp_path):
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
        proc = MagicMock()
        with (
            patch.object(gl, "get_free_memory_gb", return_value=0.4),
            patch.object(gl, "launch_gateway", return_value=proc),
            patch.object(
                gl,
                "wait_for_gateway_port",
                return_value=gl.GatewayPortResult(
                    status=gl.GatewayPortStatus.TIMEOUT_DEAD_OR_STALLED,
                    elapsed_seconds=180.0,
                    free_memory_gb=0.4,
                    memory_under_pressure=True,
                ),
            ),
            patch.object(gl, "stop_gateway") as mock_stop,
            patch.object(gl, "_urgent") as mock_urgent,
            patch("backend.executor.main") as mock_exec,
            patch.object(gl, "_backup_after_run"),
        ):
            code = gl.run_nightly(today=MONDAY)
        assert code == 3
        mock_urgent.assert_called_once()
        title, body = mock_urgent.call_args[0]
        assert "NOT RUN" in title
        assert "machine under memory pressure (0.4 GB free)" in body
        assert "gateway may be slow rather than broken" in body
        mock_stop.assert_called_once_with(proc)
        mock_exec.assert_not_called()

    def test_port_slow_success_in_grace_window_runs_executor(self, monkeypatch, tmp_path):
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
        proc = MagicMock()
        with (
            patch.object(gl, "get_free_memory_gb", return_value=0.5),
            patch.object(gl, "launch_gateway", return_value=proc),
            patch.object(
                gl,
                "wait_for_gateway_port",
                return_value=gl.GatewayPortResult(
                    status=gl.GatewayPortStatus.OPEN_SLOW,
                    elapsed_seconds=240.0,
                    free_memory_gb=0.5,
                    memory_under_pressure=True,
                ),
            ),
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
            # #838: this test's subject is teardown deferral, not the new
            # launch-side wait (covered by TestWaitForTenantClear /
            # test_aborts_with_audited_alert_when_tenant_never_clears) — a
            # live fill_check.lock here would otherwise make the launch wait
            # poll for real up to its 5-minute deadline.
            patch.object(gl, "wait_for_tenant_clear", return_value=True),
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
            # #838: same reasoning as the fill_check-lock test above — this
            # test's subject is teardown deferral, not the launch-side wait.
            patch.object(gl, "wait_for_tenant_clear", return_value=True),
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

    def test_waits_for_a_live_tenant_then_launches(self, monkeypatch, tmp_path):
        # #838: a preflight still active (e.g. woken late by
        # -StartWhenAvailable -WakeToRun) must not collide with the nightly
        # launch — the launch waits it out rather than colliding.
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
        proc = MagicMock()
        with (
            patch.object(gl, "wait_for_tenant_clear", return_value=True) as mock_wait,
            patch.object(gl, "launch_gateway", return_value=proc) as mock_launch,
            patch.object(gl, "wait_for_port", return_value=True),
            patch.object(gl, "stop_gateway") as mock_stop,
            patch.object(gl.time, "sleep"),
            patch("backend.executor.main") as mock_exec,
            patch.object(gl, "_backup_after_run"),
        ):
            code = gl.run_nightly(today=MONDAY)
        assert code == 0
        mock_wait.assert_called_once_with("gateway")
        mock_launch.assert_called_once()  # only launched AFTER the wait cleared
        mock_exec.assert_called_once()
        mock_stop.assert_called_once_with(proc)

    def test_aborts_with_audited_alert_when_tenant_never_clears(self, monkeypatch, tmp_path):
        # #838: a loud NOT-RUN beats launching a second Gateway into a
        # clientId collision when the other tenant is still live at the
        # bounded-wait deadline.
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
        with (
            patch.object(gl, "wait_for_tenant_clear", return_value=False),
            patch.object(gl, "launch_gateway") as mock_launch,
            patch.object(gl, "stop_gateway") as mock_stop,
            patch.object(gl, "_urgent") as mock_urgent,
            patch("backend.executor.main") as mock_exec,
            patch.object(gl, "_backup_after_run") as mock_backup,
        ):
            code = gl.run_nightly(today=MONDAY)
        assert code == 6
        mock_launch.assert_not_called()
        mock_exec.assert_not_called()
        mock_stop.assert_not_called()
        mock_urgent.assert_called_once()
        assert mock_urgent.call_args.kwargs.get("event_type") == "EXECUTOR_ABORTED_TENANT_ACTIVE"
        # The gateway tenancy lock is still released even on this abort path.
        assert not (tmp_path / "gateway.lock").exists()
        # #548 LOW-2: backup still runs in finally regardless of exit reason.
        mock_backup.assert_called_once()

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


class TestProcessDiscrimination:
    def test_matches_ibc_gateway_and_jts_command_lines(self):
        assert gl.matches_gateway_cmdline(r'cmd.exe /c "C:\IBC\StartGateway.bat"')
        assert gl.matches_gateway_cmdline(r"java.exe -cp C:\IBC\IBC.jar;C:\IBC\ibgateway\985 ibc.IbcAlpha")
        assert gl.matches_gateway_cmdline(r"C:\Jts\ibgateway\985\ibgateway.exe")
        assert gl.matches_gateway_cmdline(r"conhost.exe --headless C:\IBC\Logs\IBC-20260827.txt")
        assert gl.matches_gateway_cmdline(r"C:\jts\tws.exe")
        assert gl.matches_gateway_cmdline("StartGateway")

    def test_rejects_unrelated_processes(self):
        assert not gl.matches_gateway_cmdline(r"java.exe -jar C:\Apps\unrelated.jar")
        assert not gl.matches_gateway_cmdline(r"cmd.exe /c echo hello")
        assert not gl.matches_gateway_cmdline(r"conhost.exe 0x4")
        assert not gl.matches_gateway_cmdline(r"python.exe -m backend.executor")
        assert not gl.matches_gateway_cmdline("")
        assert not gl.matches_gateway_cmdline(None)


class TestEnumerateProcesses:
    def test_parses_json_array_from_powershell(self):
        fake_stdout = json.dumps(
            [
                {
                    "ProcessId": 1001,
                    "Name": "cmd.exe",
                    "CommandLine": "cmd /c C:\\IBC\\test.bat",
                    "Created": 1724784240.0,
                },
                {
                    "ProcessId": 1002,
                    "Name": "java.exe",
                    "CommandLine": "java -cp C:\\Jts\\ibgateway.jar",
                    "Created": 1724784245.0,
                },
            ]
        )
        fake_res = MagicMock(returncode=0, stdout=fake_stdout)
        procs = gl._enumerate_processes_windows(run=lambda *a, **k: fake_res)
        assert len(procs) == 2
        assert procs[0].pid == 1001
        assert procs[0].name == "cmd.exe"
        assert procs[0].created_at == 1724784240.0
        assert procs[1].pid == 1002

    def test_parses_single_json_object(self):
        fake_stdout = json.dumps(
            {"ProcessId": 1001, "Name": "cmd.exe", "CommandLine": "cmd /c C:\\IBC\\test.bat", "Created": 1724784240.0}
        )
        fake_res = MagicMock(returncode=0, stdout=fake_stdout)
        procs = gl._enumerate_processes_windows(run=lambda *a, **k: fake_res)
        assert len(procs) == 1
        assert procs[0].pid == 1001

    def test_handles_empty_or_failed_output(self):
        fake_res = MagicMock(returncode=1, stdout="")
        assert gl._enumerate_processes_windows(run=lambda *a, **k: fake_res) == []

        fake_res_bad_json = MagicMock(returncode=0, stdout="not valid json")
        assert gl._enumerate_processes_windows(run=lambda *a, **k: fake_res_bad_json) == []


class TestStopGatewayTreeOnly:
    def test_kills_only_its_own_pid_no_sweep_when_no_cutoff(self):
        # #838: when no created_after is passed, only kills the taskkill tree.
        proc = MagicMock()
        proc.pid = 4242
        run = MagicMock()
        gl.stop_gateway_tree_only(proc, run=run)
        run.assert_called_once_with(["taskkill", "/PID", "4242", "/T", "/F"], capture_output=True, check=False)

    def test_kills_detached_ibc_child_created_after_launch(self):
        # #851 requirement (a): detached IBC child created after launch -> killed
        cutoff = 1000.0
        proc = MagicMock(pid=4242)
        run = MagicMock()
        fake_processes = [
            gl.ProcessInfo(pid=5001, name="cmd.exe", cmdline=r"cmd.exe /c C:\IBC\StartGateway.bat", created_at=1001.0),
            gl.ProcessInfo(pid=5002, name="java.exe", cmdline=r"C:\Jts\ibgateway\985\ibgateway.exe", created_at=1005.0),
            gl.ProcessInfo(pid=5003, name="conhost.exe", cmdline=r"conhost.exe C:\IBC\Logs\IBC.txt", created_at=1002.0),
        ]
        killed = gl.stop_gateway_tree_only(
            proc=proc,
            created_after=cutoff,
            run=run,
            enumerate_processes=lambda: fake_processes,
            now=lambda: 1010.0,
        )
        assert len(killed) == 3
        pids_killed = [p.pid for p in killed]
        assert pids_killed == [5001, 5002, 5003]
        # Tree kill on proc + 3 taskkill calls on the detached processes
        assert run.call_count == 4
        assert run.call_args_list[0][0][0] == ["taskkill", "/PID", "4242", "/T", "/F"]
        assert run.call_args_list[1][0][0] == ["taskkill", "/PID", "5001", "/T", "/F"]
        assert run.call_args_list[2][0][0] == ["taskkill", "/PID", "5002", "/T", "/F"]
        assert run.call_args_list[3][0][0] == ["taskkill", "/PID", "5003", "/T", "/F"]

    def test_preserves_preexisting_gateway_created_before_launch(self):
        # #851 requirement (b): pre-existing gateway from before launch -> NOT killed
        cutoff = 1000.0
        proc = MagicMock(pid=4242)
        run = MagicMock()
        fake_processes = [
            # Pre-existing gateway launched at t=950 (before cutoff 1000)
            gl.ProcessInfo(pid=6001, name="java.exe", cmdline=r"C:\Jts\ibgateway\985\ibgateway.exe", created_at=950.0),
            # Detached process from this launch at t=1001
            gl.ProcessInfo(pid=6002, name="cmd.exe", cmdline=r"cmd.exe /c C:\IBC\StartGateway.bat", created_at=1001.0),
        ]
        killed = gl.stop_gateway_tree_only(
            proc=proc,
            created_after=cutoff,
            run=run,
            enumerate_processes=lambda: fake_processes,
            now=lambda: 1010.0,
        )
        assert len(killed) == 1
        assert killed[0].pid == 6002
        # Only proc and pid 6002 killed, NOT 6001
        called_pids = [call[0][0][2] for call in run.call_args_list]
        assert "6001" not in called_pids
        assert "6002" in called_pids

    def test_ignores_unrelated_processes_with_similar_name(self):
        # #851 requirement (c): unrelated process with similar name but wrong cmdline -> NOT killed
        cutoff = 1000.0
        proc = MagicMock(pid=4242)
        run = MagicMock()
        fake_processes = [
            gl.ProcessInfo(pid=7001, name="java.exe", cmdline=r"java.exe -jar C:\Apps\webapp.jar", created_at=1005.0),
            gl.ProcessInfo(pid=7002, name="cmd.exe", cmdline=r"cmd.exe /c dir", created_at=1005.0),
            gl.ProcessInfo(pid=7003, name="conhost.exe", cmdline=r"conhost.exe 0x4", created_at=1005.0),
        ]
        killed = gl.stop_gateway_tree_only(
            proc=proc,
            created_after=cutoff,
            run=run,
            enumerate_processes=lambda: fake_processes,
            now=lambda: 1010.0,
        )
        assert killed == []
        # Only the tree kill on proc itself
        assert run.call_count == 1
        assert run.call_args_list[0][0][0] == ["taskkill", "/PID", "4242", "/T", "/F"]

    def test_never_kills_own_process(self, monkeypatch):
        cutoff = 1000.0
        import os

        my_pid = os.getpid()
        proc = MagicMock(pid=4242)
        run = MagicMock()
        fake_processes = [
            gl.ProcessInfo(
                pid=my_pid, name="python.exe", cmdline=r"python -m backend.preflight C:\IBC\test", created_at=1005.0
            ),
        ]
        killed = gl.stop_gateway_tree_only(
            proc=proc,
            created_after=cutoff,
            run=run,
            enumerate_processes=lambda: fake_processes,
            now=lambda: 1010.0,
        )
        assert killed == []


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
