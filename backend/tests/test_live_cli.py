"""The live process's entry and CLI (#1065): env overlay loading, and the
refusals that happen before any broker call."""

import pytest

from backend import env as env_mod
from backend import live_cli, live_entry


def test_overlay_wins_over_base_env_and_missing_overlay_refuses(tmp_path, monkeypatch):
    base = tmp_path / ".env"
    base.write_text("IBKR_TRADING_MODE=paper\nIBKR_GATEWAY_PORT=4002\nONLY_BASE=1\n")
    overlay = tmp_path / ".env.live"
    overlay.write_text("IBKR_TRADING_MODE=live\nIBKR_GATEWAY_PORT=4001\n")
    monkeypatch.setattr(env_mod, "BASE_ENV_FILE", base)
    monkeypatch.setattr(env_mod, "REPO_ROOT", tmp_path)
    # setenv first so monkeypatch restores the real values afterwards —
    # load_dotenv writes os.environ directly.
    for name in ("IBKR_TRADING_MODE", "IBKR_GATEWAY_PORT", "ONLY_BASE"):
        monkeypatch.setenv(name, "placeholder")
    monkeypatch.setenv(env_mod.ENV_OVERLAY_VAR, ".env.live")
    env_mod.load_env()
    import os

    assert os.environ["IBKR_TRADING_MODE"] == "live"
    assert os.environ["IBKR_GATEWAY_PORT"] == "4001"
    assert os.environ["ONLY_BASE"] == "1"
    assert env_mod.base_env_values()["IBKR_GATEWAY_PORT"] == "4002"

    monkeypatch.setenv(env_mod.ENV_OVERLAY_VAR, "missing.env")
    with pytest.raises(RuntimeError, match="does not exist"):
        env_mod.load_env()


def test_no_overlay_is_the_plain_base_load(tmp_path, monkeypatch):
    base = tmp_path / ".env"
    base.write_text("SOME_BASE_VAR=x\n")
    monkeypatch.setattr(env_mod, "BASE_ENV_FILE", base)
    monkeypatch.delenv(env_mod.ENV_OVERLAY_VAR, raising=False)
    monkeypatch.setenv("SOME_BASE_VAR", "placeholder")
    env_mod.load_env()
    assert env_mod.overlay_path() is None
    import os

    assert os.environ["SOME_BASE_VAR"] == "x"
    monkeypatch.setattr(env_mod, "BASE_ENV_FILE", tmp_path / "nope")
    assert env_mod.base_env_values() == {}


def test_overlay_path_absolute(tmp_path, monkeypatch):
    monkeypatch.setenv(env_mod.ENV_OVERLAY_VAR, str(tmp_path / "x.env"))
    assert env_mod.overlay_path() == tmp_path / "x.env"


def test_entry_refuses_when_the_overlay_is_missing(monkeypatch, capsys):
    monkeypatch.setenv(env_mod.ENV_OVERLAY_VAR, "definitely-missing.env")
    assert live_entry.main(["run"]) == 2
    assert "LIVE NOT RUN" in capsys.readouterr().err


@pytest.fixture
def quiet(monkeypatch):
    alerts: list[tuple] = []
    monkeypatch.setattr(live_cli, "_alert", lambda *a, **k: alerts.append(a))
    monkeypatch.setattr("backend.run_logging.setup_run_logging", lambda name: None)
    return alerts


def test_dispatch_refuses_outside_live_mode(quiet, monkeypatch):
    monkeypatch.setattr(live_cli, "live_mode_env_ok", lambda: False)
    assert live_cli.dispatch(["run", "--dry-run"]) == 2
    assert quiet and "NOT RUN" in quiet[0][0]


def test_dispatch_rehearse_needs_dry_run(quiet, monkeypatch):
    monkeypatch.setattr(live_cli, "live_mode_env_ok", lambda: True)
    assert live_cli.dispatch(["run", "--rehearse"]) == 2


def test_dispatch_refuses_a_bad_environment_without_touching_a_broker(quiet, monkeypatch):
    monkeypatch.setattr(live_cli, "live_mode_env_ok", lambda: True)
    monkeypatch.setattr(live_cli, "overlay_path", lambda: None)
    called: list = []
    monkeypatch.setattr(live_cli, "_run_once", lambda *a, **k: called.append(a) or 0)
    assert live_cli.dispatch(["run"]) == 2
    assert called == []
    assert "overlay" in quiet[0][1]


def test_dispatch_runs_once_or_nightly(quiet, monkeypatch):
    monkeypatch.setattr(live_cli, "live_mode_env_ok", lambda: True)
    monkeypatch.setattr(live_cli, "resolve_live_config", lambda *a, **k: "cfg")
    monkeypatch.setattr(live_cli, "_run_once", lambda config, rehearse: 7)
    monkeypatch.setattr(live_cli, "run_live_nightly", lambda config: 9)
    assert live_cli.dispatch(["run", "--dry-run"]) == 7
    assert live_cli.dispatch(["run", "--nightly"]) == 9


def test_nightly_refuses_a_missing_start_script(quiet):
    from backend.live_executor import LiveConfig

    config = LiveConfig("U1", "127.0.0.1", 4001, 17, "Z:/nope/live.bat", armed=False, dry_run_requested=True)
    assert live_cli.run_live_nightly(config, today=__import__("datetime").date(2026, 10, 30)) == 2


def test_run_once_alerts_on_a_crash(quiet, monkeypatch):
    async def boom(config, rehearse):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(live_cli, "_execute", boom)
    assert live_cli._run_once("cfg", False) == 4
    assert "CRASHED" in quiet[0][0]
