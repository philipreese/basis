"""fill_check.py — the read-only morning fill push (#236).

Entry orders rest overnight and fill at the open, but the pipeline doesn't
look until 18:45 — this 10:00 check tells the operator what filled without
making them wait out the workday. It is strictly a NOTIFICATION:

- No trade/ledger writes. The evening executor remains the sole trade
  mutator — fills become positions there, on its schedule, exactly as before
  this existed. The one write this run may make is CONTROL-plane: the second
  daily ntfy command poll (#278) can apply a remote HALT, which only ever
  moves the system toward safety.
- Always pushes, fills or not. "0 of your resting orders filled" is
  information; silence would be indistinguishable from the check not running.

Lifecycle mirrors the nightly run (IBC start → port poll → work → teardown)
by reusing gateway_lifecycle's pieces. Executions come from
reqExecutionsAsync, whose default filter returns today's executions for the
account; only orders carrying the bot's "basis:" orderRef are reported.
"""

import datetime
import logging
import os
import sys
import time
from collections import defaultdict
from typing import Any

from backend.market_data import _run_ib

logger = logging.getLogger(__name__)

ORDER_REF_PREFIX = "basis:"


async def _fetch_today_executions(ib: Any) -> list[dict]:
    """Today's executions as plain dicts (orderRef/side/qty/price/symbol)."""
    from ib_async import ExecutionFilter

    from backend.broker import is_bag_execution

    fills = await ib.reqExecutionsAsync(ExecutionFilter())
    return [
        {
            "order_ref": f.execution.orderRef or "",
            "side": f.execution.side,
            "quantity": abs(float(f.execution.shares)),
            "price": float(f.execution.price),
            "symbol": getattr(f.contract, "localSymbol", "") or f.contract.symbol,
        }
        for f in fills
        # The BAG-level combo execution (#331) is an artifact — the push
        # shows real legs, not a mystery conId at the net price.
        if not is_bag_execution(f)
    ]


def compose_fill_push(executions: list[dict]) -> tuple[str, str]:
    """(title, body) for the morning push. Pure — tested directly."""
    ours = [e for e in executions if e["order_ref"].startswith(ORDER_REF_PREFIX)]
    if not ours:
        return "basis fills: none yet", "No resting basis orders have filled so far today."

    by_ref: dict[str, list[dict]] = defaultdict(list)
    for e in ours:
        by_ref[e["order_ref"]].append(e)
    lines = []
    for ref in sorted(by_ref):
        legs = by_ref[ref]
        leg_bits = ", ".join(f"{e['side']} {e['symbol']} @ {e['price']:.2f}" for e in legs)
        lines.append(f"{ref} — {len(legs)} leg fill(s): {leg_bits}")
    title = f"basis fills: {len(by_ref)} order(s) filled"
    return title, "\n".join(lines)


def run_fill_check(today: datetime.date | None = None) -> int:
    """Scheduled-task entry point. Returns a process exit code."""
    from backend.calendars import is_trading_day
    from backend.dates import market_today
    from backend.gateway_lifecycle import (
        GATEWAY_WARMUP_SECONDS,
        PORT_POLL_TIMEOUT_SECONDS,
        _gateway_endpoint,
        launch_gateway,
        stop_gateway,
        wait_for_port,
    )
    from backend.operator import send_ntfy

    today = today or market_today()  # market clock, not UTC (#259)
    if not is_trading_day(today):
        logger.info("Market holiday %s — no fills to check", today.isoformat())
        return 0

    start_script = os.getenv("IBC_START_SCRIPT", "")
    if not start_script or not os.path.exists(start_script):
        send_ntfy("basis fill check NOT RUN", "IBC_START_SCRIPT missing — run scripts/setup-ibc.ps1", "high")
        return 2

    host, port = _gateway_endpoint()
    proc = launch_gateway(start_script)
    try:
        time.sleep(GATEWAY_WARMUP_SECONDS)
        if not wait_for_port(host, port):
            send_ntfy(
                "basis fill check NOT RUN",
                f"IB Gateway port {host}:{port} never opened within {PORT_POLL_TIMEOUT_SECONDS}s",
                "high",
            )
            return 3
        executions = _run_ib(_fetch_today_executions)
        title, body = compose_fill_push(executions)
        send_ntfy(title, body)
        logger.info("%s\n%s", title, body)
        _poll_remote_commands()
        return 0
    finally:
        # Gateway collision guard (#418): an operator re-running a missed
        # night in the morning shares this Gateway. The stop_gateway sweep
        # kills EVERY ibgateway java process — mid-run, possibly between an
        # executor's order placement and its state commit. A fresh executor
        # run lock means that run owns the teardown; leave the Gateway up.
        from backend.run_lock import lock_is_held

        if lock_is_held("executor"):
            logger.warning("Executor run lock held — leaving Gateway up for the running executor (#418)")
        else:
            stop_gateway(proc)


def _poll_remote_commands() -> None:
    """The second daily command poll (#278, audit H7): a morning HALT used to
    wait until 18:45. Control-plane writes only — the market/ledger charter
    (evening executor is the sole trade mutator) is untouched."""
    import asyncio

    from backend.database import async_session_maker
    from backend.trading_control import apply_ntfy_commands

    async def _run() -> int:
        async with async_session_maker() as session:
            return await apply_ntfy_commands(session)

    try:
        applied = asyncio.run(_run())
        if applied:
            logger.info("Applied %d remote HALT command(s) at the morning poll", applied)
    except Exception as exc:  # the fill push already went out — never fail the run over this
        logger.warning("Morning command poll failed: %s", exc)


def main() -> int:
    from backend.operator import send_ntfy
    from backend.run_logging import setup_run_logging

    setup_run_logging("fill_check")
    # The known failure modes push their own alerts; anything ELSE crashing
    # must not exit silently — a scheduled task's exit code has no audience.
    try:
        return run_fill_check()
    except Exception as exc:
        logger.exception("Fill check crashed")
        send_ntfy("basis fill check CRASHED", f"{type(exc).__name__}: {exc}", "high")
        return 4


if __name__ == "__main__":
    sys.exit(main())
