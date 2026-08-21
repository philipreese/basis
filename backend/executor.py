"""executor.py — the Executor (Paper) nightly pipeline (design §7 item 11, #70).

Order of operations, per the sequencing rule "manage what you hold before
adding risk" and the reconciliation-first mandate:

1. Open the broker session (paper-only guard; unreachable Gateway = audited
   failure, no orders, heartbeat still written — silent non-operation is the
   worst failure mode).
2. Sync order state by orderRef: yesterday's fills become positions with
   book-cash adjustment; cancelled/expired orders release their encumbrance;
   STAGED intents absent at the broker EXPIRE (resolved decision 4 — the
   evening's prices are stale by the next session).
3. Reconciliation (backfill + drift classification). Drift latches a global
   HALT_ENTRIES; exits still run.
4. Market refresh, index history, all regime-variant readings, ntfy HALT poll.
5. Layer A: P1 positions get closing SELL combos at a marketable limit.
6. Layer C per lab book: variant regime → scan → spec → live-quote pricing →
   book gates → stage (encumber) → control check at the choke point → place
   with a GTC profit-taker resting server-side at IBKR.
7. Heartbeat.

Entries are DAY limits placed after hours: they work the NEXT trading
session and IBKR expires them at that session's close — which implements
UNFILLED_ENTRY (entries never rest beyond one session) without a resting
cancel. GTC belongs to profit-taker children only.

Timing note: the nightly cadence means the close-order escalation ladder
advances one rung per evening (mid + growing concession), not per 5 minutes
— the supervision spec's intraday ladder applies to human-initiated FLATTEN.
"""

import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.anomaly import DUPLICATE_ORDER, _market_days_between, check_duplicate_order, run_post_session_anomalies
from backend.book_gates import (
    PENDING_ORDER_STATUSES,
    BookConfig,
    CandidateOrder,
    Envelope,
    credit_book_cash,
    evaluate_book_gates,
    release_order,
    resolve_book_config,
    stage_order,
)
from backend.broker import BrokerError, BrokerSession, FillInfo, RefState, SpreadOrder
from backend.calendars import is_trading_day, stale_calendars
from backend.console import heartbeat_path
from backend.database import TRADING_MODE, async_session_maker
from backend.dates import market_evening_window_start, market_today
from backend.market_data import fetch_options_latest_quotes, format_occ_symbol
from backend.models import (
    AuditEventModel,
    BookModel,
    ClosurePostMortemModel,
    FillModel,
    MarketStateModel,
    OrderModel,
    PlaybookDefinitionModel,
    PlaybookDefinitionSchema,
    PortfolioConfigModel,
    PortfolioConfigSchema,
    PositionModel,
    ReconciliationRunModel,
    TradingControlModel,
)
from backend.observation import calculate_dte, run_lifecycle_scan
from backend.operator import (
    persist_index_history,
    refresh_market_state,
    refresh_position_values,
)
from backend.opportunity import generate_trade_spec, scan_opportunities
from backend.reconciliation import BrokerSnapshot, _backfill_missed_fills, run_reconciliation
from backend.regime_variants import INSUFFICIENT_DATA, persist_regime_readings, underlying_telemetry
from backend.run_lock import RunLock, acquire_run_lock, refresh_run_lock, release_run_lock
from backend.telemetry import telemetry_key
from backend.trading_control import (
    FLATTEN_REQUESTED,
    GLOBAL_SCOPE,
    HALT_ENTRIES,
    TradingHaltedError,
    apply_ntfy_commands,
    assert_entries_allowed,
    set_control,
)

logger = logging.getLogger(__name__)

# The raced decision-grade engines that vote in the B29 consensus gate
# (#316). V4-V6 are observation-only different-modality lenses; widening the
# electorate to them is a config decision for a future arm, not a default.
CONSENSUS_VARIANTS = ("V0", "V1", "V2", "V3")

CLOSE_CONCESSION_PER_RUNG = 0.15  # each evening a close reworks 15% closer to natural
MAX_CLOSE_RUNGS = 5  # beyond this the ladder stops conceding and escalates to a human (#280)
# TP cancel confirmation (#467): IBKR cancels are asynchronous — give the
# broker a few beats to actually drop the order before believing it did.
TP_CANCEL_CONFIRM_ATTEMPTS = 3
TP_CANCEL_CONFIRM_DELAY_S = 1.0
# Liveness escalation (#546): an unconfirmed TP cancel skips the close
# NIGHTLY with only a digest note — no rung consumed, no escalation ever.
# At this many consecutive TP_CANCEL_UNCONFIRMED nights on the same ref,
# say so urgently — a stuck close must not be able to skip silently forever.
TP_CANCEL_STUCK_THRESHOLD = 3
STALE_MARK_MAX_HOURS = 30.0  # a close limit needs a mark fresher than one missed session (#280)
# #535: the expiry-settlement guard is session-aware, not wall-clock — a
# generous absolute ceiling stays as a backstop against calendar bugs only.
STALE_MARK_ABS_CEILING_HOURS = 5 * 24.0


@dataclass(frozen=True)
class BlockedEntry:
    """One blocked entry crossing the executor→digest seam as data — the
    digest formats and groups these; nobody re-parses a string. book_id None
    means the block applies run-wide (e.g. stale telemetry)."""

    book_id: str | None
    reason: str


@dataclass
class ExecutorRunSummary:
    broker_ok: bool = True
    reconciliation: str = "SKIPPED"
    # UTC ISO timestamp of run start (#259): "tonight's events" everywhere is
    # run_at >= this, never a date-prefix match that breaks at UTC midnight.
    run_started_at: str = ""
    # The market date this run ran under (America/New_York, computed once).
    run_date: str = ""
    positions_created: list[str] = field(default_factory=list)
    intents_expired: list[str] = field(default_factory=list)
    closes_placed: list[str] = field(default_factory=list)
    entries_placed: list[str] = field(default_factory=list)
    entries_blocked: list[BlockedEntry] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # #542: order refs whose UNKNOWN broker verdict was HELD (not
    # terminalized) because the reconciliation gap exceeds 1 trading day —
    # reqCompletedOrders/reqExecutions are current-day-window, so a restored
    # backup's pending row that actually filled in the gap must not be
    # buried as CANCELLED/INTENT_EXPIRED on evidence the restore lost.
    restore_gap_held: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _audit(session: AsyncSession, event_type: str, book_id: str | None, payload: dict) -> None:
    session.add(
        AuditEventModel(run_at=_now(), book_id=book_id, event_type=event_type, actor="executor", payload=payload)
    )


async def _abort_if_lock_lost(session: AsyncSession, lock: RunLock, summary: ExecutorRunSummary, phase: str) -> bool:
    """Phase-boundary check (#536): the verify-restore arm of _break_stale
    can strand a fresh holder's lock in the graveyard — a third contender
    then holds the SAME lock this run believes it owns, with nothing else
    to stop two concurrent runs from both placing orders. refresh_run_lock
    returning False means the lock file no longer carries our token — treat
    it as fatal and abort here, before this phase's broker mutations, with
    an audited trail. Does NOT release the lock (we no longer own it;
    release_run_lock's own token check would no-op anyway) — the run just
    stops acting as the sole owner. Returns True when the caller must stop."""
    if refresh_run_lock(lock):
        return False
    summary.notes.append(
        f"⛔ RUN LOCK LOST before {phase} — another run may hold it now; aborting before further broker mutations"
    )
    await _audit(session, "RUN_LOCK_LOST", None, {"phase": phase})
    await session.commit()
    return True


def _write_heartbeat(summary: ExecutorRunSummary) -> None:
    """The dead-man watchdog (#72) checks this file's timestamp."""
    import json

    heartbeat_path().write_text(
        json.dumps(
            {
                "at": _now(),
                "broker_ok": summary.broker_ok,
                "reconciliation": summary.reconciliation,
                "entries_placed": len(summary.entries_placed),
                "closes_placed": len(summary.closes_placed),
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Phase 2 — order-state sync
# ---------------------------------------------------------------------------


async def _stamp_order_status(
    session: AsyncSession, order: OrderModel, new_status: str, *, completed_at: str | None = None
) -> bool:
    """Conditional order-status UPDATE (#466, Audit II R3 F7): the sync loads
    every pending row once, works from a single broker report, and used to
    stamp its verdicts onto the ORM object unconditionally, all flushed by
    one commit at the end. A console terminalization landing mid-sync
    through a DIFFERENT session (e.g. record_external_close's
    acknowledge_cancelled path cancelling a live order, #407) would be
    silently overwritten by that last-write-wins commit — resurrecting a
    pending latch on an already-closed position and contradicting the
    terminalization's own audit row. Only stamp rows still in a pending
    status; callers must skip every downstream side effect (position, cash,
    post-mortem, other audits) when this returns False."""
    values: dict = {"status": new_status}
    if completed_at is not None:
        values["completed_at"] = completed_at
    result = await session.execute(
        update(OrderModel)
        .where(OrderModel.id == order.id, OrderModel.status.in_(PENDING_ORDER_STATUSES))
        .values(**values)
    )
    return result.rowcount > 0


async def _latch_partial(session: AsyncSession, order: OrderModel, fills: list[FillModel] | list[FillInfo]) -> None:
    """Stamp PARTIAL (#283) + halt the book — the ONE latch for every
    fills-on-a-verdicted-row shape (#531): sync CANCELLED/UNKNOWN/OPEN arms
    and Layer A's TP cancel race all route here, so the conditional stamp
    (#466) and the full-fill disagreement marker (#470) can never drift
    between copies. *fills* is FillModel rows (ledger evidence) or FillInfo
    (fresh broker executions) — both carry .quantity. On a lost stamp race
    every side effect is skipped."""
    if not await _stamp_order_status(session, order, "PARTIAL"):
        await _audit(
            session,
            "ORDER_SYNC_SKIPPED_CONCURRENT_WRITE",
            order.book_id,
            {"order_ref": order.order_ref, "attempted": "PARTIAL"},
        )
        return
    order.status = "PARTIAL"
    await _audit(
        session,
        "PARTIAL_FILL",
        order.book_id,
        {"order_ref": order.order_ref, "executions": len(fills)},
    )
    # Full-fill disagreement marker (#470, fix-attacker F3): with #406 a
    # fully-filled entry whose completed-orders verdict was wrongly
    # non-Filled dead-ends in this latch with NO in-band recovery — no
    # position exists to externally close. When the recorded fills already
    # cover the full intended size, say so, so the operator knows which of
    # the two cases they are looking at.
    meta = order.combo_legs or {}
    intended_units = int(meta.get("quantity", 1)) * max(len(meta.get("legs") or []), 1)
    filled_units = sum(f.quantity for f in fills)
    if filled_units >= intended_units:
        await _audit(
            session,
            "PARTIAL_LATCH_FULL_FILL",
            order.book_id,
            {"order_ref": order.order_ref, "filled_units": filled_units, "intended_units": intended_units},
        )
    await set_control(
        session,
        order.book_id,
        HALT_ENTRIES,
        reason=f"PARTIAL_FILL: {order.order_ref} cancelled with {len(fills)} execution(s)",
        actor="anomaly",
    )


async def _sync_order_states(
    session: AsyncSession, broker, summary: ExecutorRunSummary, restore_gap_trading_days: int = 0
) -> None:
    """restore_gap_trading_days > 1 (#542) means reqCompletedOrders/
    reqExecutions cannot possibly see fills from the gap — a restored
    backup's pending row that actually filled while the gap was open reads
    UNKNOWN with no local FillModel evidence either (the restore lost it
    too). Terminalizing on that combination erases real broker state on
    evidence the restore, not the broker, destroyed. Hold those rows
    instead: untouched status, a RESTORE_GAP_UNKNOWN_HELD audit row, and an
    urgent digest note — resolve via the Flex audit / resolution panel, not
    a nightly guess."""
    pending = (
        (await session.execute(select(OrderModel).filter(OrderModel.status.in_(PENDING_ORDER_STATUSES))))
        .scalars()
        .all()
    )
    if not pending:
        broker.reconcile([])
        return
    report = broker.reconcile([o.order_ref for o in pending])
    executions = tuple(broker.executions())
    await _backfill_missed_fills(session, executions)

    # Entries before closes: an entry and its profit-taker child (#258) can
    # both fill the same day, and the child's CLOSE can only settle after the
    # parent's fill has created and linked the position.
    for order in sorted(pending, key=lambda o: o.action != "OPEN"):
        if order.status == "PARTIAL":
            continue  # latched for a human (#283) — never re-processed, never re-alerted
        state = report.state(order.order_ref)
        if state is RefState.FILLED:
            await _order_to_position(session, order, summary)
        elif state is RefState.CANCELLED:
            # A cancelled order that EXECUTED something first is a partial
            # fill (#283, audit M1): booking it at full intended size would
            # corrupt cash and reconciliation both. Latch PARTIAL (keeps its
            # encumbrance), halt the book, and leave correction to a human —
            # the no-auto-adjust principle applies to sizes too.
            fills = (await session.execute(select(FillModel).filter_by(order_id=order.id))).scalars().all()
            if fills:
                await _latch_partial(session, order, list(fills))
                continue
            if not await _stamp_order_status(session, order, "CANCELLED", completed_at=_now()):
                await _audit(
                    session,
                    "ORDER_SYNC_SKIPPED_CONCURRENT_WRITE",
                    order.book_id,
                    {"order_ref": order.order_ref, "attempted": "CANCELLED"},
                )
                continue
            order.status = "CANCELLED"
            order.completed_at = _now()
            await _audit(session, "ORDER_EXPIRED_AT_BROKER", order.book_id, {"order_ref": order.order_ref})
        elif state is RefState.UNKNOWN and order.status == "STAGED":
            # Same evidence, same latch (#531): "crash before submission" is
            # only a hypothesis — a crash AFTER placement leaves the row
            # STAGED too (#481 F10), and a transient open-orders blip can
            # read that genuinely-placed order UNKNOWN on the very rerun
            # night. If anything executed in between, expiring the intent
            # would bury the fills exactly like the sibling arm used to.
            fills = (await session.execute(select(FillModel).filter_by(order_id=order.id))).scalars().all()
            if fills:
                await _latch_partial(session, order, list(fills))
                continue
            if restore_gap_trading_days > 1:
                # #542: reqCompletedOrders/reqExecutions cannot see a fill
                # from the gap, and a restored backup lost the local
                # FillModel evidence too — this UNKNOWN tells us nothing.
                # Hold, don't expire the intent, on evidence the restore
                # destroyed.
                summary.restore_gap_held.append(order.order_ref)
                await _audit(
                    session,
                    "RESTORE_GAP_UNKNOWN_HELD",
                    order.book_id,
                    {"order_ref": order.order_ref, "gap_trading_days": restore_gap_trading_days},
                )
                continue
            # Crash before submission: expire, never resubmit at stale prices.
            if not await _stamp_order_status(session, order, "CANCELLED", completed_at=_now()):
                await _audit(
                    session,
                    "ORDER_SYNC_SKIPPED_CONCURRENT_WRITE",
                    order.book_id,
                    {"order_ref": order.order_ref, "attempted": "CANCELLED"},
                )
                continue
            order.status = "CANCELLED"
            order.completed_at = _now()
            summary.intents_expired.append(order.order_ref)
            await _audit(session, "INTENT_EXPIRED", order.book_id, {"order_ref": order.order_ref})
        elif state is RefState.UNKNOWN:
            # Same evidence, same latch (#470, fix-attacker F4): a GTC TP
            # that partially fills Monday and falls out of Tuesday's
            # reqCompletedOrders window reads UNKNOWN, and this branch used
            # to terminalize it with no fills check — the one route around
            # the PARTIAL latch that left no halt and no human in the loop.
            fills = (await session.execute(select(FillModel).filter_by(order_id=order.id))).scalars().all()
            if fills:
                await _latch_partial(session, order, list(fills))
                continue
            if restore_gap_trading_days > 1:
                # #542: same reasoning as the STAGED arm above — an UNKNOWN
                # verdict outside the completed-orders window, paired with a
                # restore that also lost the local fill evidence, proves
                # nothing. A real entry (broker holds legs, cash never
                # booked) or a real round-trip (broker flat, P&L never
                # booked) both survive as evidence instead of a terminal
                # CANCELLED/ORDER_LOST_AT_BROKER stamp releasing encumbrance
                # on a row that may still be live.
                summary.restore_gap_held.append(order.order_ref)
                await _audit(
                    session,
                    "RESTORE_GAP_UNKNOWN_HELD",
                    order.book_id,
                    {"order_ref": order.order_ref, "gap_trading_days": restore_gap_trading_days},
                )
                continue
            if not await _stamp_order_status(session, order, "CANCELLED", completed_at=_now()):
                await _audit(
                    session,
                    "ORDER_SYNC_SKIPPED_CONCURRENT_WRITE",
                    order.book_id,
                    {"order_ref": order.order_ref, "attempted": "CANCELLED"},
                )
                continue
            order.status = "CANCELLED"
            order.completed_at = _now()
            # A resting order on an expired position vanished WITH its
            # contracts — IB purges both together. Expected, not urgent (#261).
            pos = await session.get(PositionModel, order.position_id) if order.position_id else None
            if pos is not None and pos.expiration_date and pos.expiration_date <= summary.run_date:
                await _audit(session, "ORDER_EXPIRED_AT_BROKER", order.book_id, {"order_ref": order.order_ref})
            else:
                await _audit(
                    session, "ORDER_LOST_AT_BROKER", order.book_id, {"order_ref": order.order_ref, "was": "SUBMITTED"}
                )
        elif state is RefState.OPEN:
            # A PARTIALLY-FILLED order can be STILL RESTING for its
            # remainder (#531, Audit II R4 F1 — the last route around the
            # PARTIAL latch): the OPEN verdict used to leave the row
            # SUBMITTED with no fills check, and on an expiry night
            # _settle_expired then saw no PARTIAL row, booked a FULL-size
            # settlement over the traded contracts, and stamped the
            # fill-bearing TP CANCELLED. Same evidence, same latch — and the
            # latch is what keeps the position out of expiry settlement.
            fills = (await session.execute(select(FillModel).filter_by(order_id=order.id))).scalars().all()
            if fills:
                await _latch_partial(session, order, list(fills))
                continue
            if order.status == "STAGED":
                # Crash AFTER placement but before the SUBMITTED commit
                # (#481 F10): the order genuinely rests at the broker while
                # the row still says intent-only. The pending skip already
                # prevents duplicates, but the rung counter (#420,
                # submitted_at check) never counts it, and its eventual fill
                # lands on a row whose analysis timestamp is null. Promote
                # with a best-effort stamp.
                if await _stamp_order_status(session, order, "SUBMITTED"):
                    order.status = "SUBMITTED"
                    order.submitted_at = order.submitted_at or _now()
                    await _audit(session, "STAGED_ORDER_FOUND_RESTING", order.book_id, {"order_ref": order.order_ref})
                else:
                    await _audit(
                        session,
                        "ORDER_SYNC_SKIPPED_CONCURRENT_WRITE",
                        order.book_id,
                        {"order_ref": order.order_ref, "attempted": "SUBMITTED"},
                    )
            # OPEN + SUBMITTED, no fills: still working its next-session
            # window — leave it counted.
    await session.commit()


def _post_mortem(
    pos: PositionModel, exit_value_per_share: float, exit_trigger: str, exit_date: str
) -> ClosurePostMortemModel:
    """The expectancy evidence row (ADR-0006): every executor-side closure
    writes one, or the Live Gate's per-trade record silently never accrues.
    Realized P&L uses the same convention as the console close endpoint."""
    if pos.premium_direction == "DEBIT":
        realized = (exit_value_per_share - pos.entry_premium) * 100 * pos.contracts
    else:
        realized = (pos.entry_premium - exit_value_per_share) * 100 * pos.contracts
    realized = round(realized, 2)
    outcome = "WIN" if realized > 0.01 else "LOSS" if realized < -0.01 else "BREAKEVEN"
    return ClosurePostMortemModel(
        id=str(uuid.uuid4()),
        position_id=pos.id,
        outcome=outcome,
        realized_pnl=realized,
        actual_underlying_move_pct=0.0,  # not tracked on autonomous exits
        exit_date=exit_date,
        exit_trigger=exit_trigger,
        lesson_tags=[],
        user_override_logged=False,
        playbook_id=pos.playbook_id,
        playbook_version=pos.playbook_version,
    )


async def _settle_expired(session: AsyncSession, summary: ExecutorRunSummary) -> None:
    """Cash-settle OPEN positions whose expiration has passed (#261, audit C4).

    Runs after the fill sync (a final-day close fill must settle as a fill,
    not an expiry) and before reconciliation/Layer A. Settlement value is the
    LAST MARK (current_value_per_share): quotes for expired contracts are
    gone, index_history has no XSP, and the mark came from real option quotes
    on the final priced evening. A mark carries residual time value, so
    credit buy-backs settle slightly rich — a conservative expectancy bias.
    Any order still resting on the position died with its contracts at IB."""
    cutoff = summary.run_date
    rows = (await session.execute(select(PositionModel).filter_by(status="OPEN"))).scalars().all()
    # Belt-and-braces for #469: a PARTIAL row a human terminalized via the
    # resolution panel no longer trips the PARTIAL-row guard below, but the
    # position's true filled size is STILL unknown — settling full
    # pos.contracts fabricates cash for contracts the broker already closed,
    # on exactly the night the PARTIAL_DRIFT halt goes reconciliation-neutral.
    # resolve_partial_order now refuses while the position is OPEN, so this
    # only fires on pre-fix data — but money guards don't get to assume that.
    terminalized_partial_refs = {
        ev.payload.get("order_ref")
        for ev in (
            await session.execute(select(AuditEventModel).filter_by(event_type="RESOLUTION_PARTIAL_TERMINALIZED"))
        ).scalars()
        if ev.payload
    }
    settled = 0
    for pos in rows:
        if pos.book_id == "B00" or not pos.expiration_date or pos.expiration_date > cutoff:
            continue
        resting = (
            (
                await session.execute(
                    select(OrderModel).filter(
                        OrderModel.position_id == pos.id, OrderModel.status.in_(PENDING_ORDER_STATUSES)
                    )
                )
            )
            .scalars()
            .all()
        )
        # Audit II (#348): a PARTIAL order means the position's true filled
        # size is UNKNOWN — settling full size fabricates cash, and stamping
        # the latch CANCELLED would erase the very flag the human resolves
        # by (#283). Leave everything untouched and keep saying so nightly
        # until the partial is resolved through the reconciliation panel.
        partial = [o for o in resting if o.status == "PARTIAL"]
        if partial:
            refs = ", ".join(o.order_ref for o in partial)
            summary.notes.append(
                f"⚠ EXPIRY SETTLEMENT BLOCKED: {pos.id} expired with PARTIAL order(s) [{refs}] — "
                "true size unknown; resolve the partial before this position can settle"
            )
            await _audit(
                session,
                "EXPIRY_SETTLEMENT_BLOCKED_PARTIAL",
                pos.book_id,
                {"position_id": pos.id, "order_refs": [o.order_ref for o in partial]},
            )
            continue
        if terminalized_partial_refs:
            pos_refs = set(
                (await session.execute(select(OrderModel.order_ref).filter_by(position_id=pos.id))).scalars()
            )
            hit_refs = sorted(terminalized_partial_refs & pos_refs)
            if hit_refs:
                summary.notes.append(
                    f"⚠ EXPIRY SETTLEMENT BLOCKED: {pos.id} expired with resolved-PARTIAL history "
                    f"[{', '.join(hit_refs)}] — true filled size unknown; settle it via the resolution "
                    "panel (external close) at the real settlement value"
                )
                await _audit(
                    session,
                    "EXPIRY_SETTLEMENT_BLOCKED_PARTIAL_HISTORY",
                    pos.book_id,
                    {"position_id": pos.id, "order_refs": hit_refs},
                )
                continue
        # Staleness guard (#415, session-aware since #535): "the last mark"
        # is only a defensible settlement value when it is the FINAL priced
        # evening's mark. This runs BEFORE refresh_position_values (expired
        # contracts don't quote), so on every holiday-preceded expiry (e.g.
        # Thanksgiving Thursday — heartbeat-only, no pricing) the mark is
        # legitimately dated the PREVIOUS TRADING evening — a fixed 30h
        # wall-clock budget guaranteed a false block there every time. Fresh
        # now means "on/after the previous trading session" (<=1 trading day
        # old), with a generous absolute ceiling as a backstop against
        # calendar bugs, not the primary test.
        mark_ok = False
        if pos.last_priced_at:
            try:
                priced = datetime.fromisoformat(pos.last_priced_at)
                within_session = _market_days_between(pos.last_priced_at, cutoff) <= 1
                within_ceiling = (datetime.now(UTC) - priced).total_seconds() <= STALE_MARK_ABS_CEILING_HOURS * 3600
                mark_ok = within_session and within_ceiling
            except (ValueError, TypeError):
                # #545 L4: a naive timestamp row raises TypeError on the
                # aware-minus-naive subtraction, not ValueError — uncaught,
                # it crashed the whole run over one bad row (fail-loud, but
                # a whole night lost). Treat it as stale, same as unparseable.
                mark_ok = False
        if not mark_ok:
            summary.notes.append(
                f"⚠ EXPIRY SETTLEMENT BLOCKED: {pos.id} expired but its mark is stale "
                f"(last priced {pos.last_priced_at or 'never'}) — settle it via the resolution panel "
                "(external close) at the real settlement value"
            )
            await _audit(
                session,
                "EXPIRY_SETTLEMENT_BLOCKED_STALE_MARK",
                pos.book_id,
                {"position_id": pos.id, "last_priced_at": pos.last_priced_at},
            )
            continue
        value = pos.current_value_per_share
        # Conditional transition (#463, Audit II R3 F3): this loop runs off
        # a run-start OPEN snapshot (`rows`, above) — a position an operator
        # externally closed mid-run must not also settle here. The UPDATE is
        # the real guard: it only flips rows still OPEN, so a row already
        # moved by a concurrent close matches zero and is skipped rather
        # than double-booking cash and a duplicate post-mortem.
        result = await session.execute(
            update(PositionModel)
            .where(PositionModel.id == pos.id, PositionModel.status == "OPEN")
            .values(status="EXPIRED")
        )
        if result.rowcount == 0:
            summary.notes.append(
                f"⚠ EXPIRY SETTLEMENT SKIPPED: {pos.id} was closed concurrently — not settling at expiry"
            )
            await _audit(session, "EXPIRY_SETTLEMENT_SKIPPED_CONCURRENT_CLOSE", pos.book_id, {"position_id": pos.id})
            continue
        await credit_book_cash(
            session, pos.book_id, (value if pos.premium_direction == "DEBIT" else -value) * 100 * pos.contracts
        )
        pos.status = "EXPIRED"
        session.add(_post_mortem(pos, value, "EXPIRY", cutoff))
        for stale in resting:
            stale.status = "CANCELLED"
            stale.completed_at = _now()
            await _audit(session, "ORDER_EXPIRED_AT_BROKER", pos.book_id, {"order_ref": stale.order_ref})
        await _audit(
            session,
            "POSITION_EXPIRED",
            pos.book_id,
            {"position_id": pos.id, "settled_value_per_share": value, "expiration": pos.expiration_date},
        )
        settled += 1
    if settled:
        summary.notes.append(f"{settled} position(s) cash-settled at expiry (at last mark)")
    await session.commit()


async def _order_to_position(session: AsyncSession, order: OrderModel, summary: ExecutorRunSummary) -> None:
    """A filled entry order becomes a PositionModel; a filled close order
    closes its position. Book cash adjusts by the order's limit economics
    (fill-price refinement rides on the fills ledger; positions reprice
    nightly from live quotes)."""
    meta = order.combo_legs or {}
    quantity = int(meta.get("quantity", 1))
    book = await session.get(BookModel, order.book_id)

    # Conditional order-status stamp (#466, Audit II R3 F7), guarded FIRST:
    # a console terminalization landing on THIS order between the sync's
    # snapshot and here (e.g. record_external_close's acknowledge_cancelled
    # path) must not be overwritten by this fill's FILLED verdict, and none
    # of the downstream side effects below (position, cash, post-mortem)
    # should run for an order that already left the pending lifecycle.
    if not await _stamp_order_status(session, order, "FILLED", completed_at=_now()):
        await _audit(
            session,
            "ORDER_SYNC_SKIPPED_CONCURRENT_WRITE",
            order.book_id,
            {"order_ref": order.order_ref, "attempted": "FILLED"},
        )
        return
    order.status = "FILLED"
    order.completed_at = _now()

    if order.action == "CLOSE":
        pos = await session.get(PositionModel, order.position_id) if order.position_id else None
        closed_here = False
        if pos is not None:
            # Conditional transition (#463, Audit II R3 F3): the `pos.status
            # == "OPEN"` check below reads the (possibly stale, #464-class)
            # identity map. The UPDATE is the real guard — it only flips a
            # row still OPEN in the DB, so a position an operator closed
            # externally moments earlier matches zero rows and this fill
            # falls into the CLOSE_FILL_ON_NON_OPEN branch below instead of
            # double-booking cash and a duplicate post-mortem.
            result = await session.execute(
                update(PositionModel)
                .where(PositionModel.id == pos.id, PositionModel.status == "OPEN")
                .values(status="CLOSED")
            )
            closed_here = result.rowcount > 0
            if not closed_here:
                # Lost the race (or was already non-OPEN): pos.status in the
                # identity map is stale relative to the DB now — refresh so
                # the audit event below reports the true status, not "OPEN".
                await session.refresh(pos, ["status"])
        if closed_here:
            pos.status = "CLOSED"
            # The exit price IS the final mark (#280, audit H4): console
            # realized P&L recomputes from current_value_per_share, which
            # must agree with the post-mortem, not a stale quote.
            pos.current_value_per_share = abs(order.limit_price)
            pos.last_priced_at = _now()
            # Every executor closure writes its expectancy row (#261);
            # the trigger was stamped when the close was staged.
            exit_date = summary.run_date or market_today().isoformat()
            session.add(_post_mortem(pos, abs(order.limit_price), meta.get("exit_trigger", "MANUAL"), exit_date))
            if book is not None:
                # SELL-the-bag convention: the close's limit_price IS the
                # signed cash flow per share — negative when buying back a
                # credit spread (cash out), positive when selling out of a
                # debit spread (cash in). The old `* -1` inverted this and
                # CREDITED every buy-back cost, inflating the book by 2× the
                # exit value per close (#257). Cash moves ONLY on this
                # OPEN→CLOSED transition (#342): a fill landing on an
                # already-closed position means something else (an operator
                # external-close resolution) booked the exit first, and
                # applying the cash again would double-count it.
                await credit_book_cash(session, order.book_id, order.limit_price * 100 * quantity)
        else:
            summary.notes.append(
                f"⚠ CLOSE FILL ON NON-OPEN POSITION: {order.order_ref} (position "
                f"{order.position_id or '?'} is {pos.status if pos else 'MISSING'}) — cash NOT applied; "
                "verify the book balance against the resolution that closed it first"
            )
            await _audit(
                session,
                "CLOSE_FILL_ON_NON_OPEN",
                order.book_id,
                {
                    "order_ref": order.order_ref,
                    "position_id": order.position_id,
                    "position_status": pos.status if pos else None,
                },
            )
        await _audit(session, "CLOSE_FILLED", order.book_id, {"order_ref": order.order_ref})
        return

    pos_id = f"pos_{order.id}"
    if await session.get(PositionModel, pos_id) is None:
        legs = meta.get("legs", [])
        net = order.limit_price  # negative = credit
        journal_extra = {}
        if meta.get("rolled_from"):
            # Roll lineage (#318): the analysis joins a rolled chain here.
            journal_extra["rolled_from"] = meta["rolled_from"]
        max_loss_ps = order.encumbered_risk / (100 * quantity) if quantity else 0.0
        session.add(
            PositionModel(
                id=pos_id,
                underlying=meta.get("underlying", "?"),
                strategy_type=meta.get("strategy_type", "?"),
                legs=[
                    {
                        "option_type": leg["option_type"],
                        "direction": leg["direction"],
                        "strike": leg["strike"],
                        "expiration": leg["expiration"],
                        "delta": 0.0,
                        "theta": 0.0,
                        "vega": 0.0,
                        "gamma": 0.0,
                    }
                    for leg in legs
                ],
                entry_date=market_today().isoformat(),
                expiration_date=meta.get("expiration_date", ""),
                entry_premium=abs(net),
                premium_direction="CREDIT" if net < 0 else "DEBIT",
                current_value_per_share=abs(net),
                contracts=quantity,
                max_profit=abs(net) if net < 0 else 999999.0,
                max_loss=max_loss_ps,
                notes=f"Executor entry {order.order_ref}",
                rolls=int(meta.get("rolls", 0)),
                status="OPEN",
                journal={
                    "core_thesis_rationale": f"Autonomous entry per playbook (order {order.order_ref})",
                    "structural_invalidation": "Playbook exit rules govern",
                    "expected_underlying_move_pct": 0.0,
                    "pre_trade_emotional_state": "Calm",
                    "pre_trade_confidence_rating": 3,
                    # The regime this entry was decided under (B28, #254).
                    "entry_regime": meta.get("entry_regime", ""),
                    **journal_extra,
                },
                playbook_id=meta.get("playbook_id"),
                playbook_version=meta.get("playbook_version"),
                playbook_snapshot=meta.get("playbook_snapshot"),
                # The config fingerprint this trade was DECIDED under (#284,
                # #534): the ORDER stamped it at stage time — a seed-sync
                # landing between stage and fill (any process start runs
                # init_db) must not re-attribute the trade to a config that
                # never decided it. Book hash is only the legacy fallback.
                config_hash=order.config_hash or (book.config_hash if book is not None else None),
                book_id=order.book_id,
            )
        )
        order.position_id = pos_id
        rolled_from_id = meta.get("rolled_from")
        if rolled_from_id:
            # #483: the roll latch is normally stamped atomically with the
            # SUBMITTED commit (#421, roll_source in _try_place_entry) — but
            # a crash between placeOrder and that commit leaves the source
            # position's journal unstamped even though the roll order
            # genuinely rests (and can fill) at the broker. Stamping it AGAIN
            # here, whenever the sync discovers a rolled-from fill, makes the
            # latch durable regardless of which process observes the fill
            # first — the same-night atomic commit, or a later night's sync
            # picking up a DAY order that filled the next morning. Without
            # this, the source position's own time-exit close can keep
            # laddering on later nights with no latch in sight, and the roll
            # arm stages a SECOND roll entry from the same original.
            source = await session.get(PositionModel, rolled_from_id)
            if source is not None:
                source.journal = {**(source.journal or {}), "rolled_to_ref": order.order_ref}
        # Adopt the profit-taker child (#258): it was staged before the
        # position existed, so its fill can only settle once it knows whose
        # exit it is.
        tp = (
            await session.execute(select(OrderModel).filter_by(order_ref=f"{order.order_ref}:tp"))
        ).scalar_one_or_none()
        if tp is not None:
            tp.position_id = pos_id
        await credit_book_cash(session, order.book_id, -net * 100 * quantity)  # credit received (or debit paid)
        summary.positions_created.append(pos_id)
        await _audit(session, "ENTRY_FILLED", order.book_id, {"order_ref": order.order_ref, "position_id": pos_id})
    else:
        # Idempotent replay (#481 F11): the position already exists — a
        # prior run created it and crashed before this row's FILLED commit.
        # Correctly a no-op for cash and position, but a silent one hid the
        # replay from the audit trail entirely.
        await _audit(
            session, "ENTRY_FILL_REPLAYED", order.book_id, {"order_ref": order.order_ref, "position_id": pos_id}
        )


# ---------------------------------------------------------------------------
# Phase 5 — Layer A closes
# ---------------------------------------------------------------------------


async def _layer_a_closes(
    session: AsyncSession,
    broker,
    state: MarketStateModel,
    summary: ExecutorRunSummary,
    today: date,
    readings: dict[str, str] | None = None,
    telemetry_live: bool = True,
    drifted_occ: frozenset[str] = frozenset(),
) -> bool:
    """Returns False when an order-path BrokerError hit a roll ENTRY —
    the run must then skip Layer C (design §3.2, #421)."""
    entries_ok = True
    open_positions = (await session.execute(select(PositionModel).filter_by(status="OPEN"))).scalars().all()
    # Non-SPY-scale closes for the ex-div assignment defense (#130).
    non_spy = sorted({p.underlying for p in open_positions if p.underlying not in ("SPY", "XSP")})
    prices, _, _ = await underlying_telemetry(session, non_spy)
    # FLATTEN_REQUESTED (#281): the kill switch's third state finally does
    # something — every OPEN position in a flattened scope closes tonight,
    # regardless of what the lifecycle scan thinks. Entries in that scope are
    # already blocked (any non-ACTIVE state fails the choke point).
    # populate_existing (#464, #546 F8): a row already in this session's
    # identity map (e.g. this run's own sync latching HALT_ENTRIES on a
    # book earlier tonight) must not shadow a console FLATTEN_REQUESTED
    # posted mid-run on that same scope — matching the choke-point read's
    # own fix (trading_control.get_control_state).
    controls = {
        row.scope: row.state
        for row in (await session.execute(select(TradingControlModel).execution_options(populate_existing=True)))
        .scalars()
        .all()
    }
    flatten_global = controls.get(GLOBAL_SCOPE) == FLATTEN_REQUESTED
    book_configs: dict[str, BookConfig] = {}
    for pos in open_positions:
        if pos.book_id == "B00":
            continue  # legacy/manual book is never traded by the executor
        if flatten_global or controls.get(pos.book_id) == FLATTEN_REQUESTED:
            # Same ladder, same stale-mark guard as every other close — a
            # flatten is a limit order placed tonight, not a market order.
            scope = GLOBAL_SCOPE if flatten_global else pos.book_id
            scan = {"priority": "P1_FLATTEN", "reason": f"FLATTEN_REQUESTED on {scope}"}
        else:
            scan = run_lifecycle_scan(
                pos.to_schema(),
                current_regime=state.current_regime,
                spy_price=state.spy_price,
                catalyst_dates=state.catalyst_dates or [],
                today=today,
                underlying_prices=prices,
            )
        if not scan["priority"].startswith("P1"):
            # B28's regime-flip exit (#254): a flagged book closes positions
            # whose current variant regime left the state they were entered
            # under — the exit-side question no entry gate can ask.
            if pos.book_id not in book_configs:
                book = await session.get(BookModel, pos.book_id)
                book_configs[pos.book_id] = resolve_book_config(book.config if book else None)
            cfg = book_configs[pos.book_id]
            entry_regime = (pos.journal or {}).get("entry_regime") or ""
            current = (readings or {}).get(cfg.variant or "V0")
            # Mandatory time exit (#260, audit C3): the scan classifies the
            # DTE rule as P2 ("review") — right for the manual workbench,
            # meaningless in an unattended pipeline where nobody reviews.
            # "Mandatory" means the executor closes. The threshold comes from
            # the position's own frozen playbook snapshot, so per-book exit
            # overrides (B26's 75% PT arm, a future DTE arm) are honored.
            exit_dte = ((pos.playbook_snapshot or {}).get("exit_rules") or {}).get("mandatory_exit_dte", 21)
            dte = calculate_dte(pos.expiration_date, today)
            if (
                cfg.exit_on_regime_flip
                and entry_regime
                and current
                and current != INSUFFICIENT_DATA
                and current != entry_regime
            ):
                scan = {
                    "priority": "P1_REGIME_FLIP",
                    "reason": f"REGIME_FLIP: entered under {entry_regime}, now {current}",
                }
            elif dte <= exit_dte:
                scan = {
                    "priority": "P1_TIME_EXIT",
                    "reason": f"TIME_EXIT: {dte} DTE <= mandatory {exit_dte} DTE",
                }
            else:
                continue
        # Drift skip (#407): tonight's reconciliation says the broker does
        # not hold (all of) these legs — EXTERNAL_CLOSE or PARTIAL_DRIFT.
        # A full-size close on legs the account no longer holds is a naked
        # short waiting to fill; the books get fixed through resolution, not
        # by re-selling the bag. Marks come from market data, so the stale-
        # mark guard below cannot catch this.
        leg_occs = {
            format_occ_symbol(pos.underlying, leg["expiration"], leg["option_type"], leg["strike"]) for leg in pos.legs
        }
        hit = leg_occs & drifted_occ
        if hit:
            await _audit(
                session,
                "CLOSE_SKIPPED_DRIFTED_LEGS",
                pos.book_id,
                {"position_id": pos.id, "drifted": sorted(hit), "reason": scan["reason"]},
            )
            await session.commit()
            continue
        # Stale-mark guard (#280, audit M3): entries are stale-guarded, exits
        # were not — a close limit derived from a mark of unknown age chases
        # the market with garbage. Skip the close, alert, retry tomorrow once
        # repricing works. (Tonight's reprice ran BEFORE Layer A, so a fresh
        # mark is minutes old; anything beyond one missed session is stale.)
        mark_age_ok = False
        if pos.last_priced_at:
            try:
                priced = datetime.fromisoformat(pos.last_priced_at)
                mark_age_ok = (datetime.now(UTC) - priced).total_seconds() <= STALE_MARK_MAX_HOURS * 3600
            except (ValueError, TypeError):
                # #545 L4: see the matching guard above — a naive timestamp
                # raises TypeError, not ValueError, on the aware subtraction.
                mark_age_ok = False
        if not mark_age_ok:
            await _audit(
                session,
                "STALE_MARK_CLOSE_SKIPPED",
                pos.book_id,
                {"position_id": pos.id, "last_priced_at": pos.last_priced_at, "reason": scan["reason"]},
            )
            await session.commit()
            continue
        prior_closes = (
            (await session.execute(select(OrderModel).filter_by(position_id=pos.id, action="CLOSE"))).scalars().all()
        )
        tp_rows = [o for o in prior_closes if o.order_ref.endswith(":tp")]
        # Pending-close skip (#405): every pass through this loop mints a fresh
        # uuid ref, so neither duplicate guard can catch a re-run — a same-
        # evening catch-up after a crash would stage a SECOND live close on the
        # same legs, and both DAY orders can fill next session. If a non-TP
        # close is already resting, this position's exit is in flight: skip.
        resting = [o for o in prior_closes if not o.order_ref.endswith(":tp") and o.status in PENDING_ORDER_STATUSES]
        if resting:
            await _audit(
                session,
                "CLOSE_ALREADY_PENDING",
                pos.book_id,
                {"position_id": pos.id, "order_refs": [o.order_ref for o in resting], "reason": scan["reason"]},
            )
            await session.commit()
            continue
        # Rungs are MARKET attempts (#420): a REJECTED row never reached the
        # broker and a crash-expired intent (CANCELLED with no submitted_at)
        # never rested — counting them starts real concessions deeper than
        # intended and can exhaust the ladder without MAX_CLOSE_RUNGS genuine
        # sessions at the market.
        rung = sum(
            1
            for o in prior_closes
            if not o.order_ref.endswith(":tp") and o.status != "REJECTED" and o.submitted_at is not None
        )
        # Ladder cap (#280): concessions grew without bound — beyond
        # MAX_CLOSE_RUNGS evenings the market is telling us something a
        # bigger concession won't fix. Stop conceding, escalate to a human.
        if rung >= MAX_CLOSE_RUNGS:
            await _audit(
                session,
                "CLOSE_LADDER_EXHAUSTED",
                pos.book_id,
                {"position_id": pos.id, "rungs": rung, "reason": scan["reason"]},
            )
            await session.commit()
            continue
        # PARTIAL-aware (#413), same premise as #348: a PARTIAL order means
        # the position's true filled size is UNKNOWN, so a full-pos.contracts
        # close would over-close into naked exposure. A PARTIAL non-TP close
        # is caught by the pending skip above (PARTIAL is a pending status);
        # a PARTIAL TP must equally block staging — and never be "cancelled"
        # over (the latch is for a human, the sync never re-processes it).
        if any(tp.status == "PARTIAL" for tp in tp_rows):
            await _audit(
                session,
                "CLOSE_SKIPPED_PARTIAL_TP",
                pos.book_id,
                {"position_id": pos.id, "reason": scan["reason"]},
            )
            await session.commit()
            continue
        # The resting GTC profit-taker must come down before a manual close
        # goes up (#258) — two live exits on the same legs is a double-close
        # waiting to happen. Cancel-first: if the close placement then fails,
        # the position is briefly unprotected and Layer A retries tomorrow.
        # The TP row is not an escalation rung — it never chased the market.
        partial_tp = False
        unconfirmed_tp = False
        for tp in tp_rows:
            if tp.status in PENDING_ORDER_STATUSES:
                found = broker.cancel_by_ref(tp.order_ref)
                # Same-day partial executions (#413): the GTC TP can have
                # executed PART of the position this morning (fills were
                # backfilled by tonight's sync) while still resting. Stamping
                # CANCELLED would bury that — latch PARTIAL exactly like the
                # sync's cancelled-with-fills branch, halt the book, and do
                # NOT stage a close on a position of unknown size.
                fills = (await session.execute(select(FillModel).filter_by(order_id=tp.id))).scalars().all()
                if fills:
                    # Shared latch (#531): the conditional stamp (#466) and
                    # the full-fill disagreement marker (#470) apply here
                    # exactly as in the sync — a TP that FULLY filled is a
                    # healthy exit wearing the latch, and the operator needs
                    # the marker to tell the two cases apart. Skip staging
                    # either way: even a lost stamp race means someone else
                    # (resolution) just terminalized this row mid-run.
                    await _latch_partial(session, tp, list(fills))
                    partial_tp = True
                    continue
                # Cancel confirmation (#467, Audit II R3 F6): cancelOrder is
                # fire-and-return, and IBKR REJECTS a cancel that races a
                # fill. Stamping CANCELLED on faith makes the row terminal —
                # the sync never looks again — so a TP that fills anyway
                # becomes an invisible double exit once the replacement close
                # also fills. Confirm the order actually left the book before
                # believing the cancel.
                still_open = True
                for attempt in range(TP_CANCEL_CONFIRM_ATTEMPTS):
                    still_open = any(o.order_ref == tp.order_ref for o in broker.open_orders())
                    if not still_open:
                        break
                    if attempt < TP_CANCEL_CONFIRM_ATTEMPTS - 1:
                        await asyncio.sleep(TP_CANCEL_CONFIRM_DELAY_S)
                if still_open:
                    # PendingCancel that never resolved, or a rejected cancel.
                    # Leave the row SUBMITTED — the nightly sync verdicts it
                    # from completed orders — and stage NO close tonight: the
                    # one thing that must not exist is two live exits.
                    unconfirmed_tp = True
                    summary.notes.append(
                        f"⚠ TP CANCEL UNCONFIRMED: {tp.order_ref} still at the broker after "
                        f"{TP_CANCEL_CONFIRM_ATTEMPTS} checks — close NOT staged; retrying next session"
                    )
                    await _audit(
                        session,
                        "TP_CANCEL_UNCONFIRMED",
                        pos.book_id,
                        {"order_ref": tp.order_ref, "found_at_broker": found},
                    )
                    # Liveness escalation (#546): count this ref's consecutive
                    # TP_CANCEL_UNCONFIRMED nights — once resolved the row
                    # leaves PENDING and this ref is never revisited, so
                    # every stored occurrence for it is consecutive by
                    # construction. Autoflush applies the row just added
                    # above before this SELECT runs, so it is already
                    # counted here — no separate +1 needed.
                    unconfirmed_history = (
                        (
                            await session.execute(
                                select(AuditEventModel).filter(
                                    AuditEventModel.event_type == "TP_CANCEL_UNCONFIRMED",
                                    AuditEventModel.book_id == pos.book_id,
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    consecutive = sum(1 for e in unconfirmed_history if e.payload.get("order_ref") == tp.order_ref)
                    if consecutive >= TP_CANCEL_STUCK_THRESHOLD:
                        summary.notes.append(
                            f"⛔ TP CANCEL STUCK: {tp.order_ref} unconfirmed {consecutive} nights running — "
                            "cancel it manually at the broker"
                        )
                        await _audit(
                            session,
                            "TP_CANCEL_STUCK",
                            pos.book_id,
                            {"order_ref": tp.order_ref, "consecutive_unconfirmed": consecutive},
                        )
                    continue
                # Gone from the open-order book — which is ambiguous: Filled
                # orders leave it too. Same-day executions the sync hasn't
                # backfilled yet (the FillModel check above only sees fills
                # known BEFORE the cancel) are the tell: any execution on
                # this ref means contracts moved, so latch PARTIAL for a
                # human exactly like the known-fills branch.
                new_execs = [e for e in broker.executions() if e.order_ref == tp.order_ref]
                if new_execs:
                    await _latch_partial(session, tp, new_execs)
                    partial_tp = True
                    continue
                tp.status = "CANCELLED"
                tp.completed_at = _now()
                await _audit(
                    session, "TP_CANCELLED", pos.book_id, {"order_ref": tp.order_ref, "found_at_broker": found}
                )
        if partial_tp or unconfirmed_tp:
            await session.commit()
            continue
        # Fresh re-read immediately before staging (#465, Audit II R3 F4):
        # `open_positions` (top of this function) is a run-start snapshot —
        # an operator's external close recorded mid-run, through a DIFFERENT
        # session, leaves this ORM instance stale. The #407 drift skip above
        # only catches legs tonight's reconciliation (minutes earlier) already
        # knew about; it says nothing about a close recorded in the gap since.
        # populate_existing forces a real SELECT and overwrites the cached
        # instance — this is the LAST check before a real SELL reaches the
        # broker, and a naked short there cannot be taken back.
        fresh = await session.get(PositionModel, pos.id, populate_existing=True)
        if fresh is None or fresh.status != "OPEN":
            await _audit(
                session,
                "CLOSE_SKIPPED_NOT_OPEN",
                pos.book_id,
                {"position_id": pos.id, "status": fresh.status if fresh else "MISSING", "reason": scan["reason"]},
            )
            await session.commit()
            continue
        concession = 1.0 + CLOSE_CONCESSION_PER_RUNG * rung
        # SELL-the-bag convention: closing a credit position pays (negative
        # price); closing a debit position receives (positive price).
        if pos.premium_direction == "CREDIT":
            limit_price = round(-pos.current_value_per_share * concession, 2)
        else:
            limit_price = round(pos.current_value_per_share / concession, 2)
        # The closing bag MIRRORS the entry bag (SHORT leg = SELL, LONG = BUY);
        # the SELL order action on the bag is what reverses the position.
        # Duplicate leg entries (a BWB body stores its ratio expanded, #132)
        # re-aggregate into one combo leg with the summed ratio — IBKR combos
        # take a ratio per conId, not repeated identical legs.
        leg_counts: dict[tuple[str, str], int] = {}
        for leg in pos.legs:
            key = (
                format_occ_symbol(pos.underlying, leg["expiration"], leg["option_type"], leg["strike"]),
                "SELL" if leg["direction"] == "SHORT" else "BUY",
            )
            leg_counts[key] = leg_counts.get(key, 0) + 1
        legs = tuple((occ, action, n) for (occ, action), n in leg_counts.items())
        order_id = f"o_{uuid.uuid4().hex[:8]}"
        ref = f"basis:{pos.book_id}:{order_id}:close"
        spread = SpreadOrder(legs=legs, quantity=pos.contracts, net_limit_price=limit_price, underlying=pos.underlying)
        # The post-mortem trigger travels on the order (#261): the scan that
        # justified this close won't be re-runnable when the fill lands.
        reason = scan["reason"]
        if scan["priority"] == "P1_REGIME_FLIP":
            trigger = "REGIME_FLIP"
        elif scan["priority"] == "P1_TIME_EXIT":
            trigger = "TIME_RULE"
        elif scan["priority"] == "P1_FLATTEN":
            trigger = "MANUAL"  # a human requested the flatten (#281)
        elif reason.startswith("Profit target"):
            trigger = "PROFIT_TARGET"
        elif reason.startswith("Loss limit"):
            trigger = "LOSS_LIMIT"
        else:
            trigger = "ASSIGNMENT_RISK"  # the only remaining P1 (ex-div defense)
        order = OrderModel(
            id=order_id,
            book_id=pos.book_id,
            position_id=pos.id,
            order_ref=ref,
            ib_order_id=None,
            ib_perm_id=None,
            action="CLOSE",
            combo_legs={"legs": [dict(l) for l in pos.legs], "quantity": pos.contracts, "exit_trigger": trigger},
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
            await _audit(session, "CLOSE_REJECTED", pos.book_id, {"order_ref": ref, "error": str(exc)})
            await session.commit()
            continue
        order.status = "SUBMITTED"
        order.submitted_at = _now()
        order.ib_order_id = placed.order_id
        order.ib_perm_id = placed.perm_id
        summary.closes_placed.append(ref)
        await _audit(
            session,
            "CLOSE_SUBMITTED",
            pos.book_id,
            {"order_ref": ref, "reason": scan["reason"], "rung": rung, "limit": limit_price},
        )
        await session.commit()

        # Roll arm (B31, #318): a losing position leaving on the time exit
        # gets a roll-out entry staged alongside its close — same strikes,
        # next cycle. Winners just close (nothing to repair), and the roll
        # chain caps at EXECUTOR_MAX_ROLLS. The entry runs the full normal
        # entry path (quotes, sanity, gates, TP child); if anything blocks
        # it, the close stands alone and the arm degrades to a plain exit.
        cfg = book_configs.get(pos.book_id)
        if scan["priority"] == "P1_TIME_EXIT" and cfg is not None and cfg.roll_time_exits:
            is_loser = (
                pos.current_value_per_share > pos.entry_premium
                if pos.premium_direction == "CREDIT"
                else pos.current_value_per_share < pos.entry_premium
            )
            # One roll attempt per position (#344): the close can rest for
            # several evenings (escalation ladder), and this trigger re-fires
            # on each of them while pos.rolls still counts the ORIGINAL's
            # rolls. The journal stamp written at staging is the latch — an
            # unfilled roll entry (DAY limit) is NOT retried; the arm
            # degrades to a plain time exit.
            already_rolled = "rolled_to_ref" in (pos.journal or {})
            if is_loser and pos.rolls < EXECUTOR_MAX_ROLLS and not already_rolled:
                # Stale-telemetry parity (#350): the roll is an ENTRY, and on
                # a stale night Layer C blocks every ordinary entry — a roll
                # placed off possibly-garbage quotes must not slip through.
                # The close still stands; the arm degrades to a plain exit.
                if not telemetry_live:
                    await _audit(
                        session, "ROLL_SKIPPED", pos.book_id, {"position_id": pos.id, "reason": "stale telemetry"}
                    )
                    await session.commit()
                    continue
                regime = (readings or {}).get(cfg.variant or "V0") or ""
                # A non-reading is not a regime (#350): stamping
                # INSUFFICIENT_DATA into journal.entry_regime would poison
                # the regime-flip exit and the hit-rate analysis.
                entry_regime = "" if regime == INSUFFICIENT_DATA else regime
                if entries_ok and not await _stage_roll_entry(session, broker, pos, summary, today, entry_regime):
                    # Order-path BrokerError on the roll (#421): closes keep
                    # going (exits reduce risk), but no further ENTRIES leave
                    # tonight — the caller also skips Layer C.
                    entries_ok = False
    return entries_ok


# ---------------------------------------------------------------------------
# Phase 6 — Layer C entries per book
# ---------------------------------------------------------------------------


def _book_scan_config(base: PortfolioConfigModel, envelope: Envelope) -> PortfolioConfigSchema:
    """Clone the portfolio config with the book's envelope numbers so the
    Layer C scan gates and the book gates agree (the book gates remain the
    authority; this keeps the scan from pre-blocking at the wrong caps).
    Both layers read the same resolved Envelope, so they can never drift."""
    schema = base.to_schema()
    risk = schema.risk_profile.model_copy(
        update={
            "max_simultaneous_positions": envelope.max_positions,
            "max_capital_deployed_pct": envelope.max_deployed_pct,
            "max_trade_risk_dollars": envelope.basis * envelope.max_loss_pct_per_trade / 100.0,
            "max_trade_risk_pct": envelope.max_loss_pct_per_trade,
        }
    )
    return schema.model_copy(update={"risk_profile": risk})


def _book_playbooks(playbooks: list[PlaybookDefinitionSchema], config: BookConfig) -> list[PlaybookDefinitionSchema]:
    """Apply a book's playbook selection and overrides (#136 experiment arms).

    playbook_ids: optional whitelist — the book scans only those.
    playbook_overrides: optional dot-keyed field overrides applied to every
    selected playbook (e.g. {"execution_specs.target_dte": 24}), revalidated
    through the schema so a bad override fails loudly at scan time, not at
    order time. Both feed the book's config_hash, so every arm is
    fingerprinted (ADR-0003 pattern).
    """
    ids = config.playbook_ids
    selected = [pb for pb in playbooks if not ids or pb.id in ids]
    overrides: dict = dict(config.playbook_overrides)
    # The book's underlying becomes the playbook's ticker (#139), so strike
    # derivation, trend, and IVR all resolve per book (via telemetry_key —
    # XSP proxies to SPY). Placement no longer needs its own substitution.
    if config.underlying:
        overrides["underlying_ticker"] = config.underlying
    if not overrides:
        return selected
    adjusted = []
    for pb in selected:
        data = pb.model_dump()
        for dotted, value in overrides.items():
            node = data
            *path, last = dotted.split(".")
            for key in path:
                node = node[key]
            node[last] = value
        adjusted.append(PlaybookDefinitionSchema(**data))
    return adjusted


async def _layer_c_entries(
    session: AsyncSession,
    broker,
    state: MarketStateModel,
    readings: dict[str, str],
    telemetry_live: bool,
    summary: ExecutorRunSummary,
    today: date,
) -> None:
    if not telemetry_live:
        summary.entries_blocked.append(BlockedEntry(None, "STALE_DATA — live telemetry unavailable, no new entries"))
        await _audit(session, "ENTRIES_BLOCKED_STALE_DATA", None, {"scope": "ALL"})
        await session.commit()
        return

    playbooks = [pb.to_schema() for pb in (await session.execute(select(PlaybookDefinitionModel))).scalars().all()]
    config_model = (await session.execute(select(PortfolioConfigModel).filter_by(id=1))).scalar_one_or_none()
    if config_model is None:
        summary.notes.append("No portfolio config — Layer C skipped")
        return
    books = (
        (await session.execute(select(BookModel).filter(BookModel.status == "ACTIVE", BookModel.id != "B00")))
        .scalars()
        .all()
    )

    configs = {b.id: resolve_book_config(b.config) for b in books}
    # Per-underlying telemetry (#139): prices/SMA20/pseudo-IVR for every
    # non-SPY-scale underlying any active book trades, from index_history.
    non_spy = sorted({u for cfg in configs.values() if (u := cfg.underlying) is not None and telemetry_key(u) != "SPY"})
    prices, smas, pseudo_ivrs = await underlying_telemetry(session, non_spy)

    for book in books:
        book_config = configs[book.id]
        variant = book_config.variant or "V0"
        regime = readings.get(variant)
        if regime is None or regime == INSUFFICIENT_DATA:
            summary.entries_blocked.append(BlockedEntry(book.id, f"variant {variant} reading unavailable"))
            await _audit(session, "ENTRIES_BLOCKED_STALE_DATA", book.id, {"variant": variant})
            await session.commit()
            continue

        # Ensemble-consensus gate (B29, #316): entries only when enough raced
        # engines agree with this book's own reading tonight. Disagreement is
        # the informative early signal — this book converts it into abstention.
        # An INSUFFICIENT_DATA elector counts as DISSENT by design (#356): an
        # engine that cannot read the regime is not agreement, so early on
        # (V1-V3 still warming their history) 3-of-4 behaves like 3-of-3 —
        # deliberate conservatism, not a bug.
        if book_config.require_consensus:
            votes = sum(1 for v in CONSENSUS_VARIANTS if readings.get(v) == regime)
            if votes < book_config.require_consensus:
                summary.entries_blocked.append(
                    BlockedEntry(book.id, f"consensus {votes}/{book_config.require_consensus} on {regime}")
                )
                await _audit(
                    session,
                    "ENTRIES_BLOCKED_NO_CONSENSUS",
                    book.id,
                    {
                        "regime": regime,
                        "votes": votes,
                        "required": book_config.require_consensus,
                        "readings": {v: readings.get(v) for v in CONSENSUS_VARIANTS},
                    },
                )
                await session.commit()
                continue

        book_positions = [
            p.to_schema()
            for p in (await session.execute(select(PositionModel).filter_by(book_id=book.id))).scalars().all()
        ]
        state_schema = state.to_schema().model_copy(
            update={
                "current_regime": regime,
                "underlying_prices": prices,
                "underlying_sma20": smas,
                # Pseudo-IVRs supplement, never overwrite, real IVR entries.
                "underlying_ivrs": {**pseudo_ivrs, **(state.underlying_ivrs or {})},
            }
        )
        scan_config = _book_scan_config(config_model, book_config.envelope)
        scan = scan_opportunities(
            playbooks=_book_playbooks(playbooks, book_config),
            market_state=state_schema,
            positions=book_positions,
            portfolio_config=scan_config,
            today=today,
            # Control books (ADR-0009): B12 ignores the regime gate, B16 the
            # IVR gates — they exist to measure whether those gates earn keep.
            enforce_regime=not book_config.ignore_regime,
            enforce_ivr=not book_config.ignore_ivr,
            book_mode=True,
        )
        if scan.portfolio_blocked:
            await _audit(session, "SCAN_BLOCKED", book.id, {"reason": scan.block_reason})
            await session.commit()
            continue
        for candidate in scan.candidates:
            if not candidate.eligible:
                continue
            spec_result = generate_trade_spec(
                candidate.playbook, state_schema, book_positions, scan_config, contracts=1, today=today
            )
            if spec_result.spec is None:
                await _audit(
                    session,
                    "SPEC_HARD_BLOCKED",
                    book.id,
                    {"playbook": candidate.playbook.id, "blocks": [b.check for b in spec_result.hard_blocks]},
                )
                await session.commit()
                continue
            if not await _try_place_entry(
                session, broker, book, spec_result.spec, candidate.playbook, summary, entry_regime=regime
            ):
                await _audit(session, "ENTRY_PHASE_ABORTED", None, {"after": f"{book.id}:{candidate.playbook.id}"})
                await session.commit()
                return


# A rolled position may itself be rolled, but the chain ends here — beyond
# two rolls the trade is a thesis being defended, not a position being
# managed (same cap as the manual workbench's roll counter, #7).
EXECUTOR_MAX_ROLLS = 2


@dataclass(frozen=True)
class _RollLeg:
    action: str  # BUY | SELL
    option_type: str  # CALL | PUT
    strike: float
    expiration_date: str
    quantity: int  # combo ratio


@dataclass(frozen=True)
class _RollSpec:
    """The synthetic spec a roll feeds through the normal entry path (#318):
    the old position's exact structure, moved to the next cycle."""

    strategy_type: str
    expiration_date: str
    legs: tuple[_RollLeg, ...]
    max_loss_dollars: float
    underlying: str
    # Audit II (#356): the old position's max_loss reflects the OLD credit;
    # the roll fills at a different one, so _try_place_entry recomputes the
    # encumbrance from the new net mid once the legs are priced.
    recompute_max_loss: bool = True


async def _stage_roll_entry(session: AsyncSession, broker, pos, summary, today: date, entry_regime: str) -> bool:
    """Stage the roll-out entry for a position whose time-exit close was just
    submitted: same strikes and ratios, expiry from the position's own frozen
    target_dte recipe. Runs the complete normal entry path — quotes, sanity
    bound, duplicate check, book gates, encumbrance, GTC profit-taker — so a
    roll can never sneak past a gate an ordinary entry would hit."""
    from backend.opportunity import _target_expiration

    snapshot = pos.playbook_snapshot or {}
    if not snapshot:
        await _audit(session, "ROLL_SKIPPED", pos.book_id, {"position_id": pos.id, "reason": "no playbook snapshot"})
        await session.commit()
        return True
    playbook = PlaybookDefinitionSchema(**snapshot)
    target_dte = (snapshot.get("execution_specs") or {}).get("target_dte", 38)
    exp_date, _dte = _target_expiration(today, target_dte, require_after_catalyst=False, catalyst_dates=[])

    # Position legs store ratios expanded into duplicates — re-aggregate.
    leg_counts: dict[tuple[str, str, float], int] = {}
    for leg in pos.legs:
        key = (leg["option_type"], leg["direction"], float(leg["strike"]))
        leg_counts[key] = leg_counts.get(key, 0) + 1
    legs = tuple(
        _RollLeg(
            action="BUY" if direction == "LONG" else "SELL",
            option_type=option_type,
            strike=strike,
            expiration_date=exp_date.isoformat(),
            quantity=n,
        )
        for (option_type, direction, strike), n in leg_counts.items()
    )
    spec = _RollSpec(
        strategy_type=pos.strategy_type,
        expiration_date=exp_date.isoformat(),
        legs=legs,
        max_loss_dollars=pos.max_loss * 100,
        underlying=pos.underlying,
    )
    book = await session.get(BookModel, pos.book_id)
    if book is None:
        return True
    await _audit(
        session,
        "ROLL_STAGED",
        pos.book_id,
        {"position_id": pos.id, "roll_number": pos.rolls + 1, "new_expiry": exp_date.isoformat()},
    )
    await session.commit()
    # Gate-blocked/unpriceable rolls return True and simply retry another
    # night; False is an order-path BrokerError (design §3.2) and the caller
    # aborts every remaining ENTRY tonight (#421) — the broker just errored
    # on the order path, and Layer C would otherwise place entries against
    # it minutes later. The rolled_to_ref latch (#344) is stamped inside
    # _try_place_entry's own SUBMITTED commit via roll_source (#421).
    return await _try_place_entry(
        session,
        broker,
        book,
        spec,
        playbook,
        summary,
        entry_regime=entry_regime,
        extra_meta={"rolls": pos.rolls + 1, "rolled_from": pos.id},
        roll_source=pos,
    )


@dataclass(frozen=True)
class ComboLeg:
    """One leg of a combo order. ratio is the combo multiplier — BWB bodies
    carry 2 (#132); position legs expand it into duplicates separately."""

    occ: str
    action: str  # "BUY" | "SELL"
    direction: str  # "LONG" | "SHORT"
    ratio: int


async def _try_place_entry(
    session: AsyncSession,
    broker,
    book: BookModel,
    spec,
    playbook,
    summary,
    entry_regime: str = "",
    extra_meta=None,
    roll_source=None,
) -> bool:
    """Returns False only when the submission phase must abort (order-path
    broker error, design §3.2); every per-candidate skip returns True.
    entry_regime is stamped into the order meta so the position remembers the
    regime it was entered under (B28's regime-flip exit, #254). extra_meta
    rides into combo_legs — the roll path (#318) uses it for lineage.
    roll_source (#421): the ORIGINAL position being rolled — its
    rolled_to_ref latch is stamped inside the SAME commit as the SUBMITTED
    transition, so a crash between the two can no longer re-arm the latch
    and stage a second roll the next night."""
    cfg = resolve_book_config(book.config)
    underlying = cfg.underlying or spec.underlying
    # Per-playbook dedup (#411): with an always-on playbook and two slots
    # (ADR-0012 amendment), the night after the first lot fills a second lot
    # stages — steady state becomes 2× bleed, and the roll-night slot the
    # amendment reserved is full again. A new lot is allowed only when every
    # open same-playbook position is already in its exit window (its close
    # is imminent — that overlap IS the amendment's purpose).
    if cfg.dedup_playbook_entries:
        exit_dte = playbook.exit_rules.mandatory_exit_dte or 21
        run_day = date.fromisoformat(summary.run_date)
        same_playbook = (
            (
                await session.execute(
                    select(PositionModel).filter_by(book_id=book.id, playbook_id=playbook.id, status="OPEN")
                )
            )
            .scalars()
            .all()
        )
        blocking = [p.id for p in same_playbook if calculate_dte(p.expiration_date, run_day) > exit_dte]
        if blocking:
            summary.entries_blocked.append(BlockedEntry(book.id, f"{playbook.id} dedup (open: {blocking[0]})"))
            await _audit(
                session,
                "ENTRY_BLOCKED_PLAYBOOK_DEDUP",
                book.id,
                {"playbook": playbook.id, "open_positions": blocking, "mandatory_exit_dte": exit_dte},
            )
            await session.commit()
            return True
    legs_meta = []
    combo: list[ComboLeg] = []
    for leg in spec.legs:
        # The spec's strike is already on the underlying's real grid
        # (_STRIKE_INTERVALS — AAPL trades $2.50 strikes). Rounding to an
        # integer here silently moved B30's legs to strikes that don't exist
        # (232.5→232) or reshaped the spread (banker's rounding, #343); OCC
        # symbols carry fractional strikes natively (×1000).
        strike = leg.strike
        occ = format_occ_symbol(underlying, leg.expiration_date, leg.option_type, strike)
        direction = "LONG" if leg.action == "BUY" else "SHORT"
        # Combo ratio: BWB bodies carry quantity 2 (#132); everything else 1.
        ratio = max(1, leg.quantity)
        combo.append(ComboLeg(occ=occ, action=leg.action, direction=direction, ratio=ratio))
        # Position legs expand the ratio into duplicate entries so the
        # reconciliation leg-quantity sum matches the broker exactly.
        legs_meta.extend(
            [
                {
                    "occ": occ,
                    "option_type": leg.option_type,
                    "direction": direction,
                    "strike": float(strike),
                    "expiration": leg.expiration_date,
                }
            ]
            * ratio
        )

    quotes = fetch_options_latest_quotes([leg.occ for leg in combo])
    if any(leg.occ not in quotes for leg in combo):
        summary.entries_blocked.append(BlockedEntry(book.id, f"{playbook.id} unpriceable ({underlying})"))
        await _audit(session, "CANDIDATE_UNPRICEABLE", book.id, {"playbook": playbook.id, "underlying": underlying})
        await session.commit()
        return True
    net_mid = round(sum((quotes[leg.occ] if leg.action == "BUY" else -quotes[leg.occ]) * leg.ratio for leg in combo), 2)
    if net_mid == 0.0:
        await _audit(session, "CANDIDATE_UNPRICEABLE", book.id, {"playbook": playbook.id, "reason": "zero mid"})
        await session.commit()
        return True
    # Quote sanity bound (#282, audit H8): a same-expiry spread's value can
    # never exceed its widest same-type strike span — a mid beyond it is a
    # stale close or a broken quote, and must be skipped, never traded.
    # Calendars (same strike, two expiries) have span 0 → no bound applies.
    spans = []
    for opt_type in ("CALL", "PUT"):
        strikes = [leg["strike"] for leg in legs_meta if leg["option_type"] == opt_type]
        if len(strikes) >= 2:
            spans.append(max(strikes) - min(strikes))
    width_bound = max(spans) if spans else 0.0
    if width_bound and abs(net_mid) >= width_bound:
        await _audit(
            session,
            "CANDIDATE_UNPRICEABLE",
            book.id,
            {"playbook": playbook.id, "reason": f"absurd quote: |{net_mid}| >= {width_bound} width"},
        )
        await session.commit()
        return True

    max_loss_per_share = spec.max_loss_dollars / 100.0
    if getattr(spec, "recompute_max_loss", False) and width_bound:
        # Roll encumbrance (#356): the synthetic roll spec carries the OLD
        # position's max_loss, but the roll fills at ITS OWN credit/debit —
        # a credit spread risks width − credit; a debit spread risks the
        # debit paid. Known span gaps (#421): zero-span structures
        # (calendars, straddles/strangles — width_bound falsy) keep the OLD
        # max_loss, and a BWB's width_bound is its TOTAL span (true risk is
        # smaller). Both err toward OVER-encumbering — conservative, never
        # dangerous — so they stay as-is.
        max_loss_per_share = width_bound - abs(net_mid) if net_mid < 0 else abs(net_mid)

    candidate_order = CandidateOrder(
        book_id=book.id,
        strategy_type=spec.strategy_type,
        expiration_date=spec.expiration_date,
        legs=tuple((leg.occ, leg.direction) for leg in combo),
        max_loss_per_share=max_loss_per_share,
        contracts=1,
    )
    if await check_duplicate_order(
        session, book.id, candidate_order.legs, market_evening_window_start(date.fromisoformat(summary.run_date))
    ):
        # An identical entry already went out tonight — logic bug, not market
        # condition. Block it and latch the global halt (supervision.md).
        summary.entries_blocked.append(BlockedEntry(book.id, f"{playbook.id} DUPLICATE_ORDER"))
        await _audit(session, DUPLICATE_ORDER, book.id, {"playbook": playbook.id})
        await session.commit()
        await set_control(
            session, "GLOBAL", HALT_ENTRIES, reason=f"{DUPLICATE_ORDER}: {playbook.id} in {book.id}", actor="anomaly"
        )
        return True

    decision = await evaluate_book_gates(session, candidate_order)
    if not decision.allowed:
        summary.entries_blocked.append(
            BlockedEntry(book.id, f"{playbook.id} gated ({', '.join(decision.blocked_by())})")
        )
        return True

    order_id = f"o_{uuid.uuid4().hex[:8]}"
    ref = f"basis:{book.id}:{order_id}:open"
    await stage_order(
        session,
        candidate_order,
        order_id=order_id,
        order_ref=ref,
        limit_price=net_mid,
        decision_midpoint=net_mid,
        combo_legs={
            "legs": legs_meta,
            "quantity": 1,
            "strategy_type": spec.strategy_type,
            "expiration_date": spec.expiration_date,
            "underlying": underlying,
            "playbook_id": playbook.id,
            # Frozen contract (#260): the position must be exited under the
            # rules it was ENTERED under, even if the playbook row (or a
            # book's overrides) changes mid-flight. This is the book-resolved
            # playbook — B15/B26-style exit overrides are already applied.
            "playbook_version": playbook.version,
            "playbook_snapshot": playbook.model_dump(),
            "entry_regime": entry_regime,
            **(extra_meta or {}),
        },
    )
    pct = playbook.exit_rules.profit_take_pct / 100.0
    tp_price = round(net_mid * (1 - pct) if net_mid < 0 else net_mid * (1 + pct), 2)
    # The GTC profit-taker child is a REAL order resting at IB (#258, audit
    # C1): it can fill any future morning, and a fill with no row here is
    # invisible to the sync. Its row is written BEFORE placeOrder (#409):
    # place_spread sends BOTH orders, and a crash before the post-placement
    # commit would otherwise leave the GTC child resting at the broker with
    # no DB record — never adopted, never cancelled, a double-close when it
    # fills. Same intent-first discipline as stage_order; a crash before
    # placement expires both rows as intents on the next sync.
    session.add(
        OrderModel(
            id=f"{order_id}_tp",
            book_id=book.id,
            position_id=None,  # linked when the parent's fill creates the position
            order_ref=f"{ref}:tp",
            ib_order_id=None,
            ib_perm_id=None,
            action="CLOSE",
            combo_legs={
                "legs": legs_meta,
                "quantity": 1,
                "strategy_type": spec.strategy_type,
                "exit_trigger": "PROFIT_TARGET",
            },
            order_type="LIMIT",
            limit_price=tp_price,
            decision_midpoint=tp_price,
            status="STAGED",
            submitted_at=None,
            completed_at=None,
            encumbered_risk=0.0,  # closes reduce risk — no encumbrance
        )
    )
    await session.commit()
    spread = SpreadOrder(
        legs=tuple((leg.occ, leg.action, leg.ratio) for leg in combo),
        quantity=1,
        net_limit_price=net_mid,
        underlying=underlying,
    )
    try:
        await assert_entries_allowed(session, book.id)
        placed = broker.place_spread(spread, ref, profit_target_price=tp_price)
    except TradingHaltedError as halt:
        summary.entries_blocked.append(BlockedEntry(book.id, f"{playbook.id} halted ({halt.scope}={halt.state})"))
        await _audit(
            session,
            "WOULD_HAVE_TRADED",
            book.id,
            {"order_ref": ref, "playbook": playbook.id, "halt_scope": halt.scope},
        )
        await release_order(session, order_id, "CANCELLED")
        await release_order(session, f"{order_id}_tp", "CANCELLED")
        return True
    except BrokerError as exc:
        # 162/competing-session policy (#68, design §3.2): a broker error on
        # the ORDER path aborts the rest of the submission phase — never
        # fail-soft where orders are concerned. (Data-path failures already
        # fail soft to stored data upstream.) REPEATED_REJECTION still
        # latches the halt if this recurs across sessions.
        summary.entries_blocked.append(BlockedEntry(book.id, f"{playbook.id} rejected — submission phase aborted"))
        await _audit(session, "ORDER_REJECTED", book.id, {"order_ref": ref, "error": str(exc)})
        await release_order(session, order_id, "REJECTED")
        await release_order(session, f"{order_id}_tp", "REJECTED")
        return False
    order = await session.get(OrderModel, order_id)
    order.status = "SUBMITTED"
    order.submitted_at = _now()
    order.ib_order_id = placed.order_id
    order.ib_perm_id = placed.perm_id
    tp_row = await session.get(OrderModel, f"{order_id}_tp")
    tp_row.status = "SUBMITTED"
    tp_row.submitted_at = _now()
    if roll_source is not None:
        # One roll attempt per position (#344), stamped atomically with the
        # SUBMITTED commit (#421) — a crash between them re-armed the latch.
        roll_source.journal = {**(roll_source.journal or {}), "rolled_to_ref": ref}
    summary.entries_placed.append(ref)
    await _audit(
        session,
        "ORDER_SUBMITTED",
        book.id,
        {"order_ref": ref, "playbook": playbook.id, "limit": net_mid, "profit_target": tp_price},
    )
    await session.commit()
    return True


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


async def run_executor_evening(
    session_maker=None, broker_factory=None, today: date | None = None
) -> ExecutorRunSummary:
    # Mode guard (#204): this pipeline is the PAPER executor. The live
    # executor is a separate, unbuilt thing (approval-per-trade, ADR-0006) —
    # running THIS code against live money must be impossible, not unlikely.
    if TRADING_MODE != "paper":
        raise RuntimeError(
            f"run_executor_evening is the PAPER executor; IBKR_TRADING_MODE={TRADING_MODE!r}. "
            "The live executor does not exist yet (ADR-0006) — refusing to run."
        )
    session_maker = session_maker or async_session_maker
    broker_factory = broker_factory or BrokerSession
    today = today or market_today()
    summary = ExecutorRunSummary(run_started_at=_now(), run_date=today.isoformat())

    # Holiday guard (#68, design §3.3): write the heartbeat and exit without
    # trading — silent non-operation is only acceptable when announced. The
    # gateway lifecycle also skips launching Gateway on these days.
    if not is_trading_day(today):
        summary.notes.append(f"MARKET HOLIDAY: {today.isoformat()} — no trading, heartbeat written")
        async with session_maker() as session:
            await _audit(session, "EXECUTOR_HOLIDAY_SKIP", None, {"date": today.isoformat()})
            await session.commit()
        _write_heartbeat(summary)
        return summary

    # One run at a time (#275, audit H5): a concurrent manual run would place
    # duplicate live closes and double-adjust cash. A held lock aborts THIS
    # run loudly and leaves the live run's heartbeat alone.
    lock = acquire_run_lock("executor")
    if lock is None:
        summary.notes.append("RUN LOCK HELD — another executor run is in progress; aborted without trading")
        async with session_maker() as session:
            await _audit(session, "RUN_LOCK_HELD", None, {})
            await session.commit()
        logger.error("Executor run lock held — aborting this run")
        return summary

    broker = broker_factory()
    crashed = False
    try:
        broker.open()
    except BrokerError as exc:
        summary.broker_ok = False
        summary.notes.append(f"Broker unavailable: {exc}")
        async with session_maker() as session:
            await _audit(session, "EXECUTOR_BROKER_UNAVAILABLE", None, {"error": str(exc)})
            await session.commit()
        _write_heartbeat(summary)
        release_run_lock(lock)
        return summary
    except BaseException:
        # #547: only BrokerError was guarded above — a non-BrokerError
        # escaping open() (e.g. thread/factory construction failing before
        # open()'s own try/except) leaked the executor lock until the 2h
        # staleness break. Release it here and re-raise; the crash alert
        # still fires from the entrypoint (gateway_lifecycle.run_nightly),
        # which sees this exception.
        release_run_lock(lock)
        raise

    try:
        async with session_maker() as session:
            # Missed-night detection (#283, audit M2): reqExecutions is
            # current-day-only, so a skipped night's fills are NOT here and
            # never will be. The weekly Flex audit DETECTS what the gap lost
            # (it is read-only, #410 — it never backfills); correcting the
            # books is the human's act through the resolution panel.
            # Pretending continuity would be silently wrong books.
            last_recon = (
                await session.execute(
                    select(ReconciliationRunModel).order_by(ReconciliationRunModel.id.desc()).limit(1)
                )
            ).scalar_one_or_none()
            gap_trading_days = _market_days_between(last_recon.run_at, today.isoformat()) if last_recon else 0
            if last_recon and gap_trading_days > 1:
                summary.notes.append(
                    f"⚠ MISSED NIGHT(S): last run {last_recon.run_at[:16]} — fills from the gap are NOT in the "
                    "books; run the Flex audit (pixi run flex-audit) to SEE what was missed, then correct the "
                    "books through the resolution panel (external close / cash adjust) — the audit never "
                    "backfills"
                )
                await _audit(session, "MISSED_NIGHT_GAP", None, {"last_run_at": last_recon.run_at})
                await session.commit()
            await _sync_order_states(session, broker, summary, restore_gap_trading_days=gap_trading_days)
            if summary.restore_gap_held:
                # #542: distinct from MISSED_NIGHT_GAP above — this is the
                # sync refusing to terminalize specific UNKNOWN rows rather
                # than just noting the gap. Directs the operator to the Flex
                # audit and resolution panel BEFORE assuming these are dead.
                summary.notes.append(
                    f"⚠ RESTORE GAP: {len(summary.restore_gap_held)} order(s) with UNKNOWN broker verdicts HELD "
                    f"(not terminalized) — gap since last run is {gap_trading_days} trading day(s): "
                    f"{', '.join(summary.restore_gap_held)}. Run the Flex audit and resolve via the panel before "
                    "assuming these are dead."
                )
            await _settle_expired(session, summary)
            # Phase-boundary refreshes (#471): a legitimate run longer than
            # STALE_AFTER_SECONDS must not have its LIVE lock classify stale
            # — breakable by the next scheduled task, invisible to the
            # fill check's Gateway-tenancy check — while mid-run. A stolen
            # lock (#536) is fatal here — abort before reconciliation.
            if await _abort_if_lock_lost(session, lock, summary, "reconciliation"):
                return summary
            snapshot = BrokerSnapshot(
                positions=tuple(broker.positions()),
                executions=tuple(broker.executions()),
                open_orders=tuple(broker.open_orders()),
            )
            recon = await run_reconciliation(session, snapshot, today=today.isoformat())
            summary.reconciliation = recon.result

            await apply_ntfy_commands(session)
            await refresh_position_values(session)
            state, telemetry_live = await refresh_market_state(session, today)
            await persist_index_history(session)
            readings = await persist_regime_readings(session, today)
            if state is None:
                summary.notes.append("No market state — run aborted after reconciliation")
                return summary

            for label in stale_calendars(today):
                summary.notes.append(
                    f"CALENDAR STALE ({label}): extend the table in backend/calendars.py before coverage lapses"
                )
            # Deliberately OCC-keyed across ALL books (#481 A-F6): the broker
            # reports aggregate leg quantities with no book attribution, so
            # when two books share a drifted leg there is no way to know
            # WHOSE copy the human closed — skipping both books' closes for
            # one night is the conservative reading; guessing an attribution
            # and selling the other book's bag into a hole is not.
            drifted_occ = frozenset(d.key for d in recon.drifts if d.kind in ("EXTERNAL_CLOSE", "PARTIAL_DRIFT"))
            # #536: a stolen lock is fatal here — abort before Layer A closes.
            if await _abort_if_lock_lost(session, lock, summary, "layer_a_closes"):
                return summary
            entries_ok = await _layer_a_closes(
                session, broker, state, summary, today, readings, telemetry_live, drifted_occ
            )
            if entries_ok:
                # #536: a stolen lock is fatal here — abort before Layer C entries.
                if await _abort_if_lock_lost(session, lock, summary, "layer_c_entries"):
                    return summary
                await _layer_c_entries(session, broker, state, readings, telemetry_live, summary, today)
            else:
                # A roll entry hit an order-path BrokerError (#421, design
                # §3.2): the broker just errored on the order path — Layer C
                # must not place entries against it minutes later.
                summary.entries_blocked.append(BlockedEntry(None, "entry phase aborted after roll broker error"))
                await _audit(session, "ENTRY_PHASE_ABORTED", None, {"reason": "roll order-path broker error"})
                await session.commit()
            findings = await run_post_session_anomalies(session, today.isoformat(), since=summary.run_started_at)
            summary.anomalies.extend(f"{f.rule}({f.scope}): {f.detail}" for f in findings)
    except BaseException:
        # Audit II (#341): a crashed run must NOT stamp a fresh heartbeat —
        # that silences the 22:00 watchdog, which exists precisely for this
        # night. The crash alert itself is the entrypoint's job
        # (gateway_lifecycle.run_nightly / main), which sees the exception.
        crashed = True
        raise
    finally:
        broker.close()
        if not crashed:
            _write_heartbeat(summary)
        release_run_lock(lock)
    return summary


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    from backend.run_logging import setup_run_logging

    setup_run_logging("executor")
    from backend.database import init_db

    await init_db()
    summary = await run_executor_evening()
    logger.info(
        "Executor run complete: broker_ok=%s reconciliation=%s entries=%d closes=%d blocked=%d",
        summary.broker_ok,
        summary.reconciliation,
        len(summary.entries_placed),
        len(summary.closes_placed),
        len(summary.entries_blocked),
    )

    # Digest + urgent tiering (#72): the nightly summary batches everything;
    # interrupt-worthy events additionally go out as a separate urgent push.
    from backend.digest import compose_executor_digest, urgent_events
    from backend.operator import send_ntfy_with_retry

    # The run's own date and start time (#259) — never recomputed here, so a
    # pipeline that crosses midnight UTC still reports its own events.
    async with async_session_maker() as session:
        title, body, priority = await compose_executor_digest(
            session, summary, summary.run_date, since=summary.run_started_at
        )
        urgent = await urgent_events(session, summary.run_started_at)
    pushed = send_ntfy_with_retry(title, body, priority)
    urgent_pushed = send_ntfy_with_retry("⛔ basis executor alerts", "\n".join(urgent), "urgent") if urgent else None
    # The digest is evidence too (#277, audit H2): scheduled-task stdout
    # vanishes and send_ntfy fails soft, so the composed text and its
    # delivery outcome are persisted where the console can show them.
    async with async_session_maker() as session:
        await _audit(
            session,
            "DIGEST_COMPOSED",
            None,
            {"title": title, "body": body, "priority": priority, "pushed": pushed, "urgent_pushed": urgent_pushed},
        )
        await session.commit()
    print(f"\n{title}\n{body}")


if __name__ == "__main__":
    asyncio.run(main())
