"""The live scheduled task (live_cli.run_live_nightly) and the live Gateway
check (#1065, #1098). The live Gateway runs continuously under IBC, so the
nightly task must never launch or kill one; a Gateway that is not logged in
is refused with an urgent push naming the 2FA approval. Every Gateway and
broker call is faked."""

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from backend import gateway_lifecycle as gl
from backend import live_cli, live_grant
from backend.broker import ConnectionFailedError, LiveAccountRequiredError
from backend.live_executor import GATEWAY_NOT_LOGGED_IN, LiveConfig, LiveGatewayNotLoggedIn

TRADING_DAY = datetime.date(2026, 10, 30)


@pytest.fixture
def quiet(monkeypatch):
    alerts: list[tuple] = []
    monkeypatch.setattr(live_cli, "_alert", lambda *a, **k: alerts.append(a))
    return alerts


def _forbid_gateway_lifecycle(monkeypatch) -> list[int]:
    def forbidden(*a, **k):
        raise AssertionError("the live nightly task must never launch or kill a Gateway (#1098)")

    for name in ("launch_gateway", "stop_gateway", "stop_gateway_tree_only", "kill_detached_gateway_processes"):
        monkeypatch.setattr(gl, name, forbidden)
    backups: list[int] = []
    monkeypatch.setattr(gl, "_backup_after_run", lambda: backups.append(1))
    return backups


def _cfg() -> LiveConfig:
    return LiveConfig("U1", "127.0.0.1", 4001, 17, "C:/IBC/live.bat", armed=False, dry_run_requested=True)


def test_nightly_runs_against_the_running_gateway_and_backs_up(quiet, monkeypatch):
    backups = _forbid_gateway_lifecycle(monkeypatch)
    ran: list = []
    monkeypatch.setattr(live_cli, "_run_once", lambda config, rehearse: ran.append(rehearse) or 0)
    assert live_cli.run_live_nightly(_cfg(), today=TRADING_DAY) == 0
    assert ran == [False] and backups == [1]


def test_nightly_backs_up_even_when_the_run_refuses(quiet, monkeypatch):
    backups = _forbid_gateway_lifecycle(monkeypatch)
    monkeypatch.setattr(live_cli, "_run_once", lambda config, rehearse: 3)
    assert live_cli.run_live_nightly(_cfg(), today=TRADING_DAY) == 3
    assert backups == [1]


def test_nightly_on_a_holiday_skips_the_backup(quiet, monkeypatch):
    backups = _forbid_gateway_lifecycle(monkeypatch)
    monkeypatch.setattr(live_cli, "_run_once", lambda config, rehearse: 0)
    assert live_cli.run_live_nightly(_cfg(), today=datetime.date(2026, 10, 31)) == 0
    assert backups == []


def test_not_logged_in_refusal_is_an_urgent_push_titled_with_the_action(quiet, monkeypatch):
    async def no_init():
        return None

    async def not_logged_in(config, rehearse):
        raise LiveGatewayNotLoggedIn(f"{GATEWAY_NOT_LOGGED_IN} (the live API port did not answer)")

    monkeypatch.setattr("backend.database.init_db", no_init)
    monkeypatch.setattr(live_cli, "run_live_executor", not_logged_in)
    assert asyncio.run(live_cli._execute(_cfg(), False)) == 3
    title, body = quiet[0][0], quiet[0][1]
    assert GATEWAY_NOT_LOGGED_IN in title and "approve 2FA on your phone" in title
    assert "did not answer" in body


class _Session:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.closed = False

    def open(self) -> None:
        if self.error:
            raise self.error

    def close(self) -> None:
        self.closed = True


def test_check_port_closed_pushes_not_logged_in(quiet, monkeypatch):
    monkeypatch.setattr(live_cli, "default_gateway_probe", lambda h, p: False)
    monkeypatch.setattr(live_cli, "default_broker_factory", lambda c: pytest.fail("no broker without a port"))
    assert live_cli.check_live_gateway(_cfg()) == 3
    assert GATEWAY_NOT_LOGGED_IN in quiet[0][0]


@pytest.mark.parametrize(
    "error",
    [
        ConnectionFailedError("Could not open IB Gateway session: TimeoutError()"),
        LiveAccountRequiredError("the Gateway reported no managed accounts — refusing to trade"),
    ],
)
def test_check_handshake_without_a_login_pushes_not_logged_in(quiet, monkeypatch, error):
    monkeypatch.setattr(live_cli, "default_gateway_probe", lambda h, p: True)
    monkeypatch.setattr(live_cli, "default_broker_factory", lambda c: _Session(error))
    assert live_cli.check_live_gateway(_cfg()) == 3
    assert GATEWAY_NOT_LOGGED_IN in quiet[0][0]


def test_check_account_mismatch_keeps_its_own_message(quiet, monkeypatch):
    monkeypatch.setattr(live_cli, "default_gateway_probe", lambda h, p: True)
    error = LiveAccountRequiredError("the connected account does not match IBKR_LIVE_ACCOUNT_ID")
    monkeypatch.setattr(live_cli, "default_broker_factory", lambda c: _Session(error))
    assert live_cli.check_live_gateway(_cfg()) == 2
    assert "NOT RUN" in quiet[0][0] and "does not match" in quiet[0][1]


def test_check_logged_in_is_quiet(quiet, monkeypatch, capsys):
    session = _Session()
    monkeypatch.setattr(live_cli, "default_gateway_probe", lambda h, p: True)
    monkeypatch.setattr(live_cli, "default_broker_factory", lambda c: session)
    assert live_cli.check_live_gateway(_cfg()) == 0
    assert quiet == [] and session.closed
    assert "logged in" in capsys.readouterr().out


def test_grant_command_reports_refusals(monkeypatch):
    async def refuse(*a, **k):
        raise live_grant.GrantRefused("nope")

    async def no_init():
        return None

    monkeypatch.setattr("backend.database.init_db", no_init)
    monkeypatch.setattr(live_grant, "grant_stage1", refuse)
    args = SimpleNamespace(command="grant", book="B36", attest="x")
    assert asyncio.run(live_cli._grant_command(args)) == 2
