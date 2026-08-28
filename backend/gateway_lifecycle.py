"""gateway_lifecycle.py — start-on-demand IB Gateway via IBC (#68, design §3.1).

The nightly job launches Gateway, uses it, and shuts it down — no 24/7
session, which sidesteps the weekly forced re-login, the paper-account
Auto-Restart token bug (IBC #345), and session drift. Resting GTC profit
targets live at IB's servers and keep working while Gateway is down.

Sequence:
1. Holiday guard — on a market holiday, run the executor directly (it
   writes its heartbeat and exits) without ever launching Gateway.
2. Tenancy wait (#838): if another Gateway tenant (preflight, fill check, a
   restore drill) is still active, wait bounded rather than launch a second
   Gateway into a clientId collision; abort loud if it never clears.
3. Launch IBC's StartGateway.bat (IBC_START_SCRIPT). The bot's paper
   credentials live ONLY in the local IBC config.ini the operator wrote
   with scripts/setup-ibc.ps1 — never in this repo or its environment.
4. Poll the API port until it accepts a TCP connection (bounded); on
   timeout, push an urgent ntfy alert and abort — the executor never runs
   half-connected.
5. Run the executor pipeline (its own broker open performs the real API
   handshake, with its own audited failure path).
6. Kill the Gateway process tree, always.

Keep Gateway's built-in Auto-Restart OFF in this model.
"""

import datetime
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

PORT_POLL_TIMEOUT_SECONDS = 180
PORT_POLL_INTERVAL_SECONDS = 5
# Gateway paints the login window before the API listens; give it a head
# start so the first poll isn't wasted.
GATEWAY_WARMUP_SECONDS = 15

# #838: bounded wait for another Gateway tenant to clear before THIS run
# launches its own Gateway. A preflight that started at 14:00 with
# -StartWhenAvailable -WakeToRun can still be mid-rehearsal at 18:45 (a
# machine asleep at 14:00 runs it on wake, possibly minutes before the
# nightly launch); a bounded wait absorbs that ordinary overlap without
# either process guessing at the other's remaining runtime. If the other
# tenant is STILL live at the deadline, the caller must abort loud rather
# than launch a second Gateway into a clientId collision.
TENANT_WAIT_TIMEOUT_SECONDS = 5 * 60
TENANT_WAIT_INTERVAL_SECONDS = 15

GATEWAY_CMDLINE_PATTERN = re.compile(r"IBC|StartGateway|ibgateway|Jts", re.IGNORECASE)


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    cmdline: str
    created_at: float  # Unix timestamp in seconds


def matches_gateway_cmdline(cmdline: str | None) -> bool:
    """True if the command line references IBC, StartGateway, ibgateway, or Jts."""
    if not cmdline:
        return False
    return bool(GATEWAY_CMDLINE_PATTERN.search(cmdline))


def _enumerate_processes_windows(run: Callable[..., Any] = subprocess.run) -> list[ProcessInfo]:
    """Enumerate processes on Windows using PowerShell / CIM."""
    ps_script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId, Name, CommandLine, "
        "@{N='Created';E={if ($_.CreationDate) { ([DateTimeOffset]$_.CreationDate).ToUnixTimeMilliseconds() / 1000.0 } else { 0 }}} | "
        "ConvertTo-Json -Compress"
    )
    result = run(["powershell", "-NoProfile", "-Command", ps_script], capture_output=True, text=True, check=False)
    if result.returncode != 0 or not result.stdout or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            data = [data]
        processes = []
        for item in data:
            pid = item.get("ProcessId")
            if pid is None:
                continue
            name = item.get("Name") or ""
            cmdline = item.get("CommandLine") or ""
            created = float(item.get("Created") or 0.0)
            processes.append(ProcessInfo(pid=int(pid), name=name, cmdline=cmdline, created_at=created))
        return processes
    except Exception as exc:
        logger.warning("Failed to parse process list from PowerShell: %s", exc)
        return []


def find_detached_gateway_processes(
    created_after: float,
    enumerate_processes: Callable[[], list[ProcessInfo]] = _enumerate_processes_windows,
) -> list[ProcessInfo]:
    """Find processes created at or after *created_after* whose command line
    references IBC, StartGateway, ibgateway, or the Jts install path."""
    candidates = enumerate_processes()
    matching = []
    for proc in candidates:
        if proc.created_at >= created_after and matches_gateway_cmdline(proc.cmdline):
            matching.append(proc)
    return matching


def kill_detached_gateway_processes(
    created_after: float,
    enumerate_processes: Callable[[], list[ProcessInfo]] = _enumerate_processes_windows,
    run: Callable[..., Any] = subprocess.run,
    now: Callable[[], float] = time.time,
) -> list[ProcessInfo]:
    """Enumerate and kill processes matching IBC/Gateway created at or after *created_after* (#851)."""
    matched = find_detached_gateway_processes(created_after, enumerate_processes=enumerate_processes)
    killed = []
    current_time = now()
    for proc in matched:
        if proc.pid == os.getpid():
            continue
        age = max(0.0, current_time - proc.created_at) if proc.created_at > 0 else 0.0
        logger.info(
            "Killed detached Gateway/IBC process %s (PID %d, age %.1fs): %s",
            proc.name,
            proc.pid,
            age,
            proc.cmdline,
        )
        run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        killed.append(proc)
    return killed


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


def wait_for_tenant_clear(
    caller: str,
    timeout_seconds: float = TENANT_WAIT_TIMEOUT_SECONDS,
    interval_seconds: float = TENANT_WAIT_INTERVAL_SECONDS,
    tenant_active=None,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> bool:
    """True once no OTHER Gateway tenant (run_lock.other_gateway_tenant_active)
    is active — checked immediately, then polled up to timeout_seconds if one
    is. False when a tenant is still active at the deadline (#838): the
    launch-time symmetric half of the teardown-time deferral every launcher
    already had — a preflight or fill check mid-run must not be launched
    into a second Gateway, exactly as their own teardowns already defer to a
    live nightly run."""
    from backend.run_lock import other_gateway_tenant_active

    tenant_active = tenant_active or other_gateway_tenant_active
    deadline = monotonic() + timeout_seconds
    while True:
        if not tenant_active(caller):
            return True
        if monotonic() >= deadline:
            return False
        sleep(interval_seconds)


def launch_gateway(start_script: str) -> subprocess.Popen:
    """Start IBC's Gateway script detached-ish; the caller owns teardown."""
    logger.info("Launching IB Gateway via %s", start_script)
    return subprocess.Popen(
        ["cmd", "/c", start_script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def stop_gateway(
    proc: subprocess.Popen | None = None,
    created_after: float | None = None,
    run: Callable[..., Any] = subprocess.run,
    enumerate_processes: Callable[[], list[ProcessInfo]] = _enumerate_processes_windows,
    now: Callable[[], float] = time.time,
) -> list[ProcessInfo]:
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
    if proc is not None:
        run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    sweep = (
        "Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | "
        "Where-Object { $_.CommandLine -match 'ibgateway' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    run(["powershell", "-NoProfile", "-Command", sweep], capture_output=True, check=False)
    if created_after is not None:
        return kill_detached_gateway_processes(
            created_after=created_after,
            enumerate_processes=enumerate_processes,
            run=run,
            now=now,
        )
    return []


def stop_gateway_tree_only(
    proc: subprocess.Popen | None = None,
    created_after: float | None = None,
    run: Callable[..., Any] = subprocess.run,
    enumerate_processes: Callable[[], list[ProcessInfo]] = _enumerate_processes_windows,
    now: Callable[[], float] = time.time,
) -> list[ProcessInfo]:
    """Kill only the process tree taskkill can walk from *proc*, AND kill any
    detached processes matching IBC/StartGateway/Jts/ibgateway created at or
    after *created_after* (#838, #851).

    stop_gateway's sweep matches ANY java.exe on the box whose command line
    references ibgateway — right for the run-of-record teardowns (leaking a
    Gateway nightly, #224, is worse there), wrong for preflight: preflight's
    teardown re-checks tenancy immediately beforehand, but the check and the
    kill are still two syscalls apart, and the sweep's blast radius covers a
    Gateway some OTHER tenant launched in that gap just as readily as an
    orphan of this run's own launch. Preflight is a rehearsal, not the run
    of record — an occasional Gateway left running until the next teardown
    reaches it is a far smaller cost than killing a live tenant's Gateway
    out from under it.

    However, StartGateway.bat detaches its real work via `start`, which
    re-parents the IBC launcher and Gateway processes outside *proc*'s tree
    (#851). When *created_after* is provided, we enumerate and kill processes
    whose command line references IBC, StartGateway, ibgateway, or Jts AND
    whose creation time is >= *created_after*, guaranteeing we clean up
    detached orphans from this launch attempt without touching any pre-existing
    processes from other tenants or operators."""
    if proc is not None:
        run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    if created_after is not None:
        return kill_detached_gateway_processes(
            created_after=created_after,
            enumerate_processes=enumerate_processes,
            run=run,
            now=now,
        )
    return []


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
    from backend.run_lock import acquire_run_lock, other_gateway_tenant_active, release_run_lock

    gateway_lock = acquire_run_lock("gateway")
    if gateway_lock is None:
        # #546 F9: a scheduler/tenancy condition, not a code crash — matches
        # every other "NOT RUN" alert in this function.
        _urgent(
            "basis executor NOT RUN",
            "gateway tenancy lock held — another nightly run is mid-window; not launching a second Gateway",
            event_type="SCHEDULER_ALERT",
        )
        return 5
    # #547: launch_gateway sits INSIDE the try so a Popen raise (AV,
    # permissions) still hits the finally below and releases the lock —
    # previously that leaked the lock until the 2h staleness break, aborting
    # a same-window retry with "NOT RUN". proc starts None so teardown has
    # something defined to check even when Popen itself never returned.
    proc = None
    try:
        # #838: the tenancy LOCK guards against a second nightly run, but
        # says nothing about a preflight, fill check, or restore drill still
        # mid-window on the shared Gateway — a preflight's own launch-time
        # check is symmetric (skip-clean, #827) but this run is the one that
        # must not collide, so it waits instead of skipping. A bounded wait
        # absorbs the ordinary overlap (a preflight woken late by
        # -StartWhenAvailable -WakeToRun can still be running minutes before
        # 18:45); if the other tenant is still live at the deadline, a loud
        # NOT-RUN beats launching a second Gateway into a clientId collision.
        if not wait_for_tenant_clear("gateway"):
            _urgent(
                "basis executor NOT RUN",
                f"another Gateway tenant was still active after a {TENANT_WAIT_TIMEOUT_SECONDS}s wait — "
                "refusing to launch a second Gateway",
                event_type="EXECUTOR_ABORTED_TENANT_ACTIVE",
            )
            return 6
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
        return 0
    finally:
        # #548 LOW-2: backup moved into finally — it used to run only on the
        # clean-exit path, so a crash night (some phases committed, code !=
        # 0 from _run_executor_alerting_on_crash) took NO backup. Repeated
        # crash nights then left the newest restore point arbitrarily old,
        # on exactly the nights most likely to need one. alert-don't-raise
        # (#207, _backup_after_run) already makes this safe unconditionally.
        _backup_after_run()
        # Symmetric guard (#471): stop_gateway kills EVERY ibgateway java
        # process — a fill check mid-fetch, or a restore drill mid-query
        # (#641/#681), on the shared Gateway would otherwise die/lose its
        # evidence. Any other tenant's lock marks its tenancy exactly like
        # ours marks this run's — checked against run_lock.GATEWAY_TENANT_LOCKS
        # as a whole, not a hand-spelled subset that predates newer tenants.
        if other_gateway_tenant_active("gateway"):
            logger.warning("Another Gateway tenant is active — leaving Gateway up (#471/#681)")
        elif proc is not None:
            stop_gateway(proc)
        release_run_lock(gateway_lock)


if __name__ == "__main__":
    from backend.run_logging import setup_run_logging

    setup_run_logging("gateway_lifecycle")
    sys.exit(run_nightly())
