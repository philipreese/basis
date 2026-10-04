"""resolution.py — audited book corrections (#310).

When books and broker diverge (reconciliation drift, a partial fill, a
manual action at the broker), the ledger must be corrected BY A HUMAN,
THROUGH AN AUDITED PATH — never by the system guessing (reconciliation's
no-auto-adjust principle) and never by hand SQL (invisible to the evidence).

Every correction here demands a reason, moves cash with the same signed
conventions as the executor's own close path, and lands in audit_events as
actor='resolution'. Resuming a halted scope remains a separate console act
(ADR-0008) — fixing the books never silently un-halts anything.
"""

import logging
import math
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.book_gates import credit_book_cash, resolve_for_book
from backend.dates import market_today
from backend.market_data import parse_occ_symbol
from backend.models import (
    AuditEventModel,
    BookModel,
    ClosurePostMortemModel,
    FillModel,
    FlexAckModel,
    OrderModel,
    PositionModel,
    ShareHoldingModel,
    ShareOrderModel,
)
from backend.reconciliation import (
    ASSIGNMENT_SUSPECTED,
    ORPHAN,
    SHARE_DRIFT,
    SHARE_QTY_TOLERANCE,
    _expected_share_quantities,
    latest_reconciliation_run,
)
from backend.share_book import book_fills, terminalize_unfilled
from backend.states import ORDER_STAGED_OR_SUBMITTED_STATUSES, POSITION_OPEN_STATUS, SHARE_ORDER_PENDING_STATUSES

logger = logging.getLogger(__name__)


class ResolutionError(ValueError):
    """A correction that cannot be applied as stated."""


def _require_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ResolutionError("A resolution requires a reason (min 3 characters) — it becomes the audit record.")
    return reason


async def _audit(session: AsyncSession, event_type: str, book_id: str | None, payload: dict) -> None:
    session.add(
        AuditEventModel(
            run_at=datetime.now(UTC).isoformat(),
            book_id=book_id,
            event_type=event_type,
            actor="resolution",
            payload=payload,
        )
    )


def validate_exit_value_per_share(exit_value_per_share: float) -> None:
    """#346 / #468: NaN survives every comparison below (NaN < 0 is False)
    and would poison cash_balance permanently. The schema deliberately does
    NOT reject it at the Pydantic layer (see ExternalCloseRequest and
    ClosePositionRequest in models.py — allow_inf_nan=False embeds the NaN
    input in the 422 error, which FastAPI's encoder cannot serialize and
    would 500 instead of a clean 400) — this function-level check is the
    only guard, shared by every cash-writing close path so it can never
    drift between them (#468)."""
    if not math.isfinite(exit_value_per_share):
        raise ResolutionError(f"exit_value_per_share must be a finite number, got {exit_value_per_share!r}")
    if exit_value_per_share < 0:
        raise ResolutionError("exit_value_per_share is a magnitude — sign comes from the premium direction.")


async def terminalize_live_orders_or_refuse(
    session: AsyncSession, position_id: str, reason: str, acknowledge_cancelled: bool
) -> None:
    """#345/#407/#468: while a STAGED/SUBMITTED order still references this
    position, its fill can arrive on the next sync — closing the position
    now (by any path: resolution's external-close, or the console's
    bookkeeping-only close) and that fill would each book the same exit.
    Refuse unless the caller acknowledges the orders are cancelled at the
    broker, then terminalize the DB rows here (audited) so the close can
    proceed. PARTIAL is deliberately exempt: the sync latches partials for a
    human and never re-processes them, and record_external_close is the
    designated cleanup path for exactly that latch (#283) — acknowledge_cancelled
    only ever asserts about STAGED/SUBMITTED rows.

    Audit II R2 (#407): refusing unconditionally was a lockout — DB rows only
    leave SUBMITTED via the nightly sync, so an operator who HAS cancelled at
    the broker was refused every day while Layer A re-staged a fresh close
    each night. acknowledge_cancelled=True is the operator's assertion that
    the listed orders are cancelled at the broker.

    Shared by every OPEN-position close path (#468) — previously only
    record_external_close had this guard; the console's close_position moved
    cash with no such check, stranding a resting GTC profit-taker SUBMITTED
    forever on a position the books already call CLOSED."""
    live = (
        (
            await session.execute(
                select(OrderModel).filter(
                    OrderModel.position_id == position_id, OrderModel.status.in_(ORDER_STAGED_OR_SUBMITTED_STATUSES)
                )
            )
        )
        .scalars()
        .all()
    )
    if live and not acknowledge_cancelled:
        refs = ", ".join(o.order_ref for o in live)
        raise ResolutionError(
            f"Position {position_id!r} has live broker order(s) [{refs}] — cancel them at the broker "
            "first, then re-submit with acknowledge_cancelled=true; an uncancelled order's fill "
            "would double-count this exit on the next sync."
        )
    # The operator's assertion covers CANCELLATION, not execution (#470,
    # Audit II R3): a close that partially executed before its cancel has
    # fills already backfilled onto the row. Terminalizing it here would book
    # a full-size exit over contracts that actually moved — the broker still
    # holds the remainder, surfacing as ORPHAN drift a session late with the
    # contradicting fills sitting silent. Refuse; the nightly sync latches
    # exactly this evidence as PARTIAL, and the partial workflow is the
    # designated path for it.
    for order in live:
        fills = (await session.execute(select(FillModel).filter_by(order_id=order.id))).scalars().all()
        if fills:
            raise ResolutionError(
                f"Order {order.order_ref!r} has {len(fills)} recorded fill(s) — it partially executed, "
                "so acknowledge_cancelled does not apply: this position's true size is unknown. Wait for "
                "the sync to latch the order PARTIAL (or resolve it via the partial workflow), then "
                "record the close for the true remaining size."
            )
    for order in live:
        order.status = "CANCELLED"
        order.completed_at = datetime.now(UTC).isoformat()
        await _audit(
            session,
            "RESOLUTION_ORDER_TERMINALIZED",
            order.book_id,
            {"order_ref": order.order_ref, "position_id": position_id, "reason": reason},
        )


async def record_external_close(
    session: AsyncSession,
    position_id: str,
    exit_value_per_share: float,
    reason: str,
    acknowledge_cancelled: bool = False,
) -> ClosurePostMortemModel:
    """The position was closed AT THE BROKER (manually, or by a partial-fill
    cleanup) — record that fact in the books: CLOSED at the stated per-share
    value, cash moved with the executor's own signed convention, a MANUAL
    post-mortem written, everything audited."""
    reason = _require_reason(reason)
    validate_exit_value_per_share(exit_value_per_share)
    pos = await session.get(PositionModel, position_id)
    if pos is None:
        raise ResolutionError(f"No position {position_id!r}")
    if pos.status != "OPEN":
        raise ResolutionError(f"Position {position_id!r} is {pos.status}, not OPEN")

    await terminalize_live_orders_or_refuse(session, position_id, reason, acknowledge_cancelled)

    # Conditional transition (#463, Audit II R3 F3): the OPEN check above is
    # a plain SELECT — a double-submitted close (two tabs, a retried
    # request) can both pass it and both reach here. This UPDATE is the real
    # guard: it only flips rows still OPEN, and SQLite serializes concurrent
    # writers (WAL + busy_timeout, #271), so the loser's WHERE matches zero
    # rows once the winner has committed. Losing raises here, before any
    # cash moves or a duplicate post-mortem is written.
    result = await session.execute(
        update(PositionModel)
        .where(PositionModel.id == pos.id, PositionModel.status == POSITION_OPEN_STATUS)
        .values(status="CLOSED")
    )
    if result.rowcount == 0:
        raise ResolutionError(f"Position {position_id!r} was closed concurrently — nothing to do")

    # Credit position: buying back COSTS the exit value; debit: receives.
    flow = exit_value_per_share if pos.premium_direction == "DEBIT" else -exit_value_per_share
    await credit_book_cash(session, pos.book_id, flow * 100 * pos.contracts)

    pos.status = "CLOSED"
    pos.current_value_per_share = exit_value_per_share
    pos.last_priced_at = datetime.now(UTC).isoformat()

    if pos.premium_direction == "DEBIT":
        realized = (exit_value_per_share - pos.entry_premium) * 100 * pos.contracts
    else:
        realized = (pos.entry_premium - exit_value_per_share) * 100 * pos.contracts
    realized = round(realized, 2)
    pm = ClosurePostMortemModel(
        id=str(uuid.uuid4()),
        position_id=pos.id,
        outcome="WIN" if realized > 0.01 else "LOSS" if realized < -0.01 else "BREAKEVEN",
        realized_pnl=realized,
        actual_underlying_move_pct=0.0,
        exit_date=market_today().isoformat(),
        exit_trigger="MANUAL",
        lesson_tags=[],
        user_override_logged=True,  # a human resolution IS an override, by definition
        playbook_id=pos.playbook_id,
        playbook_version=pos.playbook_version,
    )
    session.add(pm)
    await _audit(
        session,
        "RESOLUTION_EXTERNAL_CLOSE",
        pos.book_id,
        {"position_id": pos.id, "exit_value_per_share": exit_value_per_share, "reason": reason},
    )
    await session.commit()
    logger.info("Resolution: external close %s @ %.2f (%s)", position_id, exit_value_per_share, reason)
    return pm


async def resolve_partial_order(session: AsyncSession, order_ref: str, reason: str) -> str:
    """Terminalize a PARTIAL order row (#414). The PARTIAL latch (#283) keeps
    its encumbrance and slot count against MAX_DEPLOYED/MAX_POSITIONS until
    the row leaves a pending status — and nothing else ever moves it: the
    sync skips PARTIAL forever, and record_external_close deliberately
    doesn't touch order rows. Without this, a partial ENTRY (no position, so
    no external close applies) haircuts the book's capacity permanently.

    This releases the latch ONLY. The cash/position consequences of the
    partial are the human's to record first — record_external_close for a
    partial close, adjust_book_cash for a partial entry's remainder.
    """
    reason = _require_reason(reason)
    order = (await session.execute(select(OrderModel).filter_by(order_ref=order_ref))).scalar_one_or_none()
    if order is None:
        raise ResolutionError(f"No order with ref {order_ref!r}")
    if order.status != "PARTIAL":
        raise ResolutionError(f"Order {order_ref!r} is {order.status}, not PARTIAL — nothing to resolve")
    # Order of operations is load-bearing (#469): while this order's position
    # is still OPEN, releasing the latch re-arms full-size expiry settlement —
    # nothing can shrink pos.contracts, the PARTIAL_DRIFT halt goes
    # reconciliation-neutral once the legs expire, and _settle_expired's
    # PARTIAL-row guard dies with the row this function terminalizes. The
    # position's true outcome must be recorded FIRST (record_external_close
    # at the real exit value closes it); only then is the latch safe to drop.
    if order.position_id:
        pos = await session.get(PositionModel, order.position_id)
        if pos is not None and pos.status == "OPEN":
            raise ResolutionError(
                f"Order {order_ref!r} belongs to position {order.position_id!r}, which is still OPEN — "
                "record the position's true outcome first (external close at the real exit value), "
                "then release this latch; releasing it now would re-arm full-size expiry settlement "
                "for contracts the broker no longer holds."
            )
    order.status = "CANCELLED"
    order.completed_at = datetime.now(UTC).isoformat()
    await _audit(
        session,
        "RESOLUTION_PARTIAL_TERMINALIZED",
        order.book_id,
        {"order_ref": order_ref, "released_encumbrance": order.encumbered_risk, "reason": reason},
    )
    await session.commit()
    logger.info("Resolution: PARTIAL %s terminalized (%s)", order_ref, reason)
    # The row's actual terminal status (#479) — the caller shouldn't hardcode
    # "CANCELLED" separately from what this function actually set.
    return order.status


async def adjust_book_cash(session: AsyncSession, book_id: str, delta: float, reason: str) -> float:
    """A signed cash correction with a mandatory reason — for discrepancies
    that aren't a whole position (fees, partial-fill remainders). Returns the
    new balance."""
    reason = _require_reason(reason)
    if not math.isfinite(delta):
        raise ResolutionError(f"delta must be a finite number, got {delta!r}")
    if delta == 0.0:
        raise ResolutionError("A zero adjustment corrects nothing.")
    new_balance = await credit_book_cash(session, book_id, delta)
    if new_balance is None:
        raise ResolutionError(f"No book {book_id!r}")
    await _audit(
        session,
        "RESOLUTION_CASH_ADJUSTED",
        book_id,
        {"delta": delta, "new_balance": round(new_balance, 2), "reason": reason},
    )
    await session.commit()
    logger.info("Resolution: cash %+.2f on %s (%s)", delta, book_id, reason)
    return round(new_balance, 2)


async def ack_flex_discrepancies(
    session: AsyncSession, exec_ids: list[str], reason: str
) -> tuple[list[str], list[str]]:
    """Explain a weekly Flex-audit discrepancy exec_id once (#544).

    Corrections made via the resolution endpoints above never create a
    FillModel row, and nothing recorded "this exec_id was explained" — so a
    corrected discrepancy re-alerted at urgent priority forever. This is the
    sanctioned, audited way to say "seen it, handled it": append-only,
    idempotent (already-acked ids are reported back, not re-inserted), and
    never auto-applied — a human explains each id explicitly.
    """
    reason = _require_reason(reason)
    exec_ids = [e.strip() for e in exec_ids if e and e.strip()]
    if not exec_ids:
        raise ResolutionError("At least one exec_id is required.")
    existing = set(
        (await session.execute(select(FlexAckModel.exec_id).filter(FlexAckModel.exec_id.in_(exec_ids)))).scalars()
    )
    to_ack = [e for e in exec_ids if e not in existing]
    now = datetime.now(UTC).isoformat()
    for exec_id in to_ack:
        session.add(FlexAckModel(exec_id=exec_id, reason=reason, acked_at=now))
    if to_ack:
        await _audit(
            session,
            "FLEX_DISCREPANCY_ACKED",
            None,
            {"exec_ids": to_ack, "reason": reason},
        )
    await session.commit()
    logger.info("Resolution: flex-ack %d exec_id(s) (%d already acked) — %s", len(to_ack), len(existing), reason)
    return to_ack, sorted(existing)


# ---------------------------------------------------------------------------
# Share drift (#1074) — the share book's holdings through the same audited path
# ---------------------------------------------------------------------------


def _require_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ResolutionError(f"{name} must be a finite number, got {value!r}")


async def _pending_share_refs(session: AsyncSession, book_id: str, symbol: str) -> list[str]:
    rows = await session.execute(
        select(ShareOrderModel.order_ref).filter(
            ShareOrderModel.book_id == book_id,
            ShareOrderModel.symbol == symbol,
            ShareOrderModel.status.in_(SHARE_ORDER_PENDING_STATUSES),
        )
    )
    return list(rows.scalars().all())


async def _increase_evidence(
    session: AsyncSession, book_id: str, symbol: str, before: float, corrected: float
) -> tuple[int, float]:
    """The fail-closed gate on RAISING a holding — the one direction that could
    launder an assignment into a clean night. Returns (reconciliation run id,
    broker quantity) for the audit record, or raises.

    An increase is accepted only against the latest UNRESOLVED drift run that
    reported this designated symbol off its holding, never mixed-sign, with
    no ASSIGNMENT_SUSPECTED on an option of the same underlying in that run,
    and only up to what that run saw at the broker (net of every other
    designated book's holding). Nobody can claim shares the broker did not
    show, and an assignment in flight on the symbol must be closed first."""
    run = await latest_reconciliation_run(session)
    if run is None or run.result != "DRIFT" or run.resolved_at is not None:
        raise ResolutionError(
            "Raising a share holding is only accepted against an unresolved reconciliation DRIFT run that shows "
            "the extra shares at the broker — there is none."
        )
    details = run.drift_details or []
    items = [
        d
        for d in details
        if d.get("key") == symbol and d.get("sec_type") == "STK" and d.get("kind") in (ORPHAN, SHARE_DRIFT)
    ]
    if not items:
        raise ResolutionError(
            f"Reconciliation run #{run.id} reports no share drift on {symbol} — nothing at the broker supports "
            "raising the holding."
        )
    item = items[0]
    if item.get("mixed_sign"):
        raise ResolutionError(
            f"Run #{run.id} saw {symbol} in both a long and a short row — short stock is never a deliberate "
            "holding; close it at the broker first."
        )
    for d in details:
        parsed = parse_occ_symbol(str(d.get("key") or "")) if d.get("kind") == ASSIGNMENT_SUSPECTED else None
        if parsed is not None and parsed["underlying"] == symbol:
            raise ResolutionError(
                f"Run #{run.id} suspects an option assignment on {symbol} ({d.get('key')}) — those shares are not "
                "this book's. Close the assigned shares at the broker first, then correct the holding."
            )
    broker_qty = float(item.get("broker_qty") or 0.0)
    others = (await _expected_share_quantities(session)).get(symbol, 0.0) - before
    if corrected + others > broker_qty + SHARE_QTY_TOLERANCE:
        raise ResolutionError(
            f"{corrected:g} {symbol} (plus {others:g} held by other designated books) is more than the "
            f"{broker_qty:g} run #{run.id} saw at the broker — a holding can never claim shares the broker "
            "did not show."
        )
    return run.id, broker_qty


async def correct_share_holding(
    session: AsyncSession,
    book_id: str,
    symbol: str,
    current_quantity: float,
    corrected_quantity: float,
    cause: str,
    reason: str,
    claim_increase: bool = False,
    cash_delta: float = 0.0,
) -> tuple[float, float, float]:
    """Set a designated book's share holding to what the broker shows, after a
    human says why (#1074). Returns (before, after, cash balance).

    Never automatic: reconciliation still only detects, and this runs only
    when the operator submits it. Fails closed:
    - only a book designated for the symbol (`share_symbols`) — an
      undesignated symbol's shares are an orphan or an assignment, closed at
      the broker, never adopted into a book here;
    - never negative — short stock is never a deliberate holding;
    - never while a share order on that book and symbol is pending (its fill
      would land on top of the correction: settle it first);
    - compare-and-set against `current_quantity`, so a fill the evening sync
      booked since the operator looked is never overwritten;
    - raising the holding is an explicit, audited claim (`claim_increase`)
      checked against the latest drift run (see _increase_evidence).
    Optional `cash_delta` moves the matching cash in the same transaction, so
    a hand sale never shows up as a one-night equity drop."""
    reason = _require_reason(reason)
    symbol = (symbol or "").strip().upper()
    for name, value in (
        ("current_quantity", current_quantity),
        ("corrected_quantity", corrected_quantity),
        ("cash_delta", cash_delta),
    ):
        _require_finite(name, value)
    if corrected_quantity < 0:
        raise ResolutionError("A share holding cannot be negative — short stock is never deliberate.")
    book = await session.get(BookModel, book_id)
    if book is None:
        raise ResolutionError(f"No book {book_id!r}")
    if symbol not in resolve_for_book(book).share_symbols:
        raise ResolutionError(
            f"{book_id} is not designated to hold {symbol!r} (share_symbols) — shares no book holds on purpose are "
            "an orphan or an assignment: close them at the broker, they are never adopted into a book here."
        )
    pending = await _pending_share_refs(session, book_id, symbol)
    if pending:
        raise ResolutionError(
            f"Share order(s) {', '.join(pending)} on {book_id} {symbol} are still pending — settle them first "
            "(share-order settlement); their fills would otherwise land on top of this correction."
        )
    holding = await session.get(ShareHoldingModel, (book_id, symbol), populate_existing=True)
    before = holding.quantity if holding is not None else 0.0
    if abs(before - current_quantity) > SHARE_QTY_TOLERANCE:
        raise ResolutionError(
            f"{book_id} holds {before:g} {symbol} in the books, not {current_quantity:g} — it changed since you "
            "looked (the evening sync may have booked a fill). Re-read it and resubmit."
        )
    delta = corrected_quantity - before
    if abs(delta) <= SHARE_QTY_TOLERANCE and cash_delta == 0.0:
        raise ResolutionError("The corrected quantity equals the holding and no cash moves — nothing to correct.")
    run_id: int | None = None
    broker_qty: float | None = None
    if delta > SHARE_QTY_TOLERANCE:
        if not claim_increase:
            raise ResolutionError(
                f"Raising {book_id} {symbol} by {delta:g} claims those shares are the book's own, not an option "
                "assignment — confirm the claim explicitly (claim_increase) to proceed."
            )
        run_id, broker_qty = await _increase_evidence(session, book_id, symbol, before, corrected_quantity)
    now = datetime.now(UTC).isoformat()
    if holding is None:
        session.add(ShareHoldingModel(book_id=book_id, symbol=symbol, quantity=corrected_quantity, updated_at=now))
    else:
        # Compare-and-set in SQL (#463/#466): the sync's own write can land
        # between the read above and this one.
        result = await session.execute(
            update(ShareHoldingModel)
            .where(
                ShareHoldingModel.book_id == book_id,
                ShareHoldingModel.symbol == symbol,
                ShareHoldingModel.quantity == before,
            )
            .values(quantity=corrected_quantity, updated_at=now)
        )
        if result.rowcount == 0:
            raise ResolutionError(f"{book_id} {symbol} changed concurrently — re-read it and resubmit.")
    if cash_delta:
        balance = await credit_book_cash(session, book_id, cash_delta)
    else:
        await session.refresh(book, ["cash_balance"])
        balance = book.cash_balance
    await _audit(
        session,
        "RESOLUTION_SHARE_HOLDING_CORRECTED",
        book_id,
        {
            "symbol": symbol,
            "cause": cause,
            "quantity_before": before,
            "quantity_after": corrected_quantity,
            "delta": delta,
            "claim_increase": claim_increase,
            "claimed_shares": delta if delta > SHARE_QTY_TOLERANCE else 0.0,
            "reconciliation_run_id": run_id,
            "broker_qty": broker_qty,
            "cash_delta": cash_delta,
            "reason": reason,
        },
    )
    await session.commit()
    logger.info(
        "Resolution: %s %s holding %g -> %g (%s: %s)", book_id, symbol, before, corrected_quantity, cause, reason
    )
    return before, corrected_quantity, round(balance or 0.0, 2)


async def settle_share_order(
    session: AsyncSession,
    order_ref: str,
    filled_quantity: float,
    avg_fill_price: float | None,
    commission: float,
    reason: str,
) -> tuple[str, float, float]:
    """Settle a pending share order the sync cannot (#1074): FILLED at the
    broker with its executions out of reach (a missed night — reqExecutions
    is current-day-only), or UNKNOWN after a restore gap. Returns (status,
    filled quantity, holding after).

    The operator states the order's TOTAL execution, read off the statement
    or the Flex audit. Executions the sync already recorded stay; the rest is
    appended as one `resolution:` execution priced so the order's average is
    the stated one, and then share_book.book_fills books it into
    share_holdings and book cash exactly as the sync would have (audited
    actor=resolution). FILLED when the whole order executed, else CANCELLED
    with its filled quantity — the sync's own partial semantics. Asserting an
    order is finished at the broker is the operator's call; a ref that is in
    fact still resting there surfaces as a GHOST_ORDER on the next run."""
    reason = _require_reason(reason)
    for name, value in (("filled_quantity", filled_quantity), ("commission", commission)):
        _require_finite(name, value)
    order = (await session.execute(select(ShareOrderModel).filter_by(order_ref=order_ref))).scalar_one_or_none()
    if order is None:
        raise ResolutionError(f"No share order with ref {order_ref!r}")
    if order.status not in SHARE_ORDER_PENDING_STATUSES:
        raise ResolutionError(f"Share order {order_ref!r} is {order.status}, not pending — nothing to settle")
    if filled_quantity < 0 or filled_quantity > order.quantity + SHARE_QTY_TOLERANCE:
        raise ResolutionError(f"filled_quantity must be between 0 and the order's {order.quantity} shares")
    if commission < 0:
        raise ResolutionError("commission is a magnitude — it cannot be negative")
    fills = list(order.fills or [])
    recorded_qty = sum(f["quantity"] for f in fills)
    recorded_notional = sum(f["quantity"] * f["price"] for f in fills)
    recorded_commission = sum(f["commission"] for f in fills)
    remainder = filled_quantity - recorded_qty
    if remainder < -SHARE_QTY_TOLERANCE:
        raise ResolutionError(
            f"{recorded_qty:g} shares are already recorded against {order_ref} — the total cannot be less."
        )
    extra_commission = commission - recorded_commission
    if extra_commission < -0.005:
        raise ResolutionError(
            f"{recorded_commission:.2f} commission is already recorded against {order_ref} — the total cannot be less."
        )
    if remainder > SHARE_QTY_TOLERANCE:
        if avg_fill_price is None or not math.isfinite(avg_fill_price) or avg_fill_price <= 0:
            raise ResolutionError("avg_fill_price (a positive number) is required for shares beyond those recorded")
        price = (avg_fill_price * filled_quantity - recorded_notional) / remainder
        if not math.isfinite(price) or price <= 0:
            raise ResolutionError(
                f"An average of {avg_fill_price:.4f} over {filled_quantity:g} shares is inconsistent with the "
                "executions already recorded against this order."
            )
        fills.append(
            {
                "exec_id": f"resolution:{order_ref}",
                "quantity": remainder,
                "price": price,
                "commission": max(0.0, extra_commission),
                "exec_time": datetime.now(UTC).isoformat(),
            }
        )
        order.fills = fills
    elif extra_commission > 0.005 and recorded_qty > SHARE_QTY_TOLERANCE:
        # Every share is already recorded; only commission was missed.
        fills.append(
            {
                "exec_id": f"resolution:{order_ref}",
                "quantity": 0.0,
                "price": 0.0,
                "commission": extra_commission,
                "exec_time": datetime.now(UTC).isoformat(),
            }
        )
        order.fills = fills
    total = sum(f["quantity"] for f in fills)
    if total <= SHARE_QTY_TOLERANCE:
        if not await terminalize_unfilled(session, order):
            raise ResolutionError(f"Share order {order_ref!r} was terminalized concurrently — nothing to do")
        status = "CANCELLED"
    else:
        status = "FILLED" if abs(total - order.quantity) <= SHARE_QTY_TOLERANCE else "CANCELLED"
        if await book_fills(session, order, status, actor="resolution") is None:
            raise ResolutionError(f"Share order {order_ref!r} was terminalized concurrently — nothing to do")
    holding = await session.get(ShareHoldingModel, (order.book_id, order.symbol))
    holding_after = holding.quantity if holding is not None else 0.0
    await _audit(
        session,
        "RESOLUTION_SHARE_ORDER_SETTLED",
        order.book_id,
        {
            "order_ref": order_ref,
            "symbol": order.symbol,
            "side": order.side,
            "ordered_quantity": order.quantity,
            "filled_quantity": total,
            "avg_fill_price": avg_fill_price,
            "commission": commission,
            "status": status,
            "holding_after": holding_after,
            "reason": reason,
        },
    )
    await session.commit()
    logger.info("Resolution: share order %s settled %s, %g filled (%s)", order_ref, status, total, reason)
    return status, total, holding_after
