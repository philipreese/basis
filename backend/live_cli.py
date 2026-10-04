"""live_cli.py — commands for the live process (#1065). Reached only through
backend/live_entry.py, which loads the live environment overlay first.

    run [--dry-run] [--rehearse] [--nightly]
    grant   --book B36 --attest "..."
    step-up --book B36 --clean-dates 2026-10-30,2026-11-30,2026-12-31 --attest "..."
    revoke  --book B36 --reason "..."

`run` is a DRY RUN unless IBKR_LIVE_ARM is exactly the arm token and
--dry-run is absent. --nightly wraps the run in the live Gateway's own
lifecycle (IBC start, port wait, run, tree-only teardown), the scheduled
task's mode; without it the run expects a live Gateway already listening.
"""

import argparse
import asyncio
import datetime
import logging
import os
import sys
import time

from backend.calendars import is_trading_day
from backend.dates import market_today
from backend.env import base_env_values, overlay_path
from backend.live_executor import (
    LiveConfig,
    LiveRefusal,
    compose_live_digest,
    live_mode_env_ok,
    resolve_live_config,
    run_live_executor,
)

logger = logging.getLogger(__name__)

LIVE_GATEWAY_LOCK = "live_gateway"


def _alert(title: str, body: str, event_type: str = "SCHEDULER_ALERT") -> None:
    from backend.operator import alert_crash

    alert_crash(title, body, "urgent", event_type=event_type)


def _refuse(reason: str) -> int:
    logger.error("Live run refused: %s", reason)
    _alert("basis LIVE NOT RUN", reason, event_type="LIVE_RUN_REFUSED")
    print(f"basis LIVE NOT RUN: {reason}", file=sys.stderr)
    return 2


async def _execute(config: LiveConfig, rehearse: bool) -> int:
    from backend.database import async_session_maker, init_db
    from backend.models import AuditEventModel
    from backend.operator import send_ntfy_with_retry

    await init_db()
    try:
        summary = await run_live_executor(config, rehearse=rehearse)
    except LiveRefusal as exc:
        return _refuse(str(exc))
    title, body, priority = compose_live_digest(summary)
    pushed = send_ntfy_with_retry(title, body, priority)
    async with async_session_maker() as session:
        session.add(
            AuditEventModel(
                run_at=datetime.datetime.now(datetime.UTC).isoformat(),
                book_id=None,
                event_type="LIVE_DIGEST_COMPOSED",
                actor="live_executor",
                payload={"title": title, "body": body, "priority": priority, "pushed": pushed},
            )
        )
        await session.commit()
    print(f"\n{title}\n{body}")
    return 0


def _run_once(config: LiveConfig, rehearse: bool) -> int:
    try:
        return asyncio.run(_execute(config, rehearse))
    except Exception as exc:
        logger.exception("Live executor crashed")
        _alert("basis LIVE executor CRASHED", f"{type(exc).__name__}: {exc}", event_type="CRASH_ALERT")
        return 4


def run_live_nightly(config: LiveConfig, today: datetime.date | None = None) -> int:
    """The live scheduled task: the paper lifecycle's shape on the live
    Gateway's own IBC script and port. Two differences, both so the paper
    and live Gateways cannot hurt each other: it holds the `live_gateway`
    tenancy lock (paper tenants wait for it and leave it alone), and its
    teardown kills only the processes this launch created — never paper's
    system-wide ibgateway sweep."""
    from backend.gateway_lifecycle import (
        GATEWAY_WARMUP_SECONDS,
        PORT_POLL_TIMEOUT_SECONDS,
        _backup_after_run,
        launch_gateway,
        stop_gateway_tree_only,
        wait_for_gateway_port,
        wait_for_port,
        wait_for_tenant_clear,
    )
    from backend.run_lock import acquire_run_lock, release_run_lock

    today = today or market_today()
    if not is_trading_day(today):
        return _run_once(config, rehearse=False)  # the run notes the holiday and exits
    if not os.path.exists(config.start_script):
        return _refuse(f"the live IBC start script ({os.path.basename(config.start_script)}) was not found")
    lock = acquire_run_lock(LIVE_GATEWAY_LOCK)
    if lock is None:
        return _refuse("the live Gateway tenancy lock is held — another live run is mid-window")
    proc = None
    launched_at: float | None = None
    try:
        if not wait_for_tenant_clear(LIVE_GATEWAY_LOCK):
            return _refuse("another Gateway tenant (a paper run?) was still active — not launching the live Gateway")
        launched_at = time.time()
        proc = launch_gateway(config.start_script)
        time.sleep(GATEWAY_WARMUP_SECONDS)
        port_res = wait_for_gateway_port(
            config.host, config.port, proc=proc, connect_fn=lambda h, p: wait_for_port(h, p, timeout_seconds=0)
        )
        if not port_res.is_open:
            return _refuse(
                f"the live Gateway API port never opened within {PORT_POLL_TIMEOUT_SECONDS}s — check the live IBC "
                "login (a 2FA prompt waiting on the phone?)"
            )
        return _run_once(config, rehearse=False)
    finally:
        _backup_after_run()
        if proc is not None:
            stop_gateway_tree_only(proc, created_after=launched_at)
        release_run_lock(lock)


async def _grant_command(args: argparse.Namespace) -> int:
    from backend import live_grant
    from backend.database import async_session_maker, init_db

    await init_db()
    async with async_session_maker() as session:
        try:
            if args.command == "grant":
                result = await live_grant.grant_stage1(session, args.book, args.attest, market_today())
            elif args.command == "step-up":
                dates = [datetime.date.fromisoformat(d.strip()) for d in args.clean_dates.split(",") if d.strip()]
                result = await live_grant.step_up(session, args.book, dates, args.attest, market_today())
            else:
                await live_grant.revoke(session, args.book, args.reason)
                print(f"{args.book}: live authority REVOKED; book entries halted")
                return 0
        except (live_grant.GrantRefused, ValueError) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
    print(
        f"{result.book_id}: {result.kind} grant #{result.grant_id} recorded under demotion policy "
        f"v{result.demotion_policy_version}. Entries stay halted until you RESUME the book on the live console."
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live", description="basis live executor (#1065)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="one live run (a dry run unless armed)")
    run.add_argument("--dry-run", action="store_true", help="never transmit, even when armed")
    run.add_argument("--rehearse", action="store_true", help="dry run of the latest month-end, on any day")
    run.add_argument("--nightly", action="store_true", help="start and stop the live Gateway around the run")
    grant = sub.add_parser("grant", help="record a stage-1 live grant")
    grant.add_argument("--book", required=True)
    grant.add_argument("--attest", required=True)
    step = sub.add_parser("step-up", help="record a step-up grant at the config's larger stake")
    step.add_argument("--book", required=True)
    step.add_argument("--clean-dates", required=True, help="three consecutive clean month-end signal dates")
    step.add_argument("--attest", required=True)
    revoke = sub.add_parser("revoke", help="manually revoke a book's live authority")
    revoke.add_argument("--book", required=True)
    revoke.add_argument("--reason", required=True)
    return parser


def dispatch(argv: list[str]) -> int:
    from backend.run_logging import setup_run_logging

    args = _parser().parse_args(argv)
    setup_run_logging("live_executor")
    if not live_mode_env_ok():
        return _refuse("this process is not in live mode (IBKR_TRADING_MODE and the database module disagree)")
    if args.command != "run":
        return asyncio.run(_grant_command(args))
    if args.rehearse and not args.dry_run:
        return _refuse("--rehearse is dry-run only — add --dry-run")
    try:
        config = resolve_live_config(
            os.environ, base_env_values(), overlay_in_use=overlay_path() is not None, dry_run=args.dry_run
        )
    except LiveRefusal as exc:
        return _refuse(str(exc))
    if args.nightly:
        return run_live_nightly(config)
    return _run_once(config, rehearse=args.rehearse)
