"""digest.py — the executor's evening digest and urgent-push tiering (#72).

Implements spec/supervision.md §6.4: the digest's section order is fixed
(control-state banner first — a halted system must say so every night, or
silence becomes indistinguishable from health), "reconciliation clean" is
stated explicitly (absence of the line must never be interpretable as
success), and events that need human action before the next evening go out
as a SEPARATE urgent push. Push fatigue is itself a safety failure: normal
fills, P&L, and gate hits batch into the digest; only control-state changes
and failures interrupt.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.book_gates import DEFAULT_ENVELOPE
from backend.executor import ExecutorRunSummary
from backend.models import (
    AuditEventModel,
    BookModel,
    GateEventModel,
    OrderModel,
    PositionModel,
    TradingControlModel,
)
from backend.pricing import capital_at_risk
from backend.trading_control import ACTIVE, sentinel_halt_active

logger = logging.getLogger(__name__)

LIVE_GATE_TRADES = 30  # ADR-0006: ≥30 closed paper trades per book config

# Audit event types that interrupt the human instead of waiting for the digest
URGENT_EVENT_TYPES = frozenset(
    {
        "REPEATED_REJECTION",
        "DUPLICATE_ORDER",
        "PNL_SHOCK",
        "ENVELOPE_BREACH_POSTHOC",
        "ORDER_REJECTED",
        "CLOSE_REJECTED",
        "EXECUTOR_BROKER_UNAVAILABLE",
        "ORDER_LOST_AT_BROKER",
    }
)
_URGENT_CONTROL_ACTORS = frozenset({"anomaly", "reconciliation", "ntfy"})


async def urgent_events(session: AsyncSession, today: str) -> list[str]:
    """Tonight's interrupt-worthy events, one line each."""
    events = (
        (await session.execute(select(AuditEventModel).filter(AuditEventModel.run_at.startswith(today))))
        .scalars()
        .all()
    )
    lines: list[str] = []
    for e in events:
        if e.event_type in URGENT_EVENT_TYPES:
            detail = e.payload.get("detail") or e.payload.get("error") or e.payload.get("order_ref") or ""
            lines.append(f"{e.event_type}{f' ({e.book_id})' if e.book_id else ''}: {detail}".rstrip(": "))
        elif e.event_type == "CONTROL_STATE_CHANGED" and e.actor in _URGENT_CONTROL_ACTORS:
            lines.append(f"HALT by {e.actor}: {e.payload.get('reason', '')}")
    return lines


async def _control_banner(session: AsyncSession) -> list[str]:
    lines: list[str] = []
    if sentinel_halt_active():
        lines.append("⛔ SENTINEL HALT file present — all entries blocked")
    rows = (await session.execute(select(TradingControlModel))).scalars().all()
    for row in sorted(rows, key=lambda r: r.scope):
        if row.state != ACTIVE:
            lines.append(f"⛔ {row.scope} {row.state} since {row.changed_at[:16]} — {row.reason}")
    return lines


async def _books_section(session: AsyncSession) -> list[str]:
    books = (
        (await session.execute(select(BookModel).filter(BookModel.status == "ACTIVE", BookModel.id != "B00")))
        .scalars()
        .all()
    )
    lines: list[str] = []
    for book in sorted(books, key=lambda b: b.id):
        envelope = {**DEFAULT_ENVELOPE, **((book.config or {}).get("envelope", {}))}
        positions = (await session.execute(select(PositionModel).filter_by(book_id=book.id))).scalars().all()
        open_positions = [p for p in positions if p.status == "OPEN"]
        closed = sum(1 for p in positions if p.status in ("CLOSED", "EXPIRED"))
        deployed = sum(capital_at_risk(p.max_loss, p.contracts) for p in open_positions)
        deployed_pct = deployed / float(envelope["basis"]) * 100.0
        pnl = (book.last_mtm - book.starting_capital) if book.last_mtm is not None else 0.0
        variant = (book.config or {}).get("engine_variant", "?")
        underlying = (book.config or {}).get("underlying", "?")
        lines.append(
            f"{book.id} [{variant}/{underlying}] P&L {pnl:+.0f} | "
            f"pos {len(open_positions)}/{envelope['max_positions']} | "
            f"deployed {deployed_pct:.0f}% | gate {closed}/{LIVE_GATE_TRADES}"
        )
    return lines


async def _gate_hits(session: AsyncSession, today: str) -> list[str]:
    events = (
        (
            await session.execute(
                select(GateEventModel).filter(GateEventModel.result == "BLOCK", GateEventModel.run_at.startswith(today))
            )
        )
        .scalars()
        .all()
    )
    by_gate: dict[str, int] = {}
    for e in events:
        key = f"{e.book_id}:{e.gate}"
        by_gate[key] = by_gate.get(key, 0) + 1
    return [f"Gate {key} blocked ×{n}" for key, n in sorted(by_gate.items())]


async def _fills_section(session: AsyncSession, today: str) -> list[str]:
    orders = (
        (
            await session.execute(
                select(OrderModel).filter(OrderModel.status == "FILLED", OrderModel.completed_at.startswith(today))
            )
        )
        .scalars()
        .all()
    )
    lines = []
    for o in orders:
        strategy = (o.combo_legs or {}).get("strategy_type", o.action)
        lines.append(
            f"Filled {o.book_id} {strategy} ({o.action}) @ limit {o.limit_price:+.2f}"
            f" (decision mid {o.decision_midpoint:+.2f})"
        )
    return lines


async def compose_executor_digest(
    session: AsyncSession, summary: ExecutorRunSummary, today: str | None = None
) -> tuple[str, str, str]:
    """Build (title, body, ntfy_priority) per the §6.4 section order."""
    today = today or datetime.now(UTC).date().isoformat()

    banner = await _control_banner(session)
    fills = await _fills_section(session, today)
    books = await _books_section(session)
    gate_hits = await _gate_hits(session, today)

    lines: list[str] = []
    lines.extend(banner)
    if not summary.broker_ok:
        lines.append("⚠ IB Gateway unreachable — no orders were possible tonight")
    lines.extend(fills)
    if summary.positions_created:
        lines.append(f"{len(summary.positions_created)} position(s) opened from fills")
    for ref in summary.closes_placed:
        lines.append(f"Close submitted: {ref}")
    for ref in summary.entries_placed:
        lines.append(f"Entry submitted: {ref}")
    for blocked in summary.entries_blocked:
        lines.append(f"Blocked: {blocked}")
    for ref in summary.intents_expired:
        lines.append(f"Intent expired: {ref}")
    lines.extend(books)
    lines.extend(gate_hits)
    # Reconciliation is stated explicitly — silence must never read as success.
    if summary.reconciliation == "CLEAN":
        lines.append("Reconciliation clean")
    elif summary.reconciliation == "DRIFT":
        lines.append("⛔ Reconciliation DRIFT — entries halted until resolved")
    else:
        lines.append(f"Reconciliation: {summary.reconciliation}")
    for anomaly in summary.anomalies:
        lines.append(f"⛔ {anomaly}")
    lines.extend(summary.notes)

    title_bits: list[str] = []
    if banner or summary.anomalies or summary.reconciliation == "DRIFT":
        title_bits.append("HALTED" if banner else "alerts")
    if summary.entries_placed:
        title_bits.append(f"{len(summary.entries_placed)} entered")
    if summary.closes_placed:
        title_bits.append(f"{len(summary.closes_placed)} closing")
    if not title_bits:
        title_bits.append("all quiet")
    title = "basis executor: " + ", ".join(title_bits)

    priority = (
        "high"
        if banner or summary.anomalies or not summary.broker_ok or summary.reconciliation == "DRIFT"
        else "default"
    )
    return title, "\n".join(lines), priority
