"""anomaly.py — deterministic auto-halt rules (spec/supervision.md §6.2–6.3, #71).

Machine-checkable rules with IDs used verbatim in audit_events, halt
reasons, digests, and tests. Automatic responses stop at HALT_ENTRIES
(ADR-0008) — nothing here ever liquidates. Rules only escalate: a scope
already in FLATTEN_REQUESTED is never downgraded.

Wired by the executor: DUPLICATE_ORDER at entry-staging time, the rest as a
post-session pass. RECONCILIATION_DRIFT / UNEXPECTED_INSTRUMENT live in
backend/reconciliation.py; STALE_DATA and UNFILLED_ENTRY are pipeline
behaviors in backend/executor.py — same rule vocabulary, one enforcement
point each.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.book_gates import DEFAULT_ENVELOPE
from backend.models import AuditEventModel, BookModel, OrderModel, PositionModel, TradingControlModel
from backend.pricing import capital_at_risk
from backend.trading_control import ACTIVE, GLOBAL_SCOPE, HALT_ENTRIES, set_control

logger = logging.getLogger(__name__)

REPEATED_REJECTION = "REPEATED_REJECTION"
DUPLICATE_ORDER = "DUPLICATE_ORDER"
PNL_SHOCK = "PNL_SHOCK"
ENVELOPE_BREACH_POSTHOC = "ENVELOPE_BREACH_POSTHOC"

_REJECTION_EVENTS = ("ORDER_REJECTED", "CLOSE_REJECTED")
PNL_SHOCK_PCT = 15.0  # of book basis; envelope-derived, re-derive once real fills exist


@dataclass(frozen=True)
class AnomalyFinding:
    rule: str
    scope: str  # GLOBAL or a book id
    detail: str


async def _halt(session: AsyncSession, finding: AnomalyFinding) -> None:
    """Latch HALT_ENTRIES for the finding's scope — escalation only."""
    row = await session.get(TradingControlModel, finding.scope)
    current = row.state if row is not None else None
    session.add(
        AuditEventModel(
            run_at=datetime.now(UTC).isoformat(),
            book_id=None if finding.scope == GLOBAL_SCOPE else finding.scope,
            event_type=finding.rule,
            actor="anomaly",
            payload={"detail": finding.detail, "state_before": current},
        )
    )
    await session.commit()
    if current == ACTIVE or current is None:
        await set_control(
            session, finding.scope, HALT_ENTRIES, reason=f"{finding.rule}: {finding.detail}", actor="anomaly"
        )
    logger.error("Anomaly %s (%s): %s", finding.rule, finding.scope, finding.detail)


def entry_signature(book_id: str, legs: tuple[tuple[str, str], ...]) -> str:
    """(book, legs+directions) fingerprint — OCC symbols already encode
    underlying, expiry, strike, and option type."""
    return f"{book_id}|" + "|".join(f"{occ}:{direction}" for occ, direction in sorted(legs))


async def check_duplicate_order(
    session: AsyncSession, book_id: str, legs: tuple[tuple[str, str], ...], today: str
) -> bool:
    """True if a matching entry was already submitted this session (logic bug,
    not market condition). The caller must block the order AND halt globally."""
    signature = entry_signature(book_id, legs)
    orders = (
        (await session.execute(select(OrderModel).filter(OrderModel.book_id == book_id, OrderModel.action == "OPEN")))
        .scalars()
        .all()
    )
    for order in orders:
        if not (order.submitted_at or "").startswith(today) and not (order.completed_at or "").startswith(today):
            continue
        meta = order.combo_legs or {}
        existing = tuple((leg["occ"], leg["direction"]) for leg in meta.get("legs", []))
        if existing and entry_signature(book_id, existing) == signature:
            return True
    return False


async def check_repeated_rejection(session: AsyncSession, today: str) -> AnomalyFinding | None:
    """≥2 rejections tonight, or ≥3 across the trailing 3 sessions with
    rejections — our model of the broker's rules is wrong; retrying digs holes."""
    events = (
        (await session.execute(select(AuditEventModel).filter(AuditEventModel.event_type.in_(_REJECTION_EVENTS))))
        .scalars()
        .all()
    )
    by_date: dict[str, int] = {}
    for e in events:
        by_date[e.run_at[:10]] = by_date.get(e.run_at[:10], 0) + 1
    tonight = by_date.get(today, 0)
    if tonight >= 2:
        return AnomalyFinding(REPEATED_REJECTION, GLOBAL_SCOPE, f"{tonight} rejections tonight")
    trailing = sum(count for _date, count in sorted(by_date.items(), reverse=True)[:3])
    if trailing >= 3:
        return AnomalyFinding(REPEATED_REJECTION, GLOBAL_SCOPE, f"{trailing} rejections across trailing 3 sessions")
    return None


def book_mtm(book: BookModel, open_positions: list[PositionModel]) -> float:
    """Mark-to-market book equity: cash plus signed liquidation value of open
    positions (credit positions carry a buy-back liability)."""
    equity = book.cash_balance
    for pos in open_positions:
        value = pos.current_value_per_share * 100 * pos.contracts
        equity += value if pos.premium_direction == "DEBIT" else -value
    return round(equity, 2)


async def check_pnl_shock(
    session: AsyncSession, book: BookModel, open_positions: list[PositionModel]
) -> AnomalyFinding | None:
    """Day MTM move beyond 15% of basis: a 4-position defined-risk book
    respecting the envelope cannot legitimately lose that much in a day —
    beyond it is a pricing-data or attribution bug. Updates the baseline."""
    basis = float(((book.config or {}).get("envelope", {})).get("basis", DEFAULT_ENVELOPE["basis"]))
    mtm = book_mtm(book, open_positions)
    previous = book.last_mtm
    book.last_mtm = mtm
    book.last_mtm_at = datetime.now(UTC).isoformat()
    if previous is None:
        return None
    move = abs(mtm - previous)
    if move > basis * PNL_SHOCK_PCT / 100.0:
        return AnomalyFinding(
            PNL_SHOCK, book.id, f"day MTM move ${move:.0f} exceeds {PNL_SHOCK_PCT}% of ${basis:.0f} basis"
        )
    return None


async def check_envelope_breach(
    session: AsyncSession, book: BookModel, open_positions: list[PositionModel]
) -> AnomalyFinding | None:
    """Reconciled state violating the envelope proves a code defect — these
    are pre-blocked by gates, so post-hoc detection means a gate was bypassed."""
    envelope = {**DEFAULT_ENVELOPE, **((book.config or {}).get("envelope", {}))}
    basis = float(envelope["basis"])
    breaches: list[str] = []
    if len(open_positions) > int(envelope["max_positions"]):
        breaches.append(f"{len(open_positions)} positions > {envelope['max_positions']}")
    deployed = sum(capital_at_risk(p.max_loss, p.contracts) for p in open_positions)
    deployed_cap = basis * float(envelope["max_deployed_pct"]) / 100.0
    if deployed > deployed_cap:
        breaches.append(f"deployed ${deployed:.0f} > ${deployed_cap:.0f}")
    per_trade_cap = basis * float(envelope["max_loss_pct_per_trade"]) / 100.0
    for pos in open_positions:
        risk = capital_at_risk(pos.max_loss, pos.contracts)
        if risk > per_trade_cap:
            breaches.append(f"position {pos.id} risk ${risk:.0f} > ${per_trade_cap:.0f}")
    if breaches:
        return AnomalyFinding(ENVELOPE_BREACH_POSTHOC, book.id, "; ".join(breaches))
    return None


async def run_post_session_anomalies(session: AsyncSession, today: str) -> list[AnomalyFinding]:
    """The end-of-run sweep: repeated rejections (global) plus per-book PNL
    shock and post-hoc envelope breaches. Applies latching halts."""
    findings: list[AnomalyFinding] = []

    rejection = await check_repeated_rejection(session, today)
    if rejection:
        findings.append(rejection)

    books = (
        (await session.execute(select(BookModel).filter(BookModel.status == "ACTIVE", BookModel.id != "B00")))
        .scalars()
        .all()
    )
    for book in books:
        open_positions = list(
            (await session.execute(select(PositionModel).filter_by(status="OPEN", book_id=book.id))).scalars().all()
        )
        shock = await check_pnl_shock(session, book, open_positions)
        if shock:
            findings.append(shock)
        breach = await check_envelope_breach(session, book, open_positions)
        if breach:
            findings.append(breach)
    await session.commit()  # persists updated MTM baselines

    for finding in findings:
        await _halt(session, finding)
    return findings
