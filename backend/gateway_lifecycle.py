"""gateway_lifecycle.py — start-on-demand IB Gateway via IBC (#68, design §3.1).

The nightly job launches Gateway, uses it, and shuts it down — no 24/7
session, which sidesteps the weekly forced re-login, the paper-account
Auto-Restart token bug (IBC #345), and session drift. Resting GTC profit
targets live at IB's servers and keep working while Gateway is down.

Sequence:
1. Holiday guard — on a market holiday, run the executor directly (it
   writes its heartbeat and exits) without ever launching Gateway.
2. Launch IBC's StartGateway.bat (IBC_START_SCRIPT). The bot's paper
   credentials live ONLY in the local IBC config.ini the operator wrote
   with scripts/setup-ibc.ps1 — never in this repo or its environment.
3. Poll the API port until it accepts a TCP connection (bounded); on
   timeout, push an urgent ntfy alert and abort — the executor never runs
   half-connected.
4. Run the executor pipeline (its own broker open performs the real API
   handshake, with its own audited failure path).
5. Kill the Gateway process tree, always.

Keep Gateway's built-in Auto-Restart OFF in this model.
"""

import datetime
import logging
import os
import socket
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

PORT_POLL_TIMEOUT_SECONDS = 180
PORT_POLL_INTERVAL_SECONDS = 5
# Gateway paints the login window before the API listens; give it a head
# start so the first poll isn't wasted.
GATEWAY_WARMUP_SECONDS = 15


def _gateway_endpoint() -> tuple[str, int]:
    return os.getenv("IBKR_GATEWAY_HOST", "127.0.0.1"), int(os.getenv("IBKR_GATEWAY_PORT", "4002"))


def wait_for_port(
    host: str,
    port: int,
    timeout_seconds: float = PORT_POLL_TIMEOUT_SECONDS,
    interval_seconds: float = PORT_POLL_INTERVAL_SECONDS,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> bool:
    """True once a TCP connect to (host, port) succeeds within the window."""
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            sleep(interval_seconds)
    return False


def launch_gateway(start_script: str) -> subprocess.Popen:
    """Start IBC's Gateway script detached-ish; the caller owns teardown."""
    logger.info("Launching IB Gateway via %s", start_script)
    return subprocess.Popen(
        ["cmd", "/c", start_script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def stop_gateway(proc: subprocess.Popen, run=subprocess.run) -> None:
    """Kill the whole Gateway process tree. IBC has no Windows stop script;
    killing the tree started by StartGateway.bat is the documented pattern.
    Resting GTC orders are server-side, so a hard kill loses nothing.

    Always attempt the tree kill — IBC's bat chain spawns the Gateway java
    process and exits, so by teardown the launcher is usually dead and an
    early return would leak the Gateway every night (#224). taskkill on a
    finished tree fails harmlessly. Then sweep for Gateway java processes
    the tree kill missed (the launcher's exit orphans them out of the tree):
    only java whose command line references the ibgateway install — never a
    blanket java.exe kill."""
    run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    sweep = (
        "Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | "
        "Where-Object { $_.CommandLine -match 'ibgateway' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    run(["powershell", "-NoProfile", "-Command", sweep], capture_output=True, check=False)


def _urgent(title: str, body: str, event_type: str = "CRASH_ALERT") -> None:
    # Durable crash alert (#417): audit row + ntfy-with-retry — a bare
    # send_ntfy was silent exactly when the crash was a network problem.
    from backend.operator import alert_crash

    alert_crash(title, body, "urgent", event_type=event_type)


def _backup_after_run() -> None:
    """Copy the database after the executor finishes (#207). A failed backup
    must never fail the run — but it must be heard, so it alerts instead."""
    from backend.db_backup import backup_database
    from backend.operator import alert_crash

    try:
        backup_database()
    except Exception as exc:
        logger.warning("Database backup failed: %s", exc)
        # #472: a failed backup step is a scheduler/environment condition
        # (disk full, file locked), not a code crash.
        alert_crash(
            "basis: DB backup FAILED", f"Nightly database backup failed: {exc}", "high", event_type="SCHEDULER_ALERT"
        )


def _run_executor_alerting_on_crash(executor_main) -> int:
    """Audit II (#341): an unexpected executor crash used to escape as a bare
    traceback in a scheduled task nobody watches — and the executor's old
    finally-block heartbeat made the watchdog call the night healthy. The
    heartbeat is now withheld on crash (executor.py), and this alert is the
    loud half: crash night → urgent ntfy + stale heartbeat at 22:00."""
    import asyncio

    try:
        asyncio.run(executor_main())
        return 0
    except Exception as exc:
        logger.exception("Executor crashed")
        _urgent("basis executor CRASHED", f"{type(exc).__name__}: {exc}")
        return 4


def run_nightly(today: datetime.date | None = None) -> int:
    """The Scheduled Task entry point. Returns a process exit code."""
    from backend.calendars import is_trading_day
    from backend.dates import market_today
    from backend.executor import main as executor_main

    # Market date, not UTC (#259): after 19:00 ET in EST the UTC date is
    # tomorrow — a late-started Friday run would think it was Saturday and
    # silently skip a live trading evening with a healthy heartbeat.
    today = today or market_today()

    if not is_trading_day(today):
        # No Gateway on holidays; the executor's own guard writes the
        # heartbeat and exits, so the watchdog stays quiet.
        logger.info("Market holiday %s — running executor for its heartbeat only", today.isoformat())
        return _run_executor_alerting_on_crash(executor_main)

    start_script = os.getenv("IBC_START_SCRIPT", "")
    if not start_script or not os.path.exists(start_script):
        # #472: a missing/misconfigured start script is a scheduler/config
        # condition, not a code crash.
        _urgent(
            "basis executor NOT RUN",
            f"IBC_START_SCRIPT missing or not found ({start_script or 'unset'}) — run scripts/setup-ibc.ps1",
            event_type="SCHEDULER_ALERT",
        )
        return 2

    host, port = _gateway_endpoint()
    # Tenancy BEFORE launch (#471, Audit II R3): the executor's own run lock
    # is only taken deep inside executor_main — after Gateway launch, warmup
    # sleep, port poll and init_db. Inside that multi-second window
    # fill_check's teardown sees no fresh lock and kills the Gateway this
    # run just launched (worse: this run's wait_for_port can latch onto
    # fill_check's Gateway on the same port, then lose it mid-run). The
    # gateway lock brackets the WHOLE window, launch through teardown.
    from backend.run_lock import acquire_run_lock, lock_is_held, release_run_lock

    gateway_lock = acquire_run_lock("gateway")
    if gateway_lock is None:
        _urgent(
            "basis executor NOT RUN",
            "gateway tenancy lock held — another nightly run is mid-window; not launching a second Gateway",
        )
        return 5
    # #547: launch_gateway sits INSIDE the try so a Popen raise (AV,
    # permissions) still hits the finally below and releases the lock —
    # previously that leaked the lock until the 2h staleness break, aborting
    # a same-window retry with "NOT RUN". proc starts None so teardown has
    # something defined to check even when Popen itself never returned.
    proc = None
    try:
        proc = launch_gateway(start_script)
        time.sleep(GATEWAY_WARMUP_SECONDS)
        if not wait_for_port(host, port):
            _urgent(
                "basis executor NOT RUN",
                f"IB Gateway API port {host}:{port} never opened within {PORT_POLL_TIMEOUT_SECONDS}s — "
                "check IBC config/login (2FA prompt? bad credentials?)",
                event_type="SCHEDULER_ALERT",
            )
            return 3
        code = _run_executor_alerting_on_crash(executor_main)
        if code != 0:
            return code
        _backup_after_run()
        return 0
    finally:
        # Symmetric guard (#471): stop_gateway kills EVERY ibgateway java
        # process — a fill check mid-fetch on the shared Gateway would die
        # with a false CRASHED alert and a lost fill push. Its lock marks
        # its tenancy exactly like ours marks this run's.
        if lock_is_held("fill_check"):
            logger.warning("fill_check lock held — leaving Gateway up for the running fill check (#471)")
        elif proc is not None:
            stop_gateway(proc)
        release_run_lock(gateway_lock)


if __name__ == "__main__":
    from backend.run_logging import setup_run_logging

    setup_run_logging("gateway_lifecycle")
    sys.exit(run_nightly())
