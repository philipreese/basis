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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.dates import market_today
from backend.models import AuditEventModel, BookModel, ClosurePostMortemModel, OrderModel, PositionModel

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


async def record_external_close(
    session: AsyncSession, position_id: str, exit_value_per_share: float, reason: str
) -> ClosurePostMortemModel:
    """The position was closed AT THE BROKER (manually, or by a partial-fill
    cleanup) — record that fact in the books: CLOSED at the stated per-share
    value, cash moved with the executor's own signed convention, a MANUAL
    post-mortem written, everything audited."""
    reason = _require_reason(reason)
    # NaN survives every comparison below (NaN < 0 is False) and would poison
    # cash_balance permanently (#346) — the schema also rejects it, but this
    # function is callable without the API.
    if not math.isfinite(exit_value_per_share):
        raise ResolutionError(f"exit_value_per_share must be a finite number, got {exit_value_per_share!r}")
    if exit_value_per_share < 0:
        raise ResolutionError("exit_value_per_share is a magnitude — sign comes from the premium direction.")
    pos = await session.get(PositionModel, position_id)
    if pos is None:
        raise ResolutionError(f"No position {position_id!r}")
    if pos.status != "OPEN":
        raise ResolutionError(f"Position {position_id!r} is {pos.status}, not OPEN")

    # Audit II (#345): while a STAGED/SUBMITTED order still references this
    # position, its fill can arrive on the next sync — an external close
    # recorded now and that fill would each book the same exit. Cancel the
    # order at the broker first. PARTIAL is deliberately exempt: the sync
    # latches partials for a human and never re-processes them, and THIS is
    # the designated cleanup path for exactly that latch (#283).
    live = (
        (
            await session.execute(
                select(OrderModel).filter(
                    OrderModel.position_id == position_id, OrderModel.status.in_(("STAGED", "SUBMITTED"))
                )
            )
        )
        .scalars()
        .all()
    )
    if live:
        refs = ", ".join(o.order_ref for o in live)
        raise ResolutionError(
            f"Position {position_id!r} has live broker order(s) [{refs}] — cancel them at the broker "
            "first, or their fill would double-count this exit on the next sync."
        )

    book = await session.get(BookModel, pos.book_id)
    if book is not None:
        # Credit position: buying back COSTS the exit value; debit: receives.
        flow = exit_value_per_share if pos.premium_direction == "DEBIT" else -exit_value_per_share
        book.cash_balance += flow * 100 * pos.contracts

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


async def adjust_book_cash(session: AsyncSession, book_id: str, delta: float, reason: str) -> float:
    """A signed cash correction with a mandatory reason — for discrepancies
    that aren't a whole position (fees, partial-fill remainders). Returns the
    new balance."""
    reason = _require_reason(reason)
    if not math.isfinite(delta):
        raise ResolutionError(f"delta must be a finite number, got {delta!r}")
    if delta == 0.0:
        raise ResolutionError("A zero adjustment corrects nothing.")
    book = await session.get(BookModel, book_id)
    if book is None:
        raise ResolutionError(f"No book {book_id!r}")
    book.cash_balance += delta
    await _audit(
        session,
        "RESOLUTION_CASH_ADJUSTED",
        book_id,
        {"delta": delta, "new_balance": round(book.cash_balance, 2), "reason": reason},
    )
    await session.commit()
    logger.info("Resolution: cash %+.2f on %s (%s)", delta, book_id, reason)
    return round(book.cash_balance, 2)
