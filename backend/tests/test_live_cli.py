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
    monkeypatch.setattr("backend.run_logging.secure_live_logging", lambda secrets: None)
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


def test_dispatch_check_is_always_dry_and_routes_to_the_check(quiet, monkeypatch):
    monkeypatch.setattr(live_cli, "live_mode_env_ok", lambda: True)
    seen: dict = {}

    def fake_resolve(*a, **k):
        seen.update(k)
        return "cfg"

    monkeypatch.setattr(live_cli, "resolve_live_config", fake_resolve)
    monkeypatch.setattr(live_cli, "check_live_gateway", lambda config: 5)
    assert live_cli.dispatch(["check"]) == 5
    assert seen["dry_run"] is True and "paper_view_of_overlay" in seen


def test_live_overlay_values_reads_the_file_without_loading_it(tmp_path, monkeypatch):
    import os

    overlay = tmp_path / ".env.live"
    monkeypatch.setattr(env_mod, "LIVE_OVERLAY_FILE", overlay)
    assert env_mod.live_overlay_values() == {}
    overlay.write_text("IBC_LIVE_INI=C:/IBC/live/config.ini\n")
    monkeypatch.delenv("IBC_LIVE_INI", raising=False)
    assert env_mod.live_overlay_values() == {"IBC_LIVE_INI": "C:/IBC/live/config.ini"}
    assert "IBC_LIVE_INI" not in os.environ


def test_run_once_alerts_on_a_crash(quiet, monkeypatch):
    async def boom(config, rehearse):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(live_cli, "_execute", boom)
    assert live_cli._run_once("cfg", False) == 4
    assert "CRASHED" in quiet[0][0]


# ---------------------------------------------------------------------------
# #1101: the arm token comes from the overlay file only
# ---------------------------------------------------------------------------


def test_load_env_records_what_was_set_before_any_file(tmp_path, monkeypatch):
    base = tmp_path / ".env"
    base.write_text("FROM_BASE=1\n")
    overlay = tmp_path / ".env.live"
    overlay.write_text("IBKR_LIVE_ARM=TRANSMIT\n")
    monkeypatch.setattr(env_mod, "BASE_ENV_FILE", base)
    monkeypatch.setattr(env_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(env_mod, "_names_before_load", None)
    monkeypatch.setenv(env_mod.ENV_OVERLAY_VAR, ".env.live")
    monkeypatch.setenv("IBKR_LIVE_ARM", "TRANSMIT")  # as if set in Windows
    monkeypatch.setenv("FROM_BASE", "placeholder")
    monkeypatch.delenv("FROM_BASE")
    assert env_mod.set_before_load("IBKR_LIVE_ARM") is None  # load_env has not run: unprovable
    env_mod.load_env()
    assert env_mod.set_before_load("IBKR_LIVE_ARM") is True
    assert env_mod.set_before_load("FROM_BASE") is False
    assert env_mod.overlay_values() == {"IBKR_LIVE_ARM": "TRANSMIT"}
    monkeypatch.setenv(env_mod.ENV_OVERLAY_VAR, "missing.env")
    assert env_mod.overlay_values() == {}
    monkeypatch.delenv(env_mod.ENV_OVERLAY_VAR)
    assert env_mod.overlay_values() == {}


def test_dispatch_reads_the_arm_token_from_the_overlay_file_only(quiet, monkeypatch):
    monkeypatch.setattr(live_cli, "live_mode_env_ok", lambda: True)
    monkeypatch.setattr(live_cli, "overlay_values", lambda: {"IBKR_LIVE_ARM": "TRANSMIT"})
    monkeypatch.setattr(live_cli, "set_before_load", lambda name: name == "IBKR_LIVE_ARM")
    seen: dict = {}

    def fake_resolve(*a, **k):
        seen.update(k)
        return "cfg"

    monkeypatch.setattr(live_cli, "resolve_live_config", fake_resolve)
    monkeypatch.setattr(live_cli, "_run_once", lambda config, rehearse: 0)
    assert live_cli.dispatch(["run"]) == 0
    assert seen["overlay_values"] == {"IBKR_LIVE_ARM": "TRANSMIT"}
    assert seen["arm_set_before_load"] is True


# ---------------------------------------------------------------------------
# #1101: the live account id never reaches a log
# ---------------------------------------------------------------------------


def test_redacting_filter_scrubs_message_args_and_traceback():
    import io
    import logging

    from backend.run_logging import REDACTED, RedactingFilter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
    handler.addFilter(RedactingFilter(["U0000000", ""]))
    log = logging.getLogger("ib_async.wrapper.redaction-test")
    log.propagate = False
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("position: Position(account='%s', contract=Stock('SCHB'))", "U0000000")
        log.warning("openOrder account=U0000000 %s", 5, stack_info=False)
        try:
            raise RuntimeError("execDetails account=U0000000")
        except RuntimeError:
            log.exception("failed for U0000000")
        bad_format = "bad format U0000000 %s %s"
        log.info(bad_format, "only-one-arg")  # a malformed %-format still logs, redacted
    finally:
        log.removeHandler(handler)
    text = stream.getvalue()
    assert "U0000000" not in text
    assert text.count(REDACTED) >= 4
    assert "execDetails" in text and "Traceback" in text


def test_secure_live_logging_quiets_ib_async_and_filters_every_handler(tmp_path):
    import io
    import logging

    from backend.run_logging import secure_live_logging

    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root.addHandler(handler)
    ib_logger = logging.getLogger("ib_async")
    old_level, old_root_level = ib_logger.level, root.level
    root.setLevel(logging.INFO)
    try:
        secure_live_logging(["U0000000"])
        assert ib_logger.level == logging.WARNING
        logging.getLogger("ib_async.wrapper").info("Position account=U0000000 — INFO is dropped now")
        logging.getLogger("ib_async.wrapper").warning("orderStatus account=U0000000")
        logging.getLogger("backend.somewhere").warning("an id slipped in: U0000000")
    finally:
        root.removeHandler(handler)
        ib_logger.setLevel(old_level)
        root.setLevel(old_root_level)
    text = stream.getvalue()
    assert "U0000000" not in text
    assert "INFO is dropped" not in text
    assert "orderStatus account=[redacted]" in text and "slipped in: [redacted]" in text


def test_dispatch_secures_logging_with_the_live_account_id(monkeypatch):
    calls: list = []
    monkeypatch.setattr(live_cli, "_alert", lambda *a, **k: None)
    monkeypatch.setattr("backend.run_logging.setup_run_logging", lambda name: None)
    monkeypatch.setattr("backend.run_logging.secure_live_logging", lambda secrets: calls.append(secrets))
    monkeypatch.setattr(live_cli, "live_mode_env_ok", lambda: False)
    monkeypatch.setenv("IBKR_LIVE_ACCOUNT_ID", " U0000000 ")
    live_cli.dispatch(["run", "--dry-run"])
    assert calls == [["U0000000"]]
