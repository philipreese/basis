"""The live scheduled task's Gateway lifecycle (#1065, live_cli.run_live_nightly)
and the grant command's refusal path. Every Gateway call is faked."""

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from backend import gateway_lifecycle as gl
from backend import live_cli, live_grant
from backend.live_executor import LiveConfig

TRADING_DAY = datetime.date(2026, 10, 30)


class _Port:
    def __init__(self, is_open: bool) -> None:
        self.is_open = is_open


@pytest.fixture
def quiet(monkeypatch):
    alerts: list[tuple] = []
    monkeypatch.setattr(live_cli, "_alert", lambda *a, **k: alerts.append(a))
    return alerts


def _patch(monkeypatch, tmp_path, port_open=True, tenant_clear=True):
    calls: dict[str, list] = {"launch": [], "stop": [], "backup": []}
    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(gl, "launch_gateway", lambda script: calls["launch"].append(script) or "proc")
    monkeypatch.setattr(gl, "wait_for_gateway_port", lambda *a, **k: _Port(port_open))
    monkeypatch.setattr(gl, "wait_for_tenant_clear", lambda caller: tenant_clear)
    monkeypatch.setattr(gl, "stop_gateway_tree_only", lambda proc, created_after=None: calls["stop"].append(proc))
    monkeypatch.setattr(gl, "_backup_after_run", lambda: calls["backup"].append(1))
    monkeypatch.setattr(gl, "GATEWAY_WARMUP_SECONDS", 0)
    monkeypatch.setattr(live_cli, "_run_once", lambda config, rehearse: 0)
    return calls


def _cfg(tmp_path) -> LiveConfig:
    script = tmp_path / "live.bat"
    script.write_text("rem")
    return LiveConfig("U1", "127.0.0.1", 4001, 17, str(script), armed=False, dry_run_requested=True)


def test_nightly_launches_runs_and_tears_down_only_its_own_gateway(quiet, monkeypatch, tmp_path):
    calls = _patch(monkeypatch, tmp_path)
    assert live_cli.run_live_nightly(_cfg(tmp_path), today=TRADING_DAY) == 0
    assert len(calls["launch"]) == 1 and calls["stop"] == ["proc"] and calls["backup"] == [1]


def test_nightly_refuses_when_the_port_never_opens(quiet, monkeypatch, tmp_path):
    calls = _patch(monkeypatch, tmp_path, port_open=False)
    assert live_cli.run_live_nightly(_cfg(tmp_path), today=TRADING_DAY) == 2
    assert calls["stop"] == ["proc"]


def test_nightly_refuses_while_a_paper_tenant_is_active(quiet, monkeypatch, tmp_path):
    calls = _patch(monkeypatch, tmp_path, tenant_clear=False)
    assert live_cli.run_live_nightly(_cfg(tmp_path), today=TRADING_DAY) == 2
    assert calls["launch"] == []


def test_nightly_on_a_holiday_runs_without_a_gateway(quiet, monkeypatch, tmp_path):
    calls = _patch(monkeypatch, tmp_path)
    assert live_cli.run_live_nightly(_cfg(tmp_path), today=datetime.date(2026, 10, 31)) == 0
    assert calls["launch"] == []


def test_nightly_refuses_when_the_live_gateway_lock_is_held(quiet, monkeypatch, tmp_path):
    from backend.run_lock import acquire_run_lock, release_run_lock

    _patch(monkeypatch, tmp_path)
    lock = acquire_run_lock("live_gateway")
    try:
        assert live_cli.run_live_nightly(_cfg(tmp_path), today=TRADING_DAY) == 2
    finally:
        release_run_lock(lock)


def test_grant_command_reports_refusals(monkeypatch):
    async def refuse(*a, **k):
        raise live_grant.GrantRefused("nope")

    async def no_init():
        return None

    monkeypatch.setattr("backend.database.init_db", no_init)
    monkeypatch.setattr(live_grant, "grant_stage1", refuse)
    args = SimpleNamespace(command="grant", book="B36", attest="x")
    assert asyncio.run(live_cli._grant_command(args)) == 2
