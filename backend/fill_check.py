"""fill_check.py — the read-only morning fill push (#236).

Entry orders rest overnight and fill at the open, but the pipeline doesn't
look until 18:45 — this 10:00 check tells the operator what filled without
making them wait out the workday. It is strictly a NOTIFICATION:

- No trade/ledger writes. Fills become positions in the evening executor, on
  its schedule, exactly as before this existed. (#960 ended "the evening
  executor is the sole trade mutator": the 12:30 midday exit pass now places
  and cancels EXIT orders too. It does not change this module's charter —
  fill→position booking and every other ledger write still happen only in the
  evening — but the sentence was load-bearing prose and is no longer true as
  written.) The one write this run may make is CONTROL-plane: the second
  daily ntfy command poll (#278) can apply a remote HALT, which only ever
  moves the system toward safety.
- Each filled order reads as a plain-English headline (#1115,
  fill_notice.py) over its raw leg line; the raw line alone when the
  headline can't be stated correctly. Order context (strategy, ordered
  size, entry, exit reason) is a best-effort READ of the database.
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
from typing import TYPE_CHECKING, Any

from backend.fill_notice import DEFAULT_MULTIPLIER, ExecutionRow, OrderContext, describe_fill
from backend.market_data import _run_ib

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

ORDER_REF_PREFIX = "basis:"


async def _fetch_today_executions(ib: Any) -> list[ExecutionRow]:
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
            # #1115: the plain-English headline scales by the real contract
            # multiplier; an empty/unparseable one falls back to 100.
            "multiplier": _multiplier(getattr(f.contract, "multiplier", "")),
        }
        for f in fills
        # The BAG-level combo execution (#331) is an artifact — the push
        # shows real legs, not a mystery conId at the net price.
        if not is_bag_execution(f)
    ]


def _multiplier(raw: object) -> float:
    try:
        value = float(str(raw))
    except ValueError:
        return DEFAULT_MULTIPLIER
    return value if value > 0 else DEFAULT_MULTIPLIER


def _headline(ref: str, legs: list[ExecutionRow], contexts: dict[str, OrderContext]) -> str | None:
    """The plain-English line, or None. Never raises: an unexpected error in
    the formatter must cost the headline, not the whole push (#1115) — a
    raise here would crash run_fill_check and the operator would get a
    CRASHED alert instead of their fills."""
    try:
        return describe_fill(ref, legs, contexts.get(ref))
    except Exception:
        logger.exception("Plain-English fill headline failed for %s — sending the raw line", ref)
        return None


def compose_fill_push(
    executions: list[ExecutionRow], contexts: dict[str, OrderContext] | None = None
) -> tuple[str, str]:
    """(title, body) for the morning push. Pure — tested directly.

    Each filled order gets a plain-English headline (#1115) followed by the
    raw leg line. When the headline cannot be stated correctly (partial
    fill, missing legs, an unrecognised shape) the raw line goes alone:
    the notification is never dropped. *contexts* is what the database
    knew about each order (strategy, ordered size, entry, exit reason),
    keyed by order_ref; without it the headline is built from the legs."""
    contexts = contexts or {}
    ours = [e for e in executions if e["order_ref"].startswith(ORDER_REF_PREFIX)]
    if not ours:
        return "basis fills: none yet", "No resting basis orders have filled so far today."

    by_ref: dict[str, list[ExecutionRow]] = defaultdict(list)
    for e in ours:
        by_ref[e["order_ref"]].append(e)
    lines = []
    for ref in sorted(by_ref):
        legs = by_ref[ref]
        headline = _headline(ref, legs, contexts)
        if headline:
            lines.append(headline)
        leg_bits = ", ".join(f"{e['side']} {e['symbol']} @ {e['price']:.2f}" for e in legs)
        lines.append(f"{'  ' if headline else ''}{ref} — {len(legs)} leg fill(s): {leg_bits}")
    title = f"basis fills: {len(by_ref)} order(s) filled"
    return title, "\n".join(lines)


async def load_order_contexts(
    refs: list[str], session_maker: "async_sessionmaker[AsyncSession] | None" = None
) -> dict[str, OrderContext]:
    """What the database knows about each filled order (#1115). Read-only.

    Option orders: strategy, ordered size and legs from `combo_legs`; the
    exit reason for closes; the position's fill-derived entry premium for
    realized P&L. A resting `:tp` profit-taker is staged with no
    position_id (it is linked when the evening sync books the parent's
    fill), so its position is reached through the parent order's ref.
    Share orders: the ordered share count, for the partial-fill note."""
    from sqlalchemy import select

    from backend.database import async_session_maker
    from backend.models import OrderModel, PositionModel, ShareOrderModel

    maker = session_maker or async_session_maker
    out: dict[str, OrderContext] = {}
    async with maker() as session:
        for ref in refs:
            share = (await session.execute(select(ShareOrderModel).filter_by(order_ref=ref))).scalar_one_or_none()
            if share is not None:
                out[ref] = OrderContext(share_quantity=share.quantity)
                continue
            order = (await session.execute(select(OrderModel).filter_by(order_ref=ref))).scalar_one_or_none()
            if order is None:
                continue
            meta = order.combo_legs or {}
            position_id = order.position_id
            if position_id is None and ref.endswith(":tp"):
                parent = (
                    await session.execute(select(OrderModel).filter_by(order_ref=ref.removesuffix(":tp")))
                ).scalar_one_or_none()
                position_id = parent.position_id if parent is not None else None
            position = await session.get(PositionModel, position_id) if position_id else None
            raw_legs = meta.get("legs") or []
            occs = tuple(str(leg.get("occ", "")) for leg in raw_legs)
            qty = meta.get("quantity")
            out[ref] = OrderContext(
                strategy_type=meta.get("strategy_type") or (position.strategy_type if position else None),
                order_quantity=int(qty) if isinstance(qty, int | float) else None,
                leg_occs=occs if occs and all(occs) else (),
                exit_trigger=meta.get("exit_trigger"),
                entry_premium=position.entry_premium if position else None,
                premium_direction=position.premium_direction if position else None,
            )
    return out


def _load_contexts_best_effort(executions: list[ExecutionRow]) -> dict[str, OrderContext]:
    """load_order_contexts, but a database problem only costs the extra
    context — the push still goes out, built from the executions alone."""
    import asyncio

    refs = sorted({e["order_ref"] for e in executions if e["order_ref"].startswith(ORDER_REF_PREFIX)})
    if not refs:
        return {}
    try:
        return asyncio.run(load_order_contexts(refs))
    except Exception as exc:
        logger.warning("Fill-push order context unavailable (%s) — headlines from executions only", exc)
        return {}


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
    # Own tenancy marker (#471): the evening run's stop_gateway kills every
    # ibgateway java process — this lock is what its teardown defers to, so
    # a fill check mid-fetch on the shared Gateway doesn't die with a false
    # CRASHED alert and a lost fill push. Mirror of the executor guard below.
    from backend.run_lock import acquire_run_lock, other_gateway_tenant_active, release_run_lock

    fill_lock = acquire_run_lock("fill_check")
    if fill_lock is None:
        logger.warning("fill_check lock held — another fill check is live; aborting this one")
        return 4
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
            send_ntfy(
                "basis fill check NOT RUN",
                f"IB Gateway port {host}:{port} never opened within {PORT_POLL_TIMEOUT_SECONDS}s",
                "high",
            )
            return 3
        # #785: this is fill_check's one gateway-open per morning run — the
        # port already polled open above, but the API handshake can still
        # lose the race against Gateway's login window exactly like the
        # nightly run did. Unlike the routine per-symbol market-data
        # fetches (_run_ib's default), retrying here costs nothing extra:
        # one connect, once, not a dozen calls in a tight HTTP handler.
        executions = _run_ib(_fetch_today_executions, retry=True)
        title, body = compose_fill_push(executions, _load_contexts_best_effort(executions))
        send_ntfy(title, body)
        logger.info("%s\n%s", title, body)
        _poll_remote_commands()
        return 0
    finally:
        # Gateway collision guard (#418): an operator re-running a missed
        # night in the morning shares this Gateway. The stop_gateway sweep
        # kills EVERY ibgateway java process — mid-run, possibly between an
        # executor's order placement and its state commit. A fresh executor
        # or gateway-tenancy lock (#471 — the nightly run holds it from
        # BEFORE launch, closing the pre-run-lock window) means that run
        # owns the teardown; leave the Gateway up. #681: checked against
        # every OTHER Gateway tenant (run_lock.GATEWAY_TENANT_LOCKS), not a
        # hand-spelled subset — a restore drill mid-run is exactly as live
        # a tenant as the executor.
        if other_gateway_tenant_active("fill_check"):
            logger.warning("Another Gateway tenant is active — leaving Gateway up (#418/#471/#681)")
        elif proc is not None:
            stop_gateway(proc)
        release_run_lock(fill_lock)


def _poll_remote_commands() -> None:
    """The second daily command poll (#278, audit H7): a morning HALT used to
    wait until 18:45. Control-plane writes only — this module's market/ledger
    charter (it never places, cancels, or books anything) is untouched. The
    trade mutators are now the evening executor and the 12:30 midday exit
    pass (#960); the fill check is neither."""
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
    from backend.operator import alert_crash
    from backend.run_logging import setup_run_logging

    setup_run_logging("fill_check")
    # The known failure modes push their own alerts; anything ELSE crashing
    # must not exit silently — a scheduled task's exit code has no audience.
    try:
        return run_fill_check()
    except Exception as exc:
        logger.exception("Fill check crashed")
        alert_crash("basis fill check CRASHED", f"{type(exc).__name__}: {exc}", "high")
        return 4


if __name__ == "__main__":
    sys.exit(main())
