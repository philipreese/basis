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
import logging
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

PORT_POLL_TIMEOUT_SECONDS = 180
PORT_POLL_INTERVAL_SECONDS = 5
# Gateway paints the login window before the API listens; give it a head
# start so the first poll isn't wasted.
GATEWAY_WARMUP_SECONDS = 15

# #838: bounded wait for another Gateway tenant to clear before THIS run
# launches its own Gateway.
TENANT_WAIT_TIMEOUT_SECONDS = 5 * 60
TENANT_WAIT_INTERVAL_SECONDS = 15

# #852: distinguish "machine slow / memory pressure" from "gateway broken / credentials".
PORT_POLL_GRACE_TIMEOUT_SECONDS = 120
MEMORY_PRESSURE_THRESHOLD_GB = 1.5
LOG_PROGRESSION_WINDOW_SECONDS = 30.0
DEFAULT_IBC_LOG_DIR = os.getenv("IBC_LOG_DIR", "C:\\IBC\\Logs")


class GatewayPortStatus(str, Enum):
    OPEN = "OPEN"
    OPEN_SLOW = "OPEN_SLOW"
    TIMEOUT_PROGRESSING = "TIMEOUT_PROGRESSING"
    TIMEOUT_DEAD_OR_STALLED = "TIMEOUT_DEAD_OR_STALLED"


@dataclass(frozen=True)
class GatewayPortResult:
    status: GatewayPortStatus
    elapsed_seconds: float
    free_memory_gb: float | None = None
    memory_under_pressure: bool = False

    @property
    def is_open(self) -> bool:
        return self.status in (GatewayPortStatus.OPEN, GatewayPortStatus.OPEN_SLOW)


def get_free_memory_gb() -> float | None:
    """Sample available physical memory in GB (one call, no polling).

    None when the platform read fails — an unknown reading stays absent (no
    pressure claim, no number in a finding), never a fabricated value."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return round(stat.ullAvailPhys / (1024**3), 2)
        except Exception as exc:
            logger.debug("Failed to read memory status via ctypes: %s", exc)
    elif hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_AVPHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return round((pages * page_size) / (1024**3), 2)
        except Exception:
            pass
    return None


def get_latest_ibc_log_mtime(
    log_dir: str | os.PathLike | None = None,
    start_script: str | None = None,
) -> float | None:
    """Return newest modification timestamp (epoch) of any log file in IBC log dir, or None."""
    d = log_dir or os.getenv("IBC_LOG_DIR")
    if not d:
        script = start_script or os.getenv("IBC_START_SCRIPT", "")
        if script:
            candidate = os.path.join(os.path.dirname(script), "Logs")
            if os.path.exists(candidate):
                d = candidate
    d = d or DEFAULT_IBC_LOG_DIR
    if not os.path.exists(d) or not os.path.isdir(d):
        return None
    latest: float | None = None
    try:
        with os.scandir(d) as entries:
            for entry in entries:
                if entry.is_file():
                    try:
                        mtime = entry.stat().st_mtime
                        if latest is None or mtime > latest:
                            latest = mtime
                    except OSError:
                        pass
    except OSError:
        return None
    return latest


def is_proc_alive(proc: Any) -> bool:
    """True if proc exists and has not exited."""
    if proc is None:
        return False
    if callable(getattr(proc, "poll", None)):
        return proc.poll() is None
    return getattr(proc, "returncode", None) is None


def is_gateway_progressing(
    proc: Any = None,
    log_dir: str | os.PathLike | None = None,
    start_script: str | None = None,
    window_seconds: float = LOG_PROGRESSION_WINDOW_SECONDS,
    time_fn: Callable[[], float] = time.time,
    latest_mtime_fn: Callable[..., float | None] = get_latest_ibc_log_mtime,
) -> bool:
    """True if proc is alive AND an IBC log file was modified within window_seconds."""
    if proc is not None and not is_proc_alive(proc):
        return False
    mtime = latest_mtime_fn(log_dir=log_dir, start_script=start_script)
    if mtime is None:
        return False
    now = time_fn()
    return (now - mtime) <= window_seconds


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


def wait_for_gateway_port(
    host: str,
    port: int,
    proc: Any = None,
    timeout_seconds: float = PORT_POLL_TIMEOUT_SECONDS,
    grace_timeout_seconds: float = PORT_POLL_GRACE_TIMEOUT_SECONDS,
    interval_seconds: float = PORT_POLL_INTERVAL_SECONDS,
    free_memory_gb: float | None = None,
    memory_threshold_gb: float = MEMORY_PRESSURE_THRESHOLD_GB,
    is_progressing_fn: Callable[..., bool] | None = None,
    connect_fn: Callable[[str, int], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> GatewayPortResult:
    """True once a TCP connect succeeds, handling slow-machine grace re-probe (#852).

    1. Polls for up to timeout_seconds (180s).
    2. At timeout, checks if proc is alive and progressing.
    3. If alive and progressing, polls for up to grace_timeout_seconds (120s) more.
    """
    if free_memory_gb is None:
        free_memory_gb = get_free_memory_gb()
    # An unknown reading claims nothing: pressure requires an actual number.
    memory_under_pressure = free_memory_gb is not None and free_memory_gb < memory_threshold_gb

    def _can_connect() -> bool:
        if connect_fn is not None:
            return connect_fn(host, port)
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            return False

    start_time = monotonic()
    deadline = start_time + timeout_seconds

    while monotonic() < deadline:
        if _can_connect():
            return GatewayPortResult(
                status=GatewayPortStatus.OPEN,
                elapsed_seconds=monotonic() - start_time,
                free_memory_gb=free_memory_gb,
                memory_under_pressure=memory_under_pressure,
            )
        sleep(interval_seconds)

    # Initial window expired. Check if alive and progressing.
    progressing_check = is_progressing_fn or is_gateway_progressing
    if not progressing_check(proc=proc):
        return GatewayPortResult(
            status=GatewayPortStatus.TIMEOUT_DEAD_OR_STALLED,
            elapsed_seconds=monotonic() - start_time,
            free_memory_gb=free_memory_gb,
            memory_under_pressure=memory_under_pressure,
        )

    # Alive and progressing -> grace window re-probe
    logger.info(
        "IB Gateway port not open after %ds, but process is alive and progressing; entering %ds grace window",
        int(timeout_seconds),
        int(grace_timeout_seconds),
    )
    grace_deadline = monotonic() + grace_timeout_seconds
    while monotonic() < grace_deadline:
        if _can_connect():
            return GatewayPortResult(
                status=GatewayPortStatus.OPEN_SLOW,
                elapsed_seconds=monotonic() - start_time,
                free_memory_gb=free_memory_gb,
                memory_under_pressure=memory_under_pressure,
            )
        sleep(interval_seconds)

    return GatewayPortResult(
        status=GatewayPortStatus.TIMEOUT_PROGRESSING,
        elapsed_seconds=monotonic() - start_time,
        free_memory_gb=free_memory_gb,
        memory_under_pressure=memory_under_pressure,
    )


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


def stop_gateway_tree_only(proc: subprocess.Popen, run=subprocess.run) -> None:
    """Kill only the process tree taskkill can walk from *proc* — no
    ibgateway-wide sweep (#838).

    stop_gateway's sweep matches ANY java.exe on the box whose command line
    references ibgateway — right for the run-of-record teardowns (leaking a
    Gateway nightly, #224, is worse there), wrong for preflight: preflight's
    teardown re-checks tenancy immediately beforehand, but the check and the
    kill are still two syscalls apart, and the sweep's blast radius covers a
    Gateway some OTHER tenant launched in that gap just as readily as an
    orphan of this run's own launch. Preflight is a rehearsal, not the run
    of record — an occasional Gateway left running until the next teardown
    reaches it is a far smaller cost than killing a live tenant's Gateway
    out from under it. Note this taskkill /T tree-kill can itself miss the
    orphaned java process (the same reason stop_gateway's sweep exists,
    #224) — if that shows up in practice as a Gateway still live at the
    next preflight or nightly launch, the fix is recording the actual
    Gateway PID at launch time and killing that recorded PID here, not
    widening this back into a sweep."""
    run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)


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
        free_memory_gb = get_free_memory_gb()
        proc = launch_gateway(start_script)
        time.sleep(GATEWAY_WARMUP_SECONDS)
        port_res = wait_for_gateway_port(
            host,
            port,
            proc=proc,
            free_memory_gb=free_memory_gb,
            connect_fn=lambda h, p: wait_for_port(h, p, timeout_seconds=0),
        )
        if not port_res.is_open:
            if port_res.memory_under_pressure:
                free_str = f"{port_res.free_memory_gb:.1f}" if port_res.free_memory_gb is not None else "low"
                mem_msg = (
                    f" — machine under memory pressure ({free_str} GB free); gateway may be slow rather than broken"
                )
                action_msg = "free memory or wait for machine load to clear before tonight's run; retry"
            elif port_res.status == GatewayPortStatus.TIMEOUT_PROGRESSING:
                mem_msg = " (process alive and progressing)"
                action_msg = "gateway startup is slow but progressing — check system load or retry"
            else:
                mem_msg = " (process dead or stalled)"
                action_msg = "check IBC config/login (2FA prompt? bad credentials?)"

            _urgent(
                "basis executor NOT RUN",
                f"IB Gateway API port {host}:{port} never opened within {PORT_POLL_TIMEOUT_SECONDS}s{mem_msg} — "
                f"{action_msg}",
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
