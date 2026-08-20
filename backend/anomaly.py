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

from backend.book_gates import resolve_book_config
from backend.dates import market_today
from backend.models import (
    AuditEventModel,
    BookModel,
    BookMtmHistoryModel,
    OrderModel,
    PositionModel,
    TradingControlModel,
)
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
    session: AsyncSession, book_id: str, legs: tuple[tuple[str, str], ...], window_start: str
) -> bool:
    """True if a matching entry already exists this evening (logic bug, not
    market condition). The caller must block the order AND halt globally.

    *window_start* is a UTC ISO timestamp (market_evening_window_start);
    matching is >= against it — the caller has passed a timestamp since #259,
    and the old date-prefix startswith silently matched NOTHING against it,
    leaving duplicate detection dead (found by audit H5, #275). A STAGED row
    is always in-window: the sync expires stale STAGED intents at run start,
    so one alive during the entry phase was created this run."""
    signature = entry_signature(book_id, legs)
    orders = (
        (await session.execute(select(OrderModel).filter(OrderModel.book_id == book_id, OrderModel.action == "OPEN")))
        .scalars()
        .all()
    )
    for order in orders:
        stamp = order.submitted_at or order.completed_at or ""
        if order.status != "STAGED" and (not stamp or stamp < window_start):
            continue
        meta = order.combo_legs or {}
        existing = tuple((leg["occ"], leg["direction"]) for leg in meta.get("legs", []))
        if existing and entry_signature(book_id, existing) == signature:
            return True
    return False


async def check_repeated_rejection(
    session: AsyncSession, today: str, since: str | None = None
) -> AnomalyFinding | None:
    """≥2 rejections tonight, or ≥3 across the trailing 3 sessions with
    rejections — our model of the broker's rules is wrong; retrying digs holes.
    *since* (run-start timestamp, #259) defines "tonight" robustly: a UTC
    date-prefix undercounts every EST evening, where most of the run happens
    after midnight UTC."""
    events = (
        (await session.execute(select(AuditEventModel).filter(AuditEventModel.event_type.in_(_REJECTION_EVENTS))))
        .scalars()
        .all()
    )
    by_date: dict[str, int] = {}
    for e in events:
        by_date[e.run_at[:10]] = by_date.get(e.run_at[:10], 0) + 1
    tonight = sum(1 for e in events if e.run_at >= since) if since else by_date.get(today, 0)
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
    session: AsyncSession, book: BookModel, open_positions: list[PositionModel], today: str | None = None
) -> AnomalyFinding | None:
    """Day MTM move beyond 15% of basis: a 4-position defined-risk book
    respecting the envelope cannot legitimately lose that much in a day —
    beyond it is a pricing-data or attribution bug. Updates the baseline.
    *today* is the run's market date (#259) — the equity-curve row must not
    land under tomorrow just because UTC rolled over mid-run."""
    basis = resolve_book_config(book.config).envelope.basis
    mtm = book_mtm(book, open_positions)
    previous = book.last_mtm
    previous_at = book.last_mtm_at
    book.last_mtm = mtm
    book.last_mtm_at = datetime.now(UTC).isoformat()
    # The equity curve (#239): last_mtm alone is overwritten nightly, so
    # every mark also lands in book_mtm_history. merge = same-day rerun
    # overwrites its row instead of duplicating.
    mark_date = today or market_today().isoformat()
    await session.merge(BookMtmHistoryModel(book_id=book.id, date=mark_date, mtm=mtm))
    if previous is None:
        return None
    move = abs(mtm - previous)
    if move > basis * PNL_SHOCK_PCT / 100.0:
        # A multi-session move must not trip a ONE-day threshold (#280, M4):
        # if the previous mark is older than the prior trading day (missed
        # night, holiday+failure), record the gap instead of halting.
        if previous_at and _market_days_between(previous_at, today) > 1:
            session.add(
                AuditEventModel(
                    run_at=datetime.now(UTC).isoformat(),
                    book_id=book.id,
                    event_type="PNL_SHOCK_SKIPPED_GAP",
                    actor="anomaly",
                    payload={"move": round(move, 2), "previous_mark_at": previous_at, "today": today},
                )
            )
            return None
        return AnomalyFinding(
            PNL_SHOCK, book.id, f"day MTM move ${move:.0f} exceeds {PNL_SHOCK_PCT}% of ${basis:.0f} basis"
        )
    return None


def _market_days_between(previous_iso: str, today: str | None) -> int:
    """Trading days from the previous mark's date to *today* (ISO market
    date). Unparseable inputs read as 1 — the shock check then applies.

    Timezone (#419): previous_iso is a UTC timestamp (run_at rows), and an
    evening run that commits after 00:00 UTC would read as the NEXT day —
    under-counting a gap by one and letting a genuinely missed night look
    covered. Aware timestamps convert to the market timezone first; naive
    inputs (plain dates) are taken as market dates already."""
    from datetime import date as _date

    from backend.calendars import is_trading_day
    from backend.dates import MARKET_TZ

    try:
        prev = datetime.fromisoformat(previous_iso)
        start = prev.astimezone(MARKET_TZ).date() if prev.tzinfo is not None else prev.date()
        end = _date.fromisoformat(today) if today else market_today()
    except ValueError:
        return 1
    days = 0
    cursor = start
    while cursor < end and days < 30:
        cursor = _date.fromordinal(cursor.toordinal() + 1)
        if is_trading_day(cursor):
            days += 1
    return days


async def check_envelope_breach(
    session: AsyncSession, book: BookModel, open_positions: list[PositionModel]
) -> AnomalyFinding | None:
    """Reconciled state violating the envelope proves a code defect — these
    are pre-blocked by gates, so post-hoc detection means a gate was bypassed."""
    envelope = resolve_book_config(book.config).envelope
    breaches: list[str] = []
    if len(open_positions) > envelope.max_positions:
        breaches.append(f"{len(open_positions)} positions > {envelope.max_positions}")
    deployed = sum(capital_at_risk(p.max_loss, p.contracts) for p in open_positions)
    deployed_cap = envelope.basis * envelope.max_deployed_pct / 100.0
    if deployed > deployed_cap:
        breaches.append(f"deployed ${deployed:.0f} > ${deployed_cap:.0f}")
    per_trade_cap = envelope.basis * envelope.max_loss_pct_per_trade / 100.0
    for pos in open_positions:
        risk = capital_at_risk(pos.max_loss, pos.contracts)
        if risk > per_trade_cap:
            breaches.append(f"position {pos.id} risk ${risk:.0f} > ${per_trade_cap:.0f}")
    if breaches:
        return AnomalyFinding(ENVELOPE_BREACH_POSTHOC, book.id, "; ".join(breaches))
    return None


async def run_post_session_anomalies(
    session: AsyncSession, today: str, since: str | None = None
) -> list[AnomalyFinding]:
    """The end-of-run sweep: repeated rejections (global) plus per-book PNL
    shock and post-hoc envelope breaches. Applies latching halts. *today* is
    the run's market date; *since* its start timestamp (#259)."""
    findings: list[AnomalyFinding] = []

    rejection = await check_repeated_rejection(session, today, since=since)
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
        shock = await check_pnl_shock(session, book, open_positions, today=today)
        if shock:
            findings.append(shock)
        breach = await check_envelope_breach(session, book, open_positions)
        if breach:
            findings.append(breach)
    await session.commit()  # persists updated MTM baselines

    for finding in findings:
        await _halt(session, finding)
    return findings
