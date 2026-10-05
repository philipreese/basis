"""The morning fill check (#236): notification only, always says something,
and never runs on holidays or writes to the database."""

import datetime
import logging
import logging.handlers
from unittest.mock import MagicMock, patch

import pytest

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

    def test_plain_headline_leads_and_raw_line_follows(self):
        # #1115: the real B10 fill, decoded — the raw leg line stays below it.
        ref = "basis:B10:o_8bd5e627:open"
        execs = [
            {"order_ref": ref, "side": "BOT", "quantity": 1.0, "price": 10.70, "symbol": "GLD   261120P00380000"},
            {"order_ref": ref, "side": "SLD", "quantity": 1.0, "price": 8.35, "symbol": "GLD   261120P00375000"},
        ]
        _, body = compose_fill_push(execs)
        headline, raw = body.split("\n")
        assert headline.startswith("B10 opened a GLD bear put spread (bets GLD falls). Paid $235.")
        assert raw.startswith(f"  {ref} — 2 leg fill(s): BOT GLD   261120P00380000 @ 10.70")

    def test_context_partial_falls_back_to_raw_line_alone(self):
        ref = "basis:B10:o_1:open"
        execs = [
            {"order_ref": ref, "side": "BOT", "quantity": 1.0, "price": 10.70, "symbol": "GLD   261120P00380000"},
            {"order_ref": ref, "side": "SLD", "quantity": 1.0, "price": 8.35, "symbol": "GLD   261120P00375000"},
        ]
        _, body = compose_fill_push(execs, {ref: fc.OrderContext(order_quantity=2)})
        assert body == f"{ref} — 2 leg fill(s): BOT GLD   261120P00380000 @ 10.70, SLD GLD   261120P00375000 @ 8.35"

    def test_formatter_crash_never_drops_the_push(self):
        ref = "basis:B10:o_1:open"
        execs = [{"order_ref": ref, "side": "BOT", "quantity": 1.0, "price": 3.0, "symbol": "XSP   261120P00500000"}]
        with patch.object(fc, "describe_fill", side_effect=RuntimeError("bug")):
            title, body = compose_fill_push(execs)
        assert title == "basis fills: 1 order(s) filled"
        assert body == f"{ref} — 1 leg fill(s): BOT XSP   261120P00500000 @ 3.00"

    def test_multiplier_parsing(self):
        assert fc._multiplier("100") == 100.0
        assert fc._multiplier("10") == 10.0
        assert fc._multiplier("") == 100.0
        assert fc._multiplier("0") == 100.0


class TestLoadOrderContexts:
    """The read-only DB lookup behind the headline (#1115)."""

    @pytest.fixture
    def session_maker(self):
        import asyncio

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from backend.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        async def _create():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.run(_create())
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        asyncio.run(engine.dispose())

    def _seed(self, maker):
        import asyncio

        from backend.models import BookModel, OrderModel, PositionModel, ShareOrderModel

        legs = [
            {
                "occ": "GLD261120P00380000",
                "option_type": "PUT",
                "direction": "LONG",
                "strike": 380.0,
                "expiration": "2026-11-20",
            },
            {
                "occ": "GLD261120P00375000",
                "option_type": "PUT",
                "direction": "SHORT",
                "strike": 375.0,
                "expiration": "2026-11-20",
            },
        ]

        def order(oid, ref, action, meta, position_id=None):
            return OrderModel(
                id=oid,
                book_id="B10",
                position_id=position_id,
                order_ref=ref,
                action=action,
                combo_legs=meta,
                limit_price=2.35,
                decision_midpoint=2.35,
                status="SUBMITTED",
            )

        async def _go():
            async with maker() as s:
                s.add(
                    BookModel(
                        id="B10", name="B10", starting_capital=1.0, cash_balance=1.0, status="ACTIVE", created_at="x"
                    )
                )
                s.add(
                    BookModel(
                        id="B36", name="B36", starting_capital=1.0, cash_balance=1.0, status="ACTIVE", created_at="x"
                    )
                )
                s.add(
                    PositionModel(
                        id="pos_1",
                        underlying="GLD",
                        strategy_type="BEAR_PUT_SPREAD",
                        legs=legs,
                        entry_date="2026-10-02",
                        expiration_date="2026-11-20",
                        entry_premium=2.35,
                        premium_direction="DEBIT",
                        current_value_per_share=2.35,
                        contracts=1,
                        max_profit=265,
                        max_loss=235,
                        notes="",
                        status="OPEN",
                        book_id="B10",
                    )
                )
                await s.flush()
                s.add(
                    order(
                        "o_1",
                        "basis:B10:o_1:open",
                        "OPEN",
                        {"legs": legs, "quantity": 1, "strategy_type": "BEAR_PUT_SPREAD"},
                        "pos_1",
                    )
                )
                s.add(
                    order(
                        "o_1_tp",
                        "basis:B10:o_1:open:tp",
                        "CLOSE",
                        {
                            "legs": legs,
                            "quantity": 1,
                            "strategy_type": "BEAR_PUT_SPREAD",
                            "exit_trigger": "PROFIT_TARGET",
                        },
                    )
                )
                s.add(
                    order(
                        "o_2",
                        "basis:B10:o_2:close",
                        "CLOSE",
                        {"legs": [{**legs[0], "occ": ""}], "quantity": 1, "exit_trigger": "TIME_RULE"},
                        "pos_1",
                    )
                )
                s.add(
                    ShareOrderModel(
                        id="s_1",
                        book_id="B36",
                        order_ref="basis:B36:s_1:share",
                        symbol="SCHB",
                        side="BUY",
                        quantity=12,
                        limit_price=25.0,
                        decision_close=25.0,
                        signal_date="2026-09-30",
                        status="SUBMITTED",
                        created_at="x",
                    )
                )
                await s.commit()

        asyncio.run(_go())

    def test_contexts_for_open_tp_close_share_and_unknown(self, session_maker):
        import asyncio

        self._seed(session_maker)
        refs = [
            "basis:B10:o_1:open",
            "basis:B10:o_1:open:tp",
            "basis:B10:o_2:close",
            "basis:B36:s_1:share",
            "basis:B10:o_x:open",
        ]
        ctx = asyncio.run(fc.load_order_contexts(refs, session_maker))
        opened = ctx["basis:B10:o_1:open"]
        assert opened.strategy_type == "BEAR_PUT_SPREAD"
        assert opened.order_quantity == 1
        assert opened.leg_occs == ("GLD261120P00380000", "GLD261120P00375000")
        tp = ctx["basis:B10:o_1:open:tp"]
        assert tp.exit_trigger == "PROFIT_TARGET"
        assert (tp.entry_premium, tp.premium_direction) == (2.35, "DEBIT")  # reached through the parent
        close = ctx["basis:B10:o_2:close"]
        assert close.strategy_type == "BEAR_PUT_SPREAD"  # from the position: closes don't carry it
        assert close.leg_occs == ()  # a leg without occ disables the exact check, never guesses
        assert ctx["basis:B36:s_1:share"].share_quantity == 12
        assert "basis:B10:o_x:open" not in ctx

    def test_tp_with_no_parent_has_no_entry(self, session_maker):
        import asyncio

        self._seed(session_maker)
        ctx = asyncio.run(fc.load_order_contexts(["basis:B10:o_1:open:tp"], session_maker))
        assert ctx["basis:B10:o_1:open:tp"].entry_premium == 2.35
        # an orphan :tp (parent missing) still gets its own fields
        import asyncio as _a

        from backend.models import OrderModel

        async def _orphan():
            async with session_maker() as s:
                s.add(
                    OrderModel(
                        id="o_9_tp",
                        book_id="B10",
                        order_ref="basis:B10:o_9:open:tp",
                        action="CLOSE",
                        combo_legs={"exit_trigger": "PROFIT_TARGET"},
                        limit_price=1.0,
                        decision_midpoint=1.0,
                        status="SUBMITTED",
                    )
                )
                await s.commit()

        _a.run(_orphan())
        orphan = asyncio.run(fc.load_order_contexts(["basis:B10:o_9:open:tp"], session_maker))["basis:B10:o_9:open:tp"]
        assert orphan.entry_premium is None and orphan.exit_trigger == "PROFIT_TARGET"

    def test_best_effort_swallows_database_errors(self):
        execs = [{"order_ref": "basis:B10:o_1:open", "side": "BOT", "quantity": 1.0, "price": 1.0, "symbol": "X"}]
        with patch.object(fc, "load_order_contexts", side_effect=RuntimeError("db down")):
            assert fc._load_contexts_best_effort(execs) == {}

    def test_best_effort_skips_foreign_refs(self):
        with patch.object(fc, "load_order_contexts") as mock_load:
            assert (
                fc._load_contexts_best_effort(
                    [{"order_ref": "manual", "side": "BOT", "quantity": 1.0, "price": 1.0, "symbol": "X"}]
                )
                == {}
            )
        mock_load.assert_not_called()

    def test_best_effort_returns_loaded_contexts(self):
        execs = [{"order_ref": "basis:B10:o_1:open", "side": "BOT", "quantity": 1.0, "price": 1.0, "symbol": "X"}]
        want = {"basis:B10:o_1:open": fc.OrderContext(order_quantity=1)}

        async def _fake(refs, session_maker=None):
            assert refs == ["basis:B10:o_1:open"]
            return want

        with patch.object(fc, "load_order_contexts", _fake):
            assert fc._load_contexts_best_effort(execs) == want


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

    def test_gateway_tenancy_lock_also_leaves_the_gateway_up(self, monkeypatch, tmp_path):
        # Audit II R3 (#471): the nightly run holds the gateway lock from
        # BEFORE its launch — inside its warmup/port-poll window there is no
        # executor lock yet, and that window is exactly when this teardown
        # used to kill its Gateway.
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
        (tmp_path / "gateway.lock").write_text('{"pid": 1, "token": "live"}')
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
        mock_stop.assert_not_called()
        assert not (tmp_path / "fill_check.lock").exists()  # own marker released

    def test_restore_drill_lock_also_leaves_the_gateway_up(self, monkeypatch, tmp_path):
        # #681: fill_check's teardown only ever knew about "executor" and
        # "gateway" — restore_drill (#641) is a fourth Gateway tenant, and a
        # drill mid-query on the shared Gateway is exactly as live as a
        # catch-up executor run. Checked via run_lock.GATEWAY_TENANT_LOCKS
        # now, not a hand-spelled two-name subset.
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
        (tmp_path / "restore_drill.lock").write_text('{"pid": 1, "token": "live"}')
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
        mock_stop.assert_not_called()  # the running restore drill owns the Gateway

    def test_second_fill_check_aborts_without_launching(self, monkeypatch, tmp_path):
        # Audit II R3 (#471): the fill_check lock is its tenancy marker —
        # a second concurrent check must not launch a second Gateway.
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
        (tmp_path / "fill_check.lock").write_text('{"pid": 1, "token": "live"}')
        with patch("backend.gateway_lifecycle.launch_gateway") as mock_launch:
            code = run_fill_check(today=datetime.date(2026, 8, 24))
        assert code == 4
        mock_launch.assert_not_called()

    def test_popen_crash_before_launch_still_releases_the_fill_check_lock(self, monkeypatch, tmp_path):
        # #547: launch_gateway used to sit OUTSIDE the try/finally — a Popen
        # raise (AV, permissions) leaked the fill_check lock until the 2h
        # staleness break, aborting a same-window retry with "NOT RUN".
        script = tmp_path / "StartGateway.bat"
        script.write_text("rem stub")
        monkeypatch.setenv("IBC_START_SCRIPT", str(script))
        monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
        with (
            patch("backend.gateway_lifecycle.launch_gateway", side_effect=OSError("Access is denied")),
            patch("backend.gateway_lifecycle.stop_gateway") as mock_stop,
            pytest.raises(OSError, match="Access is denied"),
        ):
            run_fill_check(today=datetime.date(2026, 8, 24))
        assert not (tmp_path / "fill_check.lock").exists()  # tenancy released, not leaked
        mock_stop.assert_not_called()  # no proc to tear down

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
