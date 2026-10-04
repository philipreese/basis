"""live_cli.py — commands for the live process (#1065). Reached only through
backend/live_entry.py, which loads the live environment overlay first.

    run [--dry-run] [--rehearse] [--nightly]
    check
    grant   --book B36 --attest "..."
    step-up --book B36 --clean-dates 2026-10-30,2026-11-30,2026-12-31 --attest "..."
    revoke  --book B36 --reason "..."

`run` is a DRY RUN unless IBKR_LIVE_ARM is exactly the arm token and
--dry-run is absent. The live Gateway is never started or stopped here
(#1098): it runs continuously under IBC (scripts/register-live-gateway-task.ps1), so
its 2FA login survives across nights. --nightly is the scheduled task's
mode: the run plus the post-run database backup. `check` only probes the
live Gateway and its login (port, handshake, account guard) and pushes an
urgent alert when it is not logged in — something to schedule after the
Sunday cold restart if you want a backstop to IBKR's own 2FA prompt.
"""

import argparse
import asyncio
import datetime
import logging
import os
import sys

from backend.calendars import is_trading_day
from backend.dates import market_today
from backend.env import base_env_values, live_overlay_values, overlay_path, overlay_values, set_before_load
from backend.live_executor import (
    GATEWAY_NOT_LOGGED_IN,
    LIVE_ACCOUNT_VAR,
    LIVE_ARM_VAR,
    LiveConfig,
    LiveGatewayNotLoggedIn,
    LiveRefusal,
    compose_live_digest,
    default_broker_factory,
    default_gateway_probe,
    live_mode_env_ok,
    not_logged_in,
    resolve_live_config,
    run_live_executor,
)

logger = logging.getLogger(__name__)


def _alert(title: str, body: str, event_type: str = "SCHEDULER_ALERT") -> None:
    from backend.operator import alert_crash

    alert_crash(title, body, "urgent", event_type=event_type)


def _refuse(reason: str) -> int:
    logger.error("Live run refused: %s", reason)
    _alert("basis LIVE NOT RUN", reason, event_type="LIVE_RUN_REFUSED")
    print(f"basis LIVE NOT RUN: {reason}", file=sys.stderr)
    return 2


def _not_logged_in(reason: str) -> int:
    """The Gateway-login refusal: the push TITLE carries the action, so it
    reads on a locked phone (#1098)."""
    logger.error("Live Gateway not logged in: %s", reason)
    _alert(f"basis LIVE: {GATEWAY_NOT_LOGGED_IN}", reason, event_type="LIVE_GATEWAY_NOT_LOGGED_IN")
    print(f"basis LIVE NOT RUN: {reason}", file=sys.stderr)
    return 3


async def _execute(config: LiveConfig, rehearse: bool) -> int:
    from backend.database import async_session_maker, init_db
    from backend.models import AuditEventModel
    from backend.operator import send_ntfy_with_retry

    await init_db()
    try:
        summary = await run_live_executor(config, rehearse=rehearse)
    except LiveGatewayNotLoggedIn as exc:
        return _not_logged_in(str(exc))
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
    """The live scheduled task (#1098): the run against the persistent live
    Gateway, then the database backup. Unlike the paper lifecycle it never
    launches or kills a Gateway: a fresh live login needs 2FA on the phone,
    so the Gateway stays up under IBC's own daily auto-restart. The run
    itself probes the API port and refuses loudly when it is not logged in."""
    from backend.gateway_lifecycle import _backup_after_run

    today = today or market_today()
    try:
        return _run_once(config, rehearse=False)
    finally:
        if is_trading_day(today):
            _backup_after_run()


def check_live_gateway(config: LiveConfig) -> int:
    """Probe the live Gateway's login without trading: the API port, the
    handshake, and the one-account guard. The session is opened with
    transmission locked. 0 when logged in; an urgent push otherwise."""
    from backend.broker import BrokerError

    if not default_gateway_probe(config.host, config.port):
        return _not_logged_in(f"{GATEWAY_NOT_LOGGED_IN} (the live API port did not answer)")
    session = default_broker_factory(config)
    try:
        session.open()
    except BrokerError as exc:
        if not_logged_in(exc):
            return _not_logged_in(f"{GATEWAY_NOT_LOGGED_IN} ({exc})")
        return _refuse(f"the live Gateway answered but the account guard refused: {exc}")
    session.close()
    print("basis LIVE: the live Gateway is logged in to the configured account")
    return 0


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
        f"v{result.demotion_policy_version}. {grant_state_line(result.book_id, result.control_state)}"
    )
    return 0


def grant_state_line(book_id: str, state: str) -> str:
    """What the book will actually do next (#1101) — read from the control
    state the grant left, never assumed."""
    from backend.trading_control import FLATTEN_REQUESTED, HALT_ENTRIES

    if state == HALT_ENTRIES:
        return f"{book_id} entries are HALTED (book scope) — it trades only after you RESUME it on the live console."
    if state == FLATTEN_REQUESTED:
        return f"{book_id} is in FLATTEN_REQUESTED — the live run keeps selling its shares and opens nothing new."
    return f"{book_id} book scope is {state} — check the live console before the next run."


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live", description="basis live executor (#1065)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="one live run (a dry run unless armed)")
    run.add_argument("--dry-run", action="store_true", help="never transmit, even when armed")
    run.add_argument("--rehearse", action="store_true", help="dry run of the latest month-end, on any day")
    run.add_argument("--nightly", action="store_true", help="the scheduled task's mode: the run, then a DB backup")
    sub.add_parser("check", help="probe the live Gateway's login; urgent push when it is not logged in")
    grant = sub.add_parser("grant", help="record a stage-1 live grant")
    grant.add_argument("--book", required=True)
    grant.add_argument("--attest", required=True)
    step = sub.add_parser("step-up", help="record a step-up grant at the overlay's larger private stake")
    step.add_argument("--book", required=True)
    step.add_argument("--clean-dates", required=True, help="three consecutive clean month-end signal dates")
    step.add_argument("--attest", required=True)
    revoke = sub.add_parser("revoke", help="manually revoke a book's live authority")
    revoke.add_argument("--book", required=True)
    revoke.add_argument("--reason", required=True)
    return parser


def dispatch(argv: list[str]) -> int:
    from backend.run_logging import secure_live_logging, setup_run_logging

    args = _parser().parse_args(argv)
    setup_run_logging("live_executor")
    # #1101: ib_async to WARNING, and the live account id redacted from every
    # log handler — the run log is a local file, not a place for the id.
    secure_live_logging([(os.environ.get(LIVE_ACCOUNT_VAR) or "").strip()])
    if not live_mode_env_ok():
        return _refuse("this process is not in live mode (IBKR_TRADING_MODE and the database module disagree)")
    if args.command not in ("run", "check"):
        return asyncio.run(_grant_command(args))
    if args.command == "run" and args.rehearse and not args.dry_run:
        return _refuse("--rehearse is dry-run only — add --dry-run")
    try:
        config = resolve_live_config(
            os.environ,
            base_env_values(),
            overlay_in_use=overlay_path() is not None,
            # `check` never transmits, whatever the arm flag says.
            dry_run=args.command == "check" or args.dry_run,
            paper_view_of_overlay=live_overlay_values(),
            # #1101: the arm token comes from the overlay FILE only.
            overlay_values=overlay_values(),
            arm_set_before_load=set_before_load(LIVE_ARM_VAR),
        )
    except LiveRefusal as exc:
        return _refuse(str(exc))
    if args.command == "check":
        return check_live_gateway(config)
    if args.nightly:
        return run_live_nightly(config)
    return _run_once(config, rehearse=args.rehearse)
