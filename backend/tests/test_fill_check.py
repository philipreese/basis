"""The morning fill check (#236): notification only, always says something,
and never runs on holidays or writes to the database."""

import datetime
import logging
import logging.handlers
from unittest.mock import MagicMock, patch

from backend import fill_check as fc
from backend.fill_check import compose_fill_push, run_fill_check

LABOR_DAY = datetime.date(2026, 9, 7)


class TestComposeFillPush:
    def test_no_fills_still_says_so(self):
        title, body = compose_fill_push([])
        assert title == "basis fills: none yet"
        assert "No resting basis orders" in body

    def test_foreign_executions_are_ignored(self):
        execs = [{"order_ref": "manual", "side": "BOT", "quantity": 1.0, "price": 2.0, "symbol": "SPY"}]
        title, _ = compose_fill_push(execs)
        assert title == "basis fills: none yet"

    def test_legs_group_by_order_ref(self):
        execs = [
            {"order_ref": "basis:B01:o_1:open", "side": "SLD", "quantity": 1.0, "price": 1.85, "symbol": "XSP P768"},
            {"order_ref": "basis:B01:o_1:open", "side": "BOT", "quantity": 1.0, "price": 1.36, "symbol": "XSP P765"},
            {"order_ref": "basis:B07:o_2:open", "side": "SLD", "quantity": 1.0, "price": 0.92, "symbol": "XSP P770"},
        ]
        title, body = compose_fill_push(execs)
        assert title == "basis fills: 2 order(s) filled"
        assert "basis:B01:o_1:open — 2 leg fill(s)" in body
        assert "SLD XSP P768 @ 1.85, BOT XSP P765 @ 1.36" in body
        assert "basis:B07:o_2:open — 1 leg fill(s)" in body


class TestFetchExecutions:
    def test_bag_level_execution_is_excluded(self):
        # IBKR includes the BAG contract's own execution at the net price
        # (#331) — the push shows legs, never a mystery conId.
        import asyncio
        from types import SimpleNamespace

        rows = [
            SimpleNamespace(
                execution=SimpleNamespace(orderRef="basis:B07:o1:open", side="BOT", shares=1, price=3.08),
                contract=SimpleNamespace(conId=28812380, secType="BAG", symbol="XSP", localSymbol=""),
            ),
            SimpleNamespace(
                execution=SimpleNamespace(orderRef="basis:B07:o1:open", side="BOT", shares=1, price=11.98),
                contract=SimpleNamespace(conId=1000, secType="OPT", symbol="XSP", localSymbol="XSP 260918C770"),
            ),
        ]

        class _IB:
            async def reqExecutionsAsync(self, _filter=None):
                return rows

        with patch("ib_async.ExecutionFilter", MagicMock()):
            execs = asyncio.run(fc._fetch_today_executions(_IB()))
        assert [e["symbol"] for e in execs] == ["XSP 260918C770"]


class TestRunFillCheck:
    def test_holiday_never_launches_gateway(self):
        with (
            patch("backend.gateway_lifecycle.launch_gateway") as mock_launch,
            patch("backend.operator.send_ntfy") as mock_ntfy,
        ):
            code = run_fill_check(today=LABOR_DAY)
        assert code == 0
        mock_launch.assert_not_called()
        mock_ntfy.assert_not_called()

    def test_missing_start_script_alerts(self, monkeypatch):
        monkeypatch.delenv("IBC_START_SCRIPT", raising=False)
        with patch("backend.operator.send_ntfy") as mock_ntfy:
            code = run_fill_check(today=datetime.date(2026, 8, 24))
        assert code == 2
        assert "NOT RUN" in mock_ntfy.call_args[0][0]

    def test_happy_path_pushes_and_tears_down(self, monkeypatch, tmp_path):
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))  # no executor lock here (#418)
        proc = MagicMock()
        execs = [
            {"order_ref": "basis:B01:o_1:open", "side": "SLD", "quantity": 1.0, "price": 1.85, "symbol": "XSP P768"},
        ]
        with (
            patch("backend.gateway_lifecycle.launch_gateway", return_value=proc) as mock_launch,
            patch("backend.gateway_lifecycle.wait_for_port", return_value=True),
            patch("backend.gateway_lifecycle.stop_gateway") as mock_stop,
            patch.object(fc.time, "sleep"),
            patch.object(fc, "_run_ib", return_value=execs),
            patch("backend.operator.send_ntfy") as mock_ntfy,
        ):
            code = run_fill_check(today=datetime.date(2026, 8, 24))
        assert code == 0
        mock_launch.assert_called_once()
        mock_stop.assert_called_once_with(proc)
        title, body = mock_ntfy.call_args[0][0], mock_ntfy.call_args[0][1]
        assert title == "basis fills: 1 order(s) filled"
        assert "basis:B01:o_1:open" in body

    def test_executor_lock_leaves_the_gateway_up(self, monkeypatch, tmp_path):
        # Audit II R2 (#418): the teardown sweep kills EVERY ibgateway java
        # process — including a catch-up executor run's, possibly between
        # its order placement and state commit. A fresh executor lock means
        # that run owns the teardown.
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
        (tmp_path / "executor.lock").write_text('{"pid": 1, "token": "live"}')
        proc = MagicMock()
        with (
            patch("backend.gateway_lifecycle.launch_gateway", return_value=proc),
            patch("backend.gateway_lifecycle.wait_for_port", return_value=True),
            patch("backend.gateway_lifecycle.stop_gateway") as mock_stop,
            patch.object(fc.time, "sleep"),
            patch.object(fc, "_run_ib", return_value=[]),
            patch("backend.operator.send_ntfy"),
        ):
            code = run_fill_check(today=datetime.date(2026, 8, 24))
        assert code == 0
        mock_stop.assert_not_called()  # the running executor owns the Gateway

    def test_unexpected_crash_pushes_an_alert(self, monkeypatch, tmp_path):
        # #271: the known failure modes push their own alerts; anything else
        # must not exit silently — nobody reads a scheduled task's exit code.
        monkeypatch.setenv("BASIS_LOG_DIR", str(tmp_path / "logs"))
        # Keep the crash-path audit row (#417) out of the real dev database.
        import backend.database as db_mod

        monkeypatch.setattr(db_mod, "DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'x.db').as_posix()}")
        with (
            patch.object(fc, "run_fill_check", side_effect=RuntimeError("boom")),
            patch("backend.operator.send_ntfy") as mock_ntfy,
        ):
            code = fc.main()
        # main() adds a rotating file handler to the root logger; detach it so
        # later tests don't keep writing into this tmp_path.
        for h in list(logging.getLogger().handlers):
            if isinstance(h, logging.handlers.RotatingFileHandler):
                logging.getLogger().removeHandler(h)
                h.close()
        assert code == 4
        title, body = mock_ntfy.call_args[0][0], mock_ntfy.call_args[0][1]
        assert title == "basis fill check CRASHED"
        assert "RuntimeError" in body
        assert (tmp_path / "logs" / "fill_check.log").exists()
