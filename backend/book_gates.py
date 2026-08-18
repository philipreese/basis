"""book_gates.py — per-book risk-envelope gates and capital encumbrance (#67).

Shared-capital isolation is arithmetic (design §4.1): every gate evaluates
against the book's VIRTUAL ledger — the $10K book basis — never the paper
account's real balance. Every evaluation, pass or block, is written to the
append-only gate_events table, which is what makes the Live Gate's "zero
breaches" criterion a table scan (ADR-0006).

Encumbrance: capital reserved by a staged/pending OPEN order counts toward
the deployed gate until the order reaches a terminal status. Without it, two
same-evening candidates in one book could both pass the deployed gate.
Persisted on the orders row so a crash cannot forget a reservation.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import BookModel, GateEventModel, OrderModel, PositionModel
from backend.pricing import capital_at_risk

logger = logging.getLogger(__name__)

# ADR-0006 risk envelope defaults; a book's config {"envelope": {...}} overrides.
# max_positions raised 4 → 8 for the accelerated cadence (#136, ADR-0009):
# 8 × ~$250 max loss = 20% deployed, still far under the 50% cap — the old 4
# was the binding constraint on trade-count accumulation, not a risk limit.
DEFAULT_ENVELOPE = {
    "basis": 10_000.0,
    "max_loss_pct_per_trade": 2.5,
    "max_deployed_pct": 50.0,
    "max_positions": 8,
    "max_same_strategy_expiry": 2,
}

# Order statuses whose encumbrance still counts (non-terminal, capital reserved)
PENDING_ORDER_STATUSES = ("STAGED", "SUBMITTED", "PARTIAL")

# ADR-0006 Live Gate: ≥30 closed paper trades per book config before live money
LIVE_GATE_TRADES = 30

PASS = "PASS"
BLOCK = "BLOCK"


@dataclass(frozen=True)
class CandidateOrder:
    """The gate-relevant shape of a would-be entry, independent of playbook."""

    book_id: str
    strategy_type: str
    expiration_date: str  # ISO date
    legs: tuple[tuple[str, str], ...]  # (occ_symbol, "LONG" | "SHORT")
    max_loss_per_share: float
    contracts: int

    @property
    def risk_dollars(self) -> float:
        return capital_at_risk(self.max_loss_per_share, self.contracts)


@dataclass(frozen=True)
class GateOutcome:
    gate: str
    result: str  # PASS | BLOCK
    detail: str


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    outcomes: tuple[GateOutcome, ...]

    def blocked_by(self) -> tuple[str, ...]:
        return tuple(o.gate for o in self.outcomes if o.result == BLOCK)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _book_open_positions(session: AsyncSession, book_id: str) -> list[PositionModel]:
    rows = await session.execute(select(PositionModel).filter_by(status="OPEN", book_id=book_id))
    return list(rows.scalars().all())


async def _pending_open_orders(session: AsyncSession, book_id: str) -> list[OrderModel]:
    rows = await session.execute(
        select(OrderModel).filter(
            OrderModel.book_id == book_id,
            OrderModel.action == "OPEN",
            OrderModel.status.in_(PENDING_ORDER_STATUSES),
        )
    )
    return list(rows.scalars().all())


async def evaluate_book_gates(session: AsyncSession, candidate: CandidateOrder) -> GateDecision:
    """Run every envelope gate for one candidate and log each outcome.

    Fail-closed: an unknown or non-ACTIVE book blocks outright. The decision
    commits its gate_events before returning, so the record exists even if
    the caller subsequently aborts.
    """
    outcomes: list[GateOutcome] = []
    book = await session.get(BookModel, candidate.book_id)
    if book is None or book.status != "ACTIVE":
        status = "missing" if book is None else book.status
        outcomes.append(GateOutcome("BOOK_ACTIVE", BLOCK, f"book {candidate.book_id} is {status}"))
        await _log_outcomes(session, candidate.book_id, outcomes)
        return GateDecision(allowed=False, outcomes=tuple(outcomes))

    envelope = {**DEFAULT_ENVELOPE, **((book.config or {}).get("envelope", {}))}
    basis = float(envelope["basis"])
    open_positions = await _book_open_positions(session, candidate.book_id)
    pending_orders = await _pending_open_orders(session, candidate.book_id)

    risk = candidate.risk_dollars
    max_loss_cap = basis * float(envelope["max_loss_pct_per_trade"]) / 100.0
    outcomes.append(
        GateOutcome(
            "MAX_LOSS_PER_TRADE",
            PASS if risk <= max_loss_cap else BLOCK,
            f"risk ${risk:.0f} vs cap ${max_loss_cap:.0f} ({envelope['max_loss_pct_per_trade']}% of ${basis:.0f})",
        )
    )

    deployed = sum(capital_at_risk(p.max_loss, p.contracts) for p in open_positions)
    encumbered = sum(o.encumbered_risk for o in pending_orders)
    deployed_cap = basis * float(envelope["max_deployed_pct"]) / 100.0
    total_after = deployed + encumbered + risk
    outcomes.append(
        GateOutcome(
            "MAX_DEPLOYED",
            PASS if total_after <= deployed_cap else BLOCK,
            f"deployed ${deployed:.0f} + encumbered ${encumbered:.0f} + candidate ${risk:.0f}"
            f" = ${total_after:.0f} vs cap ${deployed_cap:.0f}",
        )
    )

    slots_used = len(open_positions) + len(pending_orders)
    outcomes.append(
        GateOutcome(
            "MAX_POSITIONS",
            PASS if slots_used + 1 <= int(envelope["max_positions"]) else BLOCK,
            f"{slots_used} open/pending + 1 vs max {envelope['max_positions']}",
        )
    )

    same_bucket = sum(
        1
        for p in open_positions
        if p.strategy_type == candidate.strategy_type and p.expiration_date == candidate.expiration_date
    )
    outcomes.append(
        GateOutcome(
            "STRATEGY_EXPIRY_CONCENTRATION",
            PASS if same_bucket + 1 <= int(envelope["max_same_strategy_expiry"]) else BLOCK,
            f"{same_bucket} open sharing {candidate.strategy_type}@{candidate.expiration_date}"
            f" vs max {envelope['max_same_strategy_expiry']}",
        )
    )

    outcomes.append(await _cross_book_netting_outcome(session, candidate))

    await _log_outcomes(session, candidate.book_id, outcomes)
    return GateDecision(allowed=all(o.result == PASS for o in outcomes), outcomes=tuple(outcomes))


async def _cross_book_netting_outcome(session: AsyncSession, candidate: CandidateOrder) -> GateOutcome:
    """Hard-block opposite-direction exposure on the same contract anywhere in
    the account. The broker nets per conId, so opposite directions net to flat
    at the broker — making broker state ambiguous and exercise/expiry
    unattributable (design §4.3). Same-direction sharing is fine."""
    from backend.market_data import format_occ_symbol

    open_positions = (await session.execute(select(PositionModel).filter_by(status="OPEN"))).scalars().all()
    held: dict[str, set[str]] = {}
    for pos in open_positions:
        for leg in pos.legs:
            occ = format_occ_symbol(pos.underlying, leg["expiration"], leg["option_type"], leg["strike"])
            held.setdefault(occ, set()).add(leg["direction"])
    for occ, direction in candidate.legs:
        opposite = "SHORT" if direction == "LONG" else "LONG"
        if opposite in held.get(occ, set()):
            return GateOutcome("CROSS_BOOK_NETTING", BLOCK, f"{occ}: candidate {direction} vs held {opposite}")
    return GateOutcome("CROSS_BOOK_NETTING", PASS, "no opposite-direction contract sharing")


async def _log_outcomes(session: AsyncSession, book_id: str, outcomes: list[GateOutcome]) -> None:
    now = _now()
    for o in outcomes:
        session.add(
            GateEventModel(book_id=book_id, run_at=now, gate=o.gate, result=o.result, context={"detail": o.detail})
        )
    await session.commit()


async def stage_order(
    session: AsyncSession,
    candidate: CandidateOrder,
    *,
    order_id: str,
    order_ref: str,
    limit_price: float,
    decision_midpoint: float,
    combo_legs: dict,
) -> OrderModel:
    """Write the intent row (STAGED) with its capital encumbrance — BEFORE
    placeOrder, per the idempotency contract (design §2.4). The encumbrance
    holds until release_order() moves the row to a terminal status."""
    order = OrderModel(
        id=order_id,
        book_id=candidate.book_id,
        position_id=None,
        order_ref=order_ref,
        ib_order_id=None,
        ib_perm_id=None,
        action="OPEN",
        combo_legs=combo_legs,
        order_type="LIMIT",
        limit_price=limit_price,
        decision_midpoint=decision_midpoint,
        status="STAGED",
        submitted_at=None,
        completed_at=None,
        encumbered_risk=candidate.risk_dollars,
    )
    session.add(order)
    await session.commit()
    return order


async def release_order(session: AsyncSession, order_id: str, final_status: str) -> None:
    """Move a pending order to a terminal status, releasing its encumbrance
    (the deployed gate only counts PENDING_ORDER_STATUSES)."""
    if final_status not in ("FILLED", "CANCELLED", "REJECTED"):
        raise ValueError(f"Not a terminal order status: {final_status!r}")
    order = await session.get(OrderModel, order_id)
    if order is None:
        raise ValueError(f"No order {order_id!r}")
    order.status = final_status
    order.completed_at = _now()
    await session.commit()
