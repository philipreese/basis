"""midday_exits.py — the 12:30 ET exits-only executor pass (#960).

On 2026-09-02 the B07 XSP 753/750 bull put swung from a resting profit-target
close (placed 9/1 22:45Z at 1.18, position +$157) to a −$175 loss against a
$78 limit (2.2x) by the time the nightly run looked again at 9/2 22:45Z. The
DAY exit worked the whole session unfilled and expired at the close. Nothing
looked at the position for the 17 hours in between — which is precisely what
ADR-0008's rationale ("the nightly cadence plus defined-risk structure means
positions are never unattended intraday") assumed could not matter. That
sentence is what the day refuted; the ADR carries a dated amendment saying so.

This pass runs weekdays at 12:30 ET and does exactly two things:

1. Re-prices resting exits. A CLOSE order still SUBMITTED and still in the
   broker's open-order book is cancelled and re-issued at the SAME ladder
   rung, re-marked to the current mid. Rung ESCALATION stays a nightly-only
   step (ADR-0011) — the replacement carries `midday_repriced_from` on its
   combo_legs and `executor._layer_a_closes` excludes it from the rung count,
   so a repriced close still gets MAX_CLOSE_RUNGS genuine evenings at the
   market. A close that has already FILLED is never touched: booking fills is
   the evening sync's job and stays there.
2. Fires new closes. `executor._layer_a_closes` — the same function, not a
   midday copy — with `allow_rolls=False` (a roll is an entry) and
   `skip_flatten_scopes=True` (a flatten rides the nightly ladder, ADR-0011).

Every guard the nightly run applies before a close applies here:

- Own `midday_exits` Gateway-tenant lock, and a clean skip if any other
  tenant is live (run_lock.GATEWAY_TENANT_LOCKS).
- `reconciliation.compare_books` FIRST. Drift halts the pass outright and
  nothing is placed — stricter than the nightly run, which halts entries and
  skips only the drifted legs, because a midday pass has no reconciliation
  panel session behind it and no operator expecting to adjudicate at 12:30.
  The one exception is preflight's #840/#953 carve-out, which is load-bearing
  here rather than cosmetic: expected leg quantities come from OPEN POSITIONS,
  and positions are created by the EVENING sync, so a DAY entry that filled at
  this morning's open reads ORPHAN at 12:30 every single time. Drift whose OCC
  rides on a live STAGED/SUBMITTED order's legs is the sync's own work in
  flight; it does not halt, but the affected legs are still skipped through
  the same #407 guard the nightly run uses.
- Control state: HALT_ENTRIES does NOT block exits (supervision.md — exits are
  risk-reducing and are never blocked). FLATTEN_REQUESTED positions are left
  to the nightly ladder.
- Everything inside `_layer_a_closes` unchanged: the stale-mark guard, the
  ladder cap, the PARTIAL latch, the TP-cancel-first discipline, the
  pending-close skip, the fresh re-read before staging.

Charter, by subtraction. This pass does NOT: place entries or rolls, place
TP children, run `run_reconciliation` (no `reconciliation_runs` row — see the
comment at the compare_books call site), sync order states, settle expiries,
latch or clear control state, or write the executor heartbeat. That last one
is load-bearing: the 22:00 dead-man watchdog exists to notice that the
EVENING run did not happen, and a 12:30 pass stamping the heartbeat would
pacify it every day (preflight refuses for the same reason).

Push posture (supervision.md's push-fatigue rule): a push only when the pass
ACTED (a close submitted or re-issued) or HALTED. A pass that looked and found
nothing to do writes one MIDDAY_EXITS_QUIET audit event and pushes nothing.

Exit codes: 0 when the pass ran (or there was nothing to run — holiday), 4
when it could not start (own lock held, or main()'s crash guard), 5 when the
pass completed but its push exhausted its retries (preflight's EXIT_PUSH_FAILED
shape — a scheduled task watching the exit code must not read a lost report as
success).
"""

import asyncio
import datetime
import logging
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from backend.broker import BrokerError, BrokerSession, RefState, SpreadOrder
from backend.calendars import is_trading_day
from backend.database import TRADING_MODE
from backend.dates import market_today
from backend.executor import (
    CLOSE_CONCESSION_PER_RUNG,
    MIDDAY_REPRICE_OF_KEY,
    STALE_MARK_MAX_HOURS,
    TP_CANCEL_CONFIRM_ATTEMPTS,
    TP_CANCEL_CONFIRM_DELAY_S,
    ExecutorRunSummary,
    _audit,
    _layer_a_closes,
    _mismatched_leg_expirations,
    _now,
    _position_id_from_ghost_ref,
)
from backend.gateway_lifecycle import (
    GATEWAY_WARMUP_SECONDS,
    PORT_POLL_TIMEOUT_SECONDS,
    _gateway_endpoint,
    get_free_memory_gb,
    launch_gateway,
    stop_gateway_tree_only,
    wait_for_gateway_port,
    wait_for_port,
)
from backend.market_data import format_occ_symbol
from backend.models import OrderModel, PositionModel, TradingControlModel
from backend.operator import alert_crash, refresh_market_state, refresh_position_values, send_ntfy_with_retry
from backend.preflight import _pending_order_occ_symbols
from backend.reconciliation import (
    DRIFT_LEG_MISSING_KINDS,
    EXTERNAL_CLOSE_KINDS,
    ORPHAN,
    BrokerSnapshot,
    compare_books,
)
from backend.run_lock import acquire_run_lock, other_gateway_tenant_active, release_run_lock
from backend.states import ORDER_PENDING_STATUSES, ORDER_SUBMITTED_STATUS, POSITION_OPEN_STATUS
from backend.trading_control import FLATTEN_REQUESTED, GLOBAL_SCOPE

logger = logging.getLogger(__name__)

# Same contract as preflight's EXIT_PUSH_FAILED (#840): the work happened but
# the operator never got the report, which is distinct from success.
EXIT_PUSH_FAILED = 5

LOCK_NAME = "midday_exits"

# Audit event types this pass writes. Module constants rather than inline
# literals so the set is readable in one place — backend/states.py is
# deliberately NOT the home for these: it is the vocabulary for ORM *status*
# values in query predicates (its tripwire is an AST scan for
# `Model.status == "LITERAL"`), and audit-event names are neither statuses
# nor enumerated by any predicate.
MIDDAY_EXITS_QUIET = "MIDDAY_EXITS_QUIET"  # ran, nothing to do, no push
MIDDAY_EXITS_ACTED = "MIDDAY_EXITS_ACTED"  # ran and submitted/re-issued a close
MIDDAY_EXITS_HALTED = "MIDDAY_EXITS_HALTED"  # refused to trade (urgent; digest.py)
MIDDAY_EXIT_REPRICED = "MIDDAY_EXIT_REPRICED"
MIDDAY_EXIT_REPRICE_SKIPPED = "MIDDAY_EXIT_REPRICE_SKIPPED"
MIDDAY_CANCEL_UNCONFIRMED = "MIDDAY_CANCEL_UNCONFIRMED"


@dataclass
class MiddayResult:
    """What the pass did — the whole input to the push decision."""

    halted: str | None = None  # the refusal reason; None means the pass ran
    closes_placed: list[str] = field(default_factory=list)
    repriced: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # one line per resting exit left alone
    notes: list[str] = field(default_factory=list)

    @property
    def acted(self) -> bool:
        return bool(self.closes_placed or self.repriced)


def compose_midday_push(result: MiddayResult) -> tuple[str, str, str] | None:
    """(title, body, priority), or None when there is nothing worth pushing.

    Pure — tested directly. Titles stay ASCII (#598). The push-fatigue rule
    (supervision.md) is implemented HERE: a quiet pass returns None, so a
    weekday that had no exits to manage is silent rather than training the
    operator to ignore the channel."""
    if result.halted:
        body = "\n".join([result.halted, *result.notes])
        return "basis midday exits: HALTED", body, "urgent"
    if not result.acted:
        return None
    bits = []
    if result.closes_placed:
        bits.append(f"{len(result.closes_placed)} close(s)")
    if result.repriced:
        bits.append(f"{len(result.repriced)} repriced")
    lines = [f"Closed: {ref}" for ref in result.closes_placed]
    lines += [f"Repriced: {line}" for line in result.repriced]
    lines += [f"Left alone: {line}" for line in result.skipped]
    lines += result.notes
    return f"basis midday exits: {', '.join(bits)}", "\n".join(lines), "high"


# ---------------------------------------------------------------------------
# Gateway + broker session (preflight's shape, #827/#852)
# ---------------------------------------------------------------------------


def _open_session(
    broker_factory: Callable[[], BrokerSession],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> tuple[BrokerSession | None, subprocess.Popen | None, float | None, str | None]:
    """(broker, proc, launch_start_time, failure). A failure string means the
    pass halts — with no broker session there is nothing to trade against and
    nothing to compare the books to."""
    start_script = os.getenv("IBC_START_SCRIPT", "")
    if not start_script or not os.path.exists(start_script):
        return None, None, None, f"IBC_START_SCRIPT missing or not found ({start_script or 'unset'})"
    free_memory_gb = get_free_memory_gb()
    launch_start_time = time.time()
    proc = launch_gateway(start_script)
    sleep(GATEWAY_WARMUP_SECONDS)
    host, port = _gateway_endpoint()
    port_res = wait_for_gateway_port(
        host,
        port,
        proc=proc,
        free_memory_gb=free_memory_gb,
        # timeout_seconds=0 = a single probe; wait_for_gateway_port owns the
        # polling loop and the #852 grace window (#884).
        connect_fn=lambda h, p: wait_for_port(h, p, timeout_seconds=0),
        sleep=sleep,
        monotonic=monotonic,
    )
    if not port_res.is_open:
        status = getattr(port_res.status, "value", port_res.status)
        failure = f"IB Gateway API port {host}:{port} never opened within {PORT_POLL_TIMEOUT_SECONDS}s ({status})"
        return None, proc, launch_start_time, failure
    broker = broker_factory()
    try:
        broker.open()
    except BrokerError as exc:
        detail = "; ".join(f"broker API error {code}: {message}" for code, message in exc.api_errors)
        return (
            None,
            proc,
            launch_start_time,
            f"broker session failed to open: {exc}" + (f" ({detail})" if detail else ""),
        )
    return broker, proc, launch_start_time, None


# ---------------------------------------------------------------------------
# Step 1 — resting-exit reprice
# ---------------------------------------------------------------------------


def _mark_is_fresh(pos: PositionModel) -> bool:
    """The #280 stale-mark guard, verbatim in intent: a close limit derived
    from a mark of unknown age chases the market with garbage."""
    if not pos.last_priced_at:
        return False
    try:
        priced = datetime.datetime.fromisoformat(pos.last_priced_at)
    except (ValueError, TypeError):
        return False
    try:
        return (datetime.datetime.now(datetime.UTC) - priced).total_seconds() <= STALE_MARK_MAX_HOURS * 3600
    except TypeError:  # naive timestamp vs aware now (#545 L4)
        return False


def _rung_of(order: OrderModel, siblings: list[OrderModel]) -> int:
    """The ladder rung *order* was placed at.

    Recorded on the order since #960 (`combo_legs["rung"]`) — that is the
    number the close-limit decision was actually made with. The fallback
    re-derives it the way `_layer_a_closes` does, for rows written before the
    stamp existed: how many non-TP, non-rejected, actually-submitted,
    non-midday-replacement closes preceded this one."""
    recorded = (order.combo_legs or {}).get("rung")
    if isinstance(recorded, int):
        return recorded
    return sum(
        1
        for o in siblings
        if o.id != order.id
        and not o.order_ref.endswith(":tp")
        and o.status != "REJECTED"
        and o.submitted_at is not None
        and not (o.combo_legs or {}).get(MIDDAY_REPRICE_OF_KEY)
        and (order.submitted_at is None or o.submitted_at < order.submitted_at)
    )


def _close_limit_price(pos: PositionModel, rung: int) -> float:
    """The ladder price at *rung* against the position's CURRENT mark — the
    same formula _layer_a_closes uses. SELL-the-bag convention: closing a
    credit position pays, closing a debit position receives."""
    concession = 1.0 + CLOSE_CONCESSION_PER_RUNG * rung
    if pos.premium_direction == "CREDIT":
        return round(-pos.current_value_per_share * concession, 2)
    return round(pos.current_value_per_share / concession, 2)


def _close_spread(pos: PositionModel, limit_price: float) -> SpreadOrder:
    """The closing bag mirrors the entry bag; duplicate legs (a BWB body,
    #132) re-aggregate into one combo leg with the summed ratio."""
    leg_counts: dict[tuple[str, str], int] = {}
    for leg in pos.legs:
        key = (
            format_occ_symbol(pos.underlying, leg["expiration"], leg["option_type"], leg["strike"]),
            "SELL" if leg["direction"] == "SHORT" else "BUY",
        )
        leg_counts[key] = leg_counts.get(key, 0) + 1
    legs = tuple((occ, action, n) for (occ, action), n in leg_counts.items())
    return SpreadOrder(legs=legs, quantity=pos.contracts, net_limit_price=limit_price, underlying=pos.underlying)


async def _reprice_resting_exits(
    session: Any,
    broker: BrokerSession,
    result: MiddayResult,
    controls: dict[str, str],
    report: Any,
    drifted_occ: frozenset[str] = frozenset(),
) -> None:
    """Cancel each unfilled resting DAY exit and re-issue it at the current
    mid, same rung. *report* is broker.reconcile()'s verdict, used only as a
    corroborating oracle alongside the open-order book."""
    open_refs = {o.order_ref for o in broker.open_orders()}
    flatten_global = controls.get(GLOBAL_SCOPE) == FLATTEN_REQUESTED
    positions = (await session.execute(select(PositionModel).filter_by(status=POSITION_OPEN_STATUS))).scalars().all()
    for pos in positions:
        if pos.book_id == "B00":
            continue  # legacy/manual book is never traded by the executor
        closes = (
            (await session.execute(select(OrderModel).filter_by(position_id=pos.id, action="CLOSE"))).scalars().all()
        )
        resting = [o for o in closes if not o.order_ref.endswith(":tp") and o.status == ORDER_SUBMITTED_STATUS]
        if not resting:
            continue
        if flatten_global or controls.get(pos.book_id) == FLATTEN_REQUESTED:
            # ADR-0011: a flatten is a limit-order flatten on the NIGHTLY
            # cadence. Re-marking its ladder at midday would be an intraday
            # flatten mechanism by the back door, which #960 did not decide.
            result.notes.append(f"FLATTEN deferred to the nightly ladder (ADR-0011): {pos.id}")
            continue
        if len(resting) > 1:
            # Already two live exits on one position — the thing this pass
            # must never create. Do not add a third; the evening sync and the
            # resolution panel own this.
            await _audit(
                session,
                MIDDAY_EXIT_REPRICE_SKIPPED,
                pos.book_id,
                {"position_id": pos.id, "reason": "multiple live closes", "order_refs": [o.order_ref for o in resting]},
            )
            await session.commit()
            result.skipped.append(f"{pos.id}: multiple live closes — not touched")
            continue
        old = resting[0]
        if old.order_ref not in open_refs or report.state(old.order_ref) is RefState.FILLED:
            # Filled today (or otherwise gone from the book). Booking the fill
            # is the evening sync's job and stays there — the fill path is
            # unchanged by this pass.
            result.skipped.append(f"{old.order_ref}: not resting at the broker — left to the evening sync")
            continue
        leg_occs = {
            format_occ_symbol(pos.underlying, leg["expiration"], leg["option_type"], leg["strike"]) for leg in pos.legs
        }
        hit = leg_occs & drifted_occ
        if hit:
            # #407, the reprice-side mirror of _layer_a_closes' own skip: the
            # broker does not hold (all of) these legs. Re-marking a full-size
            # SELL onto them is a naked short waiting to fill — and the usual
            # explanation at 12:30 is the benign one (this position's own exit
            # filled and the evening sync hasn't booked it), which is equally
            # a reason not to touch the order.
            await _audit(
                session,
                MIDDAY_EXIT_REPRICE_SKIPPED,
                pos.book_id,
                {"position_id": pos.id, "reason": "drifted legs", "drifted": sorted(hit)},
            )
            await session.commit()
            result.skipped.append(f"{pos.id}: legs drifted at the broker — not repriced")
            continue
        if _mismatched_leg_expirations(pos):
            # #761/#691: legs that don't share pos.expiration_date are
            # unreliable for ANY automated close, re-marked or not.
            await _audit(
                session,
                MIDDAY_EXIT_REPRICE_SKIPPED,
                pos.book_id,
                {"position_id": pos.id, "reason": "mismatched leg expirations"},
            )
            await session.commit()
            result.skipped.append(f"{old.order_ref}: mismatched leg expirations")
            continue
        if not _mark_is_fresh(pos):
            # #280: re-marking to a mark of unknown age is worse than leaving
            # last night's limit where it is.
            await _audit(
                session,
                "STALE_MARK_CLOSE_SKIPPED",
                pos.book_id,
                {"position_id": pos.id, "last_priced_at": pos.last_priced_at, "reason": "midday reprice"},
            )
            await session.commit()
            result.skipped.append(f"{old.order_ref}: stale mark")
            continue

        # Cancel first, and confirm (#467): cancelOrder is fire-and-return and
        # IBKR REJECTS a cancel that races a fill. Believing an unconfirmed
        # cancel and placing the replacement is the double-exit this pass
        # exists to avoid creating.
        found = broker.cancel_by_ref(old.order_ref)
        still_open = True
        for attempt in range(TP_CANCEL_CONFIRM_ATTEMPTS):
            still_open = any(o.order_ref == old.order_ref for o in broker.open_orders())
            if not still_open:
                break
            if attempt < TP_CANCEL_CONFIRM_ATTEMPTS - 1:
                await asyncio.sleep(TP_CANCEL_CONFIRM_DELAY_S)
        if still_open:
            # Leave the row SUBMITTED — the nightly sync verdicts it from
            # completed orders — and place nothing.
            await _audit(
                session,
                MIDDAY_CANCEL_UNCONFIRMED,
                pos.book_id,
                {"order_ref": old.order_ref, "found_at_broker": found},
            )
            await session.commit()
            result.notes.append(
                f"CANCEL UNCONFIRMED: {old.order_ref} still at the broker after "
                f"{TP_CANCEL_CONFIRM_ATTEMPTS} checks - not repriced, last night's limit still rests"
            )
            continue
        # Gone from the open-order book is ambiguous — filled orders leave it
        # too. Any execution on this ref means contracts moved: leave the row
        # alone entirely so the evening sync sees exactly what it expects.
        if any(e.order_ref == old.order_ref for e in broker.executions()):
            await _audit(
                session,
                MIDDAY_EXIT_REPRICE_SKIPPED,
                pos.book_id,
                {"order_ref": old.order_ref, "reason": "executions discovered during cancel"},
            )
            await session.commit()
            result.skipped.append(f"{old.order_ref}: filled during the cancel — left to the evening sync")
            continue

        rung = _rung_of(old, list(closes))
        old.status = "CANCELLED"
        old.completed_at = _now()
        await session.commit()

        # The one invariant that must hold before anything is placed: no live
        # exit of any kind — this position's cancelled close, a sibling close,
        # or a resting GTC :tp — may still be at the broker. Re-read rather
        # than trusting the snapshot taken at the top of this function.
        our_refs = {o.order_ref for o in closes}
        live_now = {o.order_ref for o in broker.open_orders()} & our_refs
        if live_now:
            await _audit(
                session,
                MIDDAY_EXIT_REPRICE_SKIPPED,
                pos.book_id,
                {"position_id": pos.id, "reason": "live exit still at the broker", "live_refs": sorted(live_now)},
            )
            await session.commit()
            result.skipped.append(f"{pos.id}: another live exit at the broker — no replacement placed")
            continue

        limit_price = _close_limit_price(pos, rung)
        order_id = f"o_{uuid.uuid4().hex[:8]}"
        ref = f"basis:{pos.book_id}:{order_id}:close"
        spread = _close_spread(pos, limit_price)
        order = OrderModel(
            id=order_id,
            book_id=pos.book_id,
            position_id=pos.id,
            order_ref=ref,
            ib_order_id=None,
            ib_perm_id=None,
            action="CLOSE",
            combo_legs={
                "legs": [
                    {
                        **leg,
                        "occ": format_occ_symbol(pos.underlying, leg["expiration"], leg["option_type"], leg["strike"]),
                    }
                    for leg in pos.legs
                ],
                "quantity": pos.contracts,
                # The replacement inherits the original's exit trigger — the
                # scan that justified the close is not re-run here, and the
                # post-mortem must record why the position actually left.
                "exit_trigger": (old.combo_legs or {}).get("exit_trigger"),
                "rung": rung,
                MIDDAY_REPRICE_OF_KEY: old.order_ref,
            },
            order_type="LIMIT",
            limit_price=limit_price,
            decision_midpoint=limit_price,
            status="STAGED",
            submitted_at=None,
            completed_at=None,
            encumbered_risk=0.0,  # closes reduce risk — no encumbrance
        )
        session.add(order)
        await session.commit()
        try:
            placed = broker.close_spread(spread, ref)
        except BrokerError as exc:
            order.status = "REJECTED"
            order.completed_at = _now()
            # CLOSE_REJECTED, not a midday-specific literal: a close rejection
            # is a close rejection, and anomaly.py's REPEATED_REJECTION
            # counter should see it (see the enumeration sweep in the PR).
            await _audit(session, "CLOSE_REJECTED", pos.book_id, {"order_ref": ref, "error": str(exc)})
            await session.commit()
            result.notes.append(f"REPRICE REJECTED: {ref} - {exc}; the position is briefly unprotected")
            continue
        order.status = "SUBMITTED"
        order.submitted_at = _now()
        order.ib_order_id = placed.order_id
        order.ib_perm_id = placed.perm_id
        await _audit(
            session,
            MIDDAY_EXIT_REPRICED,
            pos.book_id,
            {
                "position_id": pos.id,
                "cancelled_ref": old.order_ref,
                "order_ref": ref,
                "rung": rung,
                "limit": limit_price,
                "previous_limit": old.limit_price,
            },
        )
        await session.commit()
        result.repriced.append(f"{old.order_ref} -> {ref} @ {limit_price:.2f} (rung {rung})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_midday_exits(
    today: datetime.date | None = None,
    broker_factory: Callable[[], BrokerSession] | None = None,
    session_maker: Callable[[], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """The scheduled-task body. Returns a process exit code."""
    # Mode guard (#204), same as run_executor_evening: this places real orders
    # through the PAPER pipeline. The live executor does not exist (ADR-0006).
    if TRADING_MODE != "paper":
        raise RuntimeError(
            f"run_midday_exits is part of the PAPER executor; IBKR_TRADING_MODE={TRADING_MODE!r}. "
            "The live executor does not exist yet (ADR-0006) — refusing to run."
        )
    today = today or market_today()  # market clock, not UTC (#259)
    if not is_trading_day(today):
        logger.info("Market holiday %s - no exits to manage", today.isoformat())
        return 0

    broker_factory = broker_factory or BrokerSession
    if session_maker is None:
        from backend.database import async_session_maker

        session_maker = async_session_maker

    lock = acquire_run_lock(LOCK_NAME)
    if lock is None:
        # A previous midday pass is still running and is doing this work.
        # Silent, like preflight's own-lock case — the pass is not missing.
        logger.warning("midday_exits lock held - another pass is live; aborting this one")
        return 4

    result = MiddayResult()
    proc: subprocess.Popen | None = None
    launch_start_time: float | None = None
    broker: BrokerSession | None = None
    try:
        if other_gateway_tenant_active(LOCK_NAME):
            # Not a quiet skip: at 12:30 no scheduled tenant should be live,
            # and a pass that did not run means positions are unattended for
            # the rest of the session — the exact failure #960 exists to fix.
            result.halted = "another Gateway tenant is active - the midday exit pass did not run"
        else:
            broker, proc, launch_start_time, failure = _open_session(broker_factory, sleep, monotonic)
            if failure is not None or broker is None:
                result.halted = failure or "broker session unavailable"
            else:
                await _run_pass(session_maker, broker, today, result)

        async with session_maker() as session:
            if result.halted:
                await _audit(session, MIDDAY_EXITS_HALTED, None, {"reason": result.halted, "notes": result.notes})
            elif result.acted:
                await _audit(
                    session,
                    MIDDAY_EXITS_ACTED,
                    None,
                    {
                        "closes_placed": result.closes_placed,
                        "repriced": result.repriced,
                        "skipped": result.skipped,
                        "notes": result.notes,
                    },
                )
            else:
                # The quiet pass's ONLY output. Without this row a pass that
                # ran and found nothing is indistinguishable in the ledger
                # from a pass that never fired at all.
                await _audit(session, MIDDAY_EXITS_QUIET, None, {"skipped": result.skipped, "notes": result.notes})
            await session.commit()

        push = compose_midday_push(result)
        if push is None:
            logger.info("Midday exit pass quiet - nothing acted on, nothing pushed")
            return 0
        title, body, priority = push
        logger.info("%s\n%s", title, body)
        if not send_ntfy_with_retry(title, body, priority):
            logger.error("midday exit push failed after retries - report was NOT delivered")
            return EXIT_PUSH_FAILED
        return 0
    finally:
        if broker is not None:
            broker.close()
        # Teardown mirrors preflight (#838/#851): re-check tenancy immediately
        # before the kill, and scope the sweep to processes created at or
        # after this launch so a tenant that went live mid-pass keeps its
        # Gateway.
        if proc is not None or launch_start_time is not None:
            if other_gateway_tenant_active(LOCK_NAME):
                logger.warning("Another Gateway tenant is active - leaving Gateway up (#471/#681/#838)")
            else:
                stop_gateway_tree_only(proc, created_after=launch_start_time)
        release_run_lock(lock)


async def _run_pass(
    session_maker: Callable[[], Any], broker: BrokerSession, today: datetime.date, result: MiddayResult
) -> None:
    """The pass proper, with an open broker session. Sets result.halted rather
    than raising when it refuses to trade."""
    async with session_maker() as session:
        # Reconciliation FIRST, and read-only. compare_books is the pure half
        # of run_reconciliation, so this pass and the evening run share ONE
        # definition of drift and can never disagree about what counts.
        #
        # Deliberately NOT run_reconciliation: that writes a
        # reconciliation_runs row, and the evening run measures its
        # missed-night gap as _market_days_between(last_recon.run_at, today).
        # A 12:30 row would make every evening read a zero-day gap and
        # silently disarm the #542/#650 restore-gap hold on exactly the night
        # a night was actually missed. It would also latch a global
        # HALT_ENTRIES with no reconciliation run for the operator to resolve
        # against. Detection here, adjudication in the evening.
        snapshot = BrokerSnapshot(
            positions=tuple(broker.positions()),
            executions=(),  # backfill is the evening run's job — comparison only
            open_orders=tuple(broker.open_orders()),
        )
        comparison = await compare_books(session, snapshot, today=today.isoformat())

        # #840/#953, the carve-out this pass cannot do without. _expected_leg_
        # quantities is built from OPEN POSITIONS ONLY, and positions are
        # created by the EVENING sync — so a DAY entry that filled at this
        # morning's open reads ORPHAN at 12:30, and a GTC :tp that filled
        # midday reads EXTERNAL_CLOSE, every single time. Without this the
        # pass would halt on most mornings it was built for. Drift whose OCC
        # is carried on a live STAGED/SUBMITTED order's legs is the sync's own
        # work in flight, not a broker-vs-books disagreement; preflight makes
        # exactly this distinction at 14:00 and this reuses its predicate
        # rather than growing a second copy that can drift from it.
        pending_occ = await _pending_order_occ_symbols(session)
        explained, unexplained = [], []
        for drift in comparison.drifts:
            explainable = drift.kind == ORPHAN or drift.kind in EXTERNAL_CLOSE_KINDS
            if explainable and drift.sec_type == "OPT" and drift.key in pending_occ:
                explained.append(drift)
            else:
                unexplained.append(drift)
        if unexplained:
            # Stricter than the nightly run, on purpose: nightly halts entries
            # and skips only the drifted legs, with an operator due to read
            # the digest that evening. At 12:30 nobody is adjudicating, and a
            # full-size close on legs the account may not hold is a naked
            # short waiting to fill. Nothing is placed.
            detail = ", ".join(
                f"{d.kind}: {d.key} (broker {d.broker_qty:g}, books {d.expected_qty:g})" for d in unexplained
            )
            result.halted = f"RECONCILIATION_DRIFT: {len(unexplained)} discrepancies - no exits placed. {detail}"
            return
        if explained:
            result.notes.append(
                f"{len(explained)} broker change(s) pending tonight's sync - not treated as drift; "
                "the affected legs are skipped this pass"
            )
        # Explained or not, a drifted leg is still a leg the books and the
        # broker currently disagree about. Feed them into the SAME #407/#559
        # skips _layer_a_closes already has, and into the reprice guard below:
        # the pass keeps running for every other position instead of stopping
        # the whole session because one entry filled this morning.
        drifted_occ = frozenset(d.key for d in comparison.drifts if d.kind in DRIFT_LEG_MISSING_KINDS)
        drifted_position_ids = frozenset(
            pid
            for d in comparison.drifts
            if d.kind == "GHOST_ORDER"
            for pid in (_position_id_from_ghost_ref(d.key),)
            if pid is not None
        )

        # Fresh quotes. This is what makes the pass a MIDDAY one: the ladder
        # price and every profit/loss verdict below are computed against
        # 12:30 marks, not last night's. Position MTM only — no book ledger
        # writes, no regime readings, no index history.
        repriced_count = await refresh_position_values(session)
        state, telemetry_live = await refresh_market_state(session, today)
        if state is None:
            result.halted = "no market state - the lifecycle scan has nothing to reason from"
            return
        if not repriced_count:
            # Not a halt: every close path downstream re-checks mark
            # freshness (#280) and skips rather than pricing off a stale
            # mark, so an unpriceable midday degrades to "did nothing" with
            # an audited reason per position.
            result.notes.append("quotes unavailable - marks not refreshed; stale-mark guard will skip every close")

        controls = {
            row.scope: row.state
            for row in (await session.execute(select(TradingControlModel).execution_options(populate_existing=True)))
            .scalars()
            .all()
        }
        # HALT_ENTRIES is deliberately NOT consulted as a gate here
        # (supervision.md): "Layer A exit management continues — exits are
        # risk-reducing and are never blocked." The choke point inside the
        # order path enforces the entry side; this pass places no entries.

        # broker.close_spread requires a reconcile() first (broker.py's
        # _require_reconciled) and uses its report for the duplicate-ref
        # guard. This is a pure broker READ — the pass never acts on the
        # verdicts, which stays the evening sync's job.
        pending = (
            (await session.execute(select(OrderModel).filter(OrderModel.status.in_(ORDER_PENDING_STATUSES))))
            .scalars()
            .all()
        )
        report = broker.reconcile([o.order_ref for o in pending])

        await _reprice_resting_exits(session, broker, result, controls, report, drifted_occ)

        # New closes, through the nightly function itself. A position whose
        # exit was just re-issued above now has a resting close and is skipped
        # by _layer_a_closes' own pending-close guard (#405) — the two halves
        # of this pass can never both act on one position.
        summary = ExecutorRunSummary(run_started_at=_now(), run_date=today.isoformat())
        await _layer_a_closes(
            session,
            broker,
            state,
            summary,
            today,
            # readings=None: the B28 regime-flip exit needs today's variant
            # readings, which only persist_regime_readings produces and which
            # this pass deliberately does not run (it is the evening's
            # decision record). With no reading the arm cannot fire, so
            # midday exits are exactly profit target / loss limit / time rule
            # / assignment defense — the scope #960 asked for.
            None,
            telemetry_live,
            drifted_occ,
            drifted_position_ids,
            allow_rolls=False,
            skip_flatten_scopes=True,
        )
        result.closes_placed.extend(summary.closes_placed)
        result.notes.extend(summary.notes)


def main() -> int:
    from backend.run_logging import setup_run_logging

    setup_run_logging("midday_exits")
    try:
        return asyncio.run(run_midday_exits())
    except Exception as exc:
        logger.exception("Midday exit pass crashed")
        alert_crash("basis midday exits CRASHED", f"{type(exc).__name__}: {exc}", "high")
        return 4


if __name__ == "__main__":
    sys.exit(main())
