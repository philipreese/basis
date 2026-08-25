"""preflight.py — report-only afternoon rehearsal of the broker machinery (#827).

The system's only full rehearsal of its broker machinery is the nightly
18:45 run — one reveal per day, so every failure costs a full day (the 8/24
disclaimer wall, Error 10141, and the 8/25 whatIfOrder crash + drift halt
were each discovered only at 18:45). This 14:00 pass walks the same paths
with NO orders, while the operator is awake to act before the real run:

1. Gateway + session: launch IB Gateway and open the broker session exactly
   the way the executor does (gateway_lifecycle + BrokerSession — the #785
   connect retry and the #823/#824 needs-a-human classification included).
2. Reconciliation, read-only: the executor's own broker-vs-books comparison
   (reconciliation.compare_books) — never a reconciliation_runs row, never a
   halt, never a drift audit event.
3. Preview probe: a near-the-money XSP put vertical priced from live quotes,
   run through broker.preview_spread. PreviewRejectedError is itself a
   reportable finding, distinct from an unexpected exception.
4. Control/heartbeat state: any non-ACTIVE trading-control scope, the HALT
   sentinel, and executor-heartbeat staleness.
5. Teardown: always kill the Gateway tree (deferring to any other Gateway
   tenant, #471/#681), in a finally.

Each step is independently guarded — an exception becomes a finding, never
a crash — and the run ends in ONE ntfy push to the digest topic (ASCII-only
title, #598) listing every finding with its plain-language action.

Charter: preflight NEVER places or cancels orders, never mutates books,
positions, or control state, never writes reconciliation_runs, and never
writes the executor heartbeat (the 22:00 watchdog must not be pacified by a
rehearsal). Its one permitted database write is the PREFLIGHT_RUN audit
event. It takes its own "preflight" Gateway-tenant lock (run_lock.py) so a
preflight and the executor never share — or tear down — each other's
Gateway; if any other tenant is live at the start, preflight skips cleanly.
"""

import asyncio
import datetime
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from backend.broker import (
    NEEDS_HUMAN_BROKER_ERRORS,
    BrokerError,
    BrokerSession,
    PreviewRejectedError,
    SpreadOrder,
)
from backend.calendars import is_trading_day
from backend.console import _is_stale, heartbeat_path
from backend.dates import market_today
from backend.gateway_lifecycle import (
    GATEWAY_WARMUP_SECONDS,
    PORT_POLL_TIMEOUT_SECONDS,
    _gateway_endpoint,
    launch_gateway,
    stop_gateway,
    wait_for_port,
)
from backend.market_data import (
    fetch_index_daily_closes,
    fetch_options_latest_quotes,
    format_occ_symbol,
)
from backend.models import AuditEventModel, TradingControlModel
from backend.operator import alert_crash, send_ntfy_with_retry
from backend.reconciliation import BrokerSnapshot, compare_books
from backend.run_lock import acquire_run_lock, other_gateway_tenant_active, release_run_lock
from backend.trading_control import ACTIVE, sentinel_halt_active

logger = logging.getLogger(__name__)

# Probe geometry (#827): a smallest-viable XSP put vertical, slightly OTM so
# the strikes always exist near the money. The probe is only ever PREVIEWED
# (whatIfOrder) — its price just has to be plausible enough to evaluate.
PROBE_UNDERLYING = "XSP"
PROBE_OTM_OFFSET_POINTS = 5.0
PROBE_WIDTH_POINTS = 5.0
# Floor for the probe's net credit: quotes near the money can cross or sit
# at zero mid-afternoon; whatIf needs a non-degenerate limit, not a fill.
PROBE_MIN_CREDIT = 0.05


@dataclass(frozen=True)
class Finding:
    """One problem the rehearsal surfaced, with its plain-language action."""

    step: str  # gateway | reconciliation | preview | controls | heartbeat
    message: str
    action: str | None = None

    def lines(self) -> list[str]:
        out = [f"[{self.step}] {self.message}"]
        if self.action:
            out.append(f"  ACTION: {self.action}")
        return out


@dataclass
class PreflightReport:
    findings: list[Finding] = field(default_factory=list)
    rehearsed: list[str] = field(default_factory=list)  # steps that ran to completion
    not_rehearsed: list[str] = field(default_factory=list)  # steps skipped (and why, inline)

    def unexpected(self, step: str, exc: BaseException) -> None:
        self.findings.append(Finding(step, f"unexpected {type(exc).__name__}: {exc}"))


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def compose_preflight_push(report: PreflightReport) -> tuple[str, str, str]:
    """(title, body, priority). Pure — tested directly. Title stays ASCII (#598)."""
    lines: list[str] = []
    for finding in report.findings:
        lines.extend(finding.lines())
    if report.rehearsed:
        lines.append(f"Rehearsed: {', '.join(report.rehearsed)}")
    for skipped in report.not_rehearsed:
        lines.append(f"Not rehearsed: {skipped}")
    if not report.findings:
        title = "basis preflight: all clear"
        lines.append("No problems found - tonight's run has a clear runway.")
        return title, "\n".join(lines), "default"
    title = f"basis preflight: {len(report.findings)} problem(s)"
    return title, "\n".join(lines), "high"


# ---------------------------------------------------------------------------
# Step 1 — Gateway + broker session
# ---------------------------------------------------------------------------


def _launch(report: PreflightReport, sleep: Callable[[float], None]) -> subprocess.Popen | None:
    start_script = os.getenv("IBC_START_SCRIPT", "")
    if not start_script or not os.path.exists(start_script):
        report.findings.append(
            Finding(
                "gateway",
                f"IBC_START_SCRIPT missing or not found ({start_script or 'unset'})",
                action="run scripts/setup-ibc.ps1 - tonight's run cannot launch Gateway either",
            )
        )
        return None
    proc = launch_gateway(start_script)
    sleep(GATEWAY_WARMUP_SECONDS)
    return proc


def _open_session(report: PreflightReport, broker_factory: Callable[[], BrokerSession]) -> BrokerSession | None:
    host, port = _gateway_endpoint()
    if not wait_for_port(host, port):
        report.findings.append(
            Finding(
                "gateway",
                f"IB Gateway API port {host}:{port} never opened within {PORT_POLL_TIMEOUT_SECONDS}s",
                action="check IBC config/login (2FA prompt? bad credentials?) before tonight's run",
            )
        )
        return None
    broker = broker_factory()
    try:
        broker.open()
    except BrokerError as exc:
        # #823/#824: the terminal exception can be an anonymous TimeoutError
        # while the REAL cause (e.g. Error 10141, paper-trading disclaimer)
        # arrived only as an API error event — the classified instruction is
        # the whole point of rehearsing this path at 14:00.
        instruction = next(
            (NEEDS_HUMAN_BROKER_ERRORS[code] for code, _ in exc.api_errors if code in NEEDS_HUMAN_BROKER_ERRORS),
            None,
        )
        detail = "; ".join(f"broker API error {code}: {message}" for code, message in exc.api_errors)
        message = f"broker session failed to open: {exc}" + (f" ({detail})" if detail else "")
        report.findings.append(Finding("gateway", message, action=instruction))
        return None
    report.rehearsed.append("gateway+session")
    return broker


# ---------------------------------------------------------------------------
# Step 2 — reconciliation comparison, read-only
# ---------------------------------------------------------------------------


async def _check_reconciliation(
    session_maker: Callable[[], Any], broker: BrokerSession, today: datetime.date, report: PreflightReport
) -> None:
    """The executor's exact drift verdict (reconciliation.compare_books),
    report-only: no reconciliation_runs row, no halt, no audit event."""
    snapshot = BrokerSnapshot(
        positions=tuple(broker.positions()),
        executions=(),  # backfill is the evening run's job — comparison only
        open_orders=tuple(broker.open_orders()),
    )
    async with session_maker() as session:
        comparison = await compare_books(session, snapshot, today=today.isoformat())
    for drift in comparison.drifts:
        report.findings.append(
            Finding(
                "reconciliation",
                f"{drift.kind}: {drift.key} (broker {drift.broker_qty:g}, books {drift.expected_qty:g})",
                action="tonight's run will halt entries on this drift - resolve via the reconciliation panel first",
            )
        )
    report.rehearsed.append("reconciliation comparison")


# ---------------------------------------------------------------------------
# Step 3 — preview probe (whatIfOrder only; nothing is ever placed)
# ---------------------------------------------------------------------------


def next_trading_day(day: datetime.date) -> datetime.date:
    d = day + datetime.timedelta(days=1)
    guard = 0
    while not is_trading_day(d) and guard < 14:
        d += datetime.timedelta(days=1)
        guard += 1
    return d


def probe_leg_symbols(today: datetime.date, spot: float) -> tuple[str, str]:
    """OCC symbols for the probe vertical: short put slightly OTM, long put
    one width below, expiring the next trading day (XSP lists dailies)."""
    expiry = next_trading_day(today).isoformat()
    short_strike = float(int(spot)) - PROBE_OTM_OFFSET_POINTS
    long_strike = short_strike - PROBE_WIDTH_POINTS
    return (
        format_occ_symbol(PROBE_UNDERLYING, expiry, "PUT", short_strike),
        format_occ_symbol(PROBE_UNDERLYING, expiry, "PUT", long_strike),
    )


def _check_preview(broker: BrokerSession, today: datetime.date, report: PreflightReport) -> None:
    closes = fetch_index_daily_closes(PROBE_UNDERLYING, 5)
    if not closes:
        report.findings.append(
            Finding(
                "preview",
                f"no {PROBE_UNDERLYING} price available - probe spread could not be built",
                action="market data path is degraded; tonight's scan may also be unpriceable",
            )
        )
        return
    spot = closes[-1][1]
    short_occ, long_occ = probe_leg_symbols(today, spot)
    quotes = fetch_options_latest_quotes([short_occ, long_occ])
    if short_occ not in quotes or long_occ not in quotes:
        report.findings.append(
            Finding(
                "preview",
                f"probe legs unpriceable ({short_occ}, {long_occ}) - no usable quotes",
                action="option quote path is degraded; tonight's entries may be unpriceable",
            )
        )
        return
    credit = round(quotes[short_occ] - quotes[long_occ], 2)
    spread = SpreadOrder(
        legs=((short_occ, "SELL", 1), (long_occ, "BUY", 1)),
        quantity=1,
        net_limit_price=-max(credit, PROBE_MIN_CREDIT),
        underlying=PROBE_UNDERLYING,
    )
    try:
        broker.preview_spread(spread)
    except PreviewRejectedError as exc:
        # A refusal is the preview GATE working — reportable, but distinct
        # from the machinery itself crashing (the outer guard's territory).
        report.findings.append(
            Finding(
                "preview",
                f"preview gate refused the probe: {exc}",
                action="tonight's entries would be refused the same way - check margin/permissions at the broker",
            )
        )
    report.rehearsed.append("preview probe")


# ---------------------------------------------------------------------------
# Step 4 — control + heartbeat state (database reads and one file read only)
# ---------------------------------------------------------------------------


async def _check_controls(session_maker: Callable[[], Any], report: PreflightReport) -> None:
    if sentinel_halt_active():
        report.findings.append(
            Finding(
                "controls",
                "HALT sentinel file present - every scope reads HALT_ENTRIES",
                action="delete the HALT file if the halt is no longer intended",
            )
        )
    async with session_maker() as session:
        rows = (await session.execute(select(TradingControlModel))).scalars().all()
    for row in rows:
        if row.state != ACTIVE:
            report.findings.append(
                Finding(
                    "controls",
                    f"{row.scope} is {row.state} ({row.reason or 'no reason recorded'})",
                    action="tonight's run will not place entries for this scope - resume from the console if intended",
                )
            )
    report.rehearsed.append("control state")


def _check_heartbeat(report: PreflightReport, now: datetime.datetime | None = None) -> None:
    heartbeat_at: str | None = None
    path = heartbeat_path()
    if path.exists():
        try:
            heartbeat_at = json.loads(path.read_text(encoding="utf-8")).get("at")
        except (OSError, ValueError):
            heartbeat_at = None
    if _is_stale(heartbeat_at, now or datetime.datetime.now(datetime.UTC)):
        report.findings.append(
            Finding(
                "heartbeat",
                f"executor heartbeat stale or missing (last: {heartbeat_at or 'never'})",
                action="the last scheduled run may not have completed - check its digest and run logs",
            )
        )
    report.rehearsed.append("heartbeat")


# ---------------------------------------------------------------------------
# The one permitted database write: the PREFLIGHT_RUN audit event
# ---------------------------------------------------------------------------


async def _write_preflight_audit(session_maker: Callable[[], Any], report: PreflightReport) -> None:
    async with session_maker() as session:
        session.add(
            AuditEventModel(
                run_at=_now(),
                book_id=None,
                event_type="PREFLIGHT_RUN",
                actor="preflight",
                payload={
                    "findings": [{"step": f.step, "message": f.message, "action": f.action} for f in report.findings],
                    "rehearsed": report.rehearsed,
                    "not_rehearsed": report.not_rehearsed,
                },
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_preflight(
    today: datetime.date | None = None,
    broker_factory: Callable[[], BrokerSession] | None = None,
    session_maker: Callable[[], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """The scheduled-task body. Returns a process exit code (always 0 once
    the rehearsal ran — findings are report content, not failures)."""
    today = today or market_today()  # market clock, not UTC (#259)
    if not is_trading_day(today):
        logger.info("Market holiday %s - nothing to rehearse", today.isoformat())
        return 0

    broker_factory = broker_factory or BrokerSession
    if session_maker is None:
        from backend.database import async_session_maker

        session_maker = async_session_maker

    lock = acquire_run_lock("preflight")
    if lock is None:
        logger.warning("preflight lock held - another preflight is live; aborting this one")
        return 4

    proc: subprocess.Popen | None = None
    broker: BrokerSession | None = None
    try:
        # Respect every live Gateway tenant (#416/#471/#681): a rehearsal
        # must never share a Gateway with — or get torn down by — the real
        # run, the fill check, or a restore drill.
        if other_gateway_tenant_active("preflight"):
            send_ntfy_with_retry("basis preflight: skipped", "executor running, preflight skipped")
            logger.warning("Another Gateway tenant is active - preflight skipped")
            return 0

        report = PreflightReport()

        # Step 1: Gateway + session. Each step guarded — an exception is a
        # finding, never a crash; later steps still run and still report.
        try:
            proc = _launch(report, sleep)
        except Exception as exc:
            report.unexpected("gateway", exc)
        if proc is not None:
            try:
                broker = _open_session(report, broker_factory)
            except Exception as exc:
                report.unexpected("gateway", exc)

        # Steps 2 + 3 need the open session; without one they are named as
        # not-rehearsed rather than silently absent.
        if broker is not None:
            try:
                await _check_reconciliation(session_maker, broker, today, report)
            except Exception as exc:
                report.unexpected("reconciliation", exc)
            try:
                _check_preview(broker, today, report)
            except Exception as exc:
                report.unexpected("preview", exc)
        else:
            report.not_rehearsed.append("reconciliation comparison (broker session unavailable)")
            report.not_rehearsed.append("preview probe (broker session unavailable)")

        # Step 4 is database/file-only — runs regardless of the broker.
        try:
            await _check_controls(session_maker, report)
        except Exception as exc:
            report.unexpected("controls", exc)
        try:
            _check_heartbeat(report)
        except Exception as exc:
            report.unexpected("heartbeat", exc)

        try:
            await _write_preflight_audit(session_maker, report)
        except Exception as exc:
            # Best-effort evidence trail — the push below is the report.
            logger.warning("PREFLIGHT_RUN audit write failed: %s", exc)

        title, body, priority = compose_preflight_push(report)
        send_ntfy_with_retry(title, body, priority)
        logger.info("%s\n%s", title, body)
        return 0
    finally:
        if broker is not None:
            broker.close()
        # Teardown mirrors gateway_lifecycle (#471/#681): kill the Gateway
        # tree unless some OTHER tenant went live while we ran.
        if other_gateway_tenant_active("preflight"):
            logger.warning("Another Gateway tenant is active - leaving Gateway up (#471/#681)")
        elif proc is not None:
            stop_gateway(proc)
        release_run_lock(lock)


def main() -> int:
    from backend.run_logging import setup_run_logging

    setup_run_logging("preflight")
    # The known failure modes are findings inside the push; anything ELSE
    # crashing must not exit silently — a scheduled task has no audience.
    try:
        return asyncio.run(run_preflight())
    except Exception as exc:
        logger.exception("Preflight crashed")
        alert_crash("basis preflight CRASHED", f"{type(exc).__name__}: {exc}", "high")
        return 4


if __name__ == "__main__":
    sys.exit(main())
