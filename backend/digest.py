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

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.benchmark import spy_benchmark_line
from backend.book_gates import LIVE_GATE_TRADES, resolve_book_config
from backend.dates import market_evening_window_start, market_today
from backend.executor import BlockedEntry, ExecutorRunSummary
from backend.models import (
    AuditEventModel,
    BookModel,
    GateEventModel,
    OrderModel,
    PositionModel,
    RegimeReadingModel,
    TradingControlModel,
)
from backend.pricing import capital_at_risk
from backend.trading_control import ACTIVE, sentinel_halt_active

logger = logging.getLogger(__name__)

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
        # Exit-side escalations (#280): a needed close that DIDN'T happen is
        # exactly what must interrupt a human.
        "STALE_MARK_CLOSE_SKIPPED",
        "CLOSE_LADDER_EXHAUSTED",
        "PARTIAL_FILL",
        # #546 liveness: a TP cancel persistently unconfirmed skipped the
        # close nightly with no rung consumed and no escalation ever — this
        # is that escalation.
        "TP_CANCEL_STUCK",
        # A hard crash mid-run (#474): the executor stopped doing anything.
        "CRASH_ALERT",
    }
)
_URGENT_CONTROL_ACTORS = frozenset({"anomaly", "reconciliation", "ntfy"})
# Expiry-settlement blocks are namespaced (EXPIRY_SETTLEMENT_BLOCKED_PARTIAL,
# EXPIRY_SETTLEMENT_BLOCKED_STALE_MARK, …) rather than listed individually.
_URGENT_EVENT_PREFIXES = ("EXPIRY_SETTLEMENT_BLOCKED_",)


def is_urgent_event_type(event_type: str) -> bool:
    """Single source of truth for 'this audit row needs a human now' (#474).
    Used both for the nightly urgent push (urgent_events, below) and for the
    server-computed AuditEventSchema.urgent flag every console list renders —
    so the two can never drift apart again."""
    return event_type in URGENT_EVENT_TYPES or event_type.startswith(_URGENT_EVENT_PREFIXES)


async def urgent_events(session: AsyncSession, since: str) -> list[str]:
    """Tonight's interrupt-worthy events, one line each. *since* is the run's
    start timestamp (#259): a date-prefix match broke whenever the pipeline
    crossed midnight UTC — every EST-season evening — silently emptying the
    urgent push of the very rejections and halts it exists to carry."""
    events = (await session.execute(select(AuditEventModel).filter(AuditEventModel.run_at >= since))).scalars().all()
    lines: list[str] = []
    for e in events:
        if is_urgent_event_type(e.event_type):
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
    """Per-book lines for books with something to say; the rest collapse
    into one idle line that still names every book id — at 22 books
    (ADR-0009) a full roster every night buries the signal, but absence
    must never be silent (supervision.md), so the ids stay visible."""
    books = (
        (await session.execute(select(BookModel).filter(BookModel.status == "ACTIVE", BookModel.id != "B00")))
        .scalars()
        .all()
    )
    # Books whose orders are resting at the broker are NOT idle — orders only
    # become positions on the next fill sync, so on entry-heavy nights the
    # old positions-only heuristic listed every submitting book as idle (#225).
    pending_books = set(
        (await session.execute(select(OrderModel.book_id).filter(OrderModel.status.in_(("STAGED", "SUBMITTED")))))
        .scalars()
        .all()
    )
    lines: list[str] = []
    idle: list[str] = []
    awaiting: list[str] = []
    for book in sorted(books, key=lambda b: b.id):
        config = resolve_book_config(book.config)
        positions = (await session.execute(select(PositionModel).filter_by(book_id=book.id))).scalars().all()
        open_positions = [p for p in positions if p.status == "OPEN"]
        closed = sum(1 for p in positions if p.status in ("CLOSED", "EXPIRED"))
        deployed = sum(capital_at_risk(p.max_loss, p.contracts) for p in open_positions)
        deployed_pct = deployed / config.envelope.basis * 100.0
        pnl = (book.last_mtm - book.starting_capital) if book.last_mtm is not None else 0.0
        variant = config.variant or "?"
        underlying = config.underlying or "?"
        if not open_positions and closed == 0 and pnl == 0.0:
            (awaiting if book.id in pending_books else idle).append(book.id)
            continue
        lines.append(
            f"{book.id} [{variant}/{underlying}] P&L {pnl:+.0f} | "
            f"pos {len(open_positions)}/{config.envelope.max_positions} | "
            f"deployed {deployed_pct:.0f}% | gate {closed}/{LIVE_GATE_TRADES}"
        )
    if awaiting:
        lines.append(f"{len(awaiting)} book(s) awaiting fill (orders resting at broker): {' '.join(awaiting)}")
    if idle:
        lines.append(f"{len(idle)} book(s) idle (no positions, gate 0/{LIVE_GATE_TRADES}): {' '.join(idle)}")
    return lines


async def _regime_line(session: AsyncSession, today: str) -> str | None:
    """Tonight's regime per engine variant — one line, every night. Variant
    DISAGREEMENT is the informative early signal (regime_variants.py), long
    before per-book trade counts mean anything; a split must never require
    querying the database by hand to notice (#248)."""
    rows = (
        (
            await session.execute(
                select(RegimeReadingModel).filter(RegimeReadingModel.date == today, RegimeReadingModel.book_id == "ALL")
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    by_regime: dict[str, list[str]] = {}
    missing: list[str] = []
    for r in sorted(rows, key=lambda r: r.engine_variant):
        if r.regime == "INSUFFICIENT_DATA":
            missing.append(r.engine_variant)
        else:
            by_regime.setdefault(r.regime, []).append(r.engine_variant)
    suffix = f" ({' '.join(missing)} insufficient data)" if missing else ""
    if len(by_regime) == 1:
        regime = next(iter(by_regime))
        return f"Regime: {regime} (all variants){suffix}"
    groups = sorted(by_regime.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    rendered = " / ".join(f"{regime} ({' '.join(variants)})" for regime, variants in groups)
    return f"Regime split: {rendered}{suffix}"


async def _gate_hits(session: AsyncSession, since: str) -> list[str]:
    events = (
        (
            await session.execute(
                select(GateEventModel).filter(GateEventModel.result == "BLOCK", GateEventModel.run_at >= since)
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


async def _fills_section(session: AsyncSession, since: str) -> list[str]:
    orders = (
        (
            await session.execute(
                select(OrderModel).filter(OrderModel.status == "FILLED", OrderModel.completed_at >= since)
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


def _grouped_blocked(blocked: list[BlockedEntry]) -> list[str]:
    """Group identical block reasons across books: six copies of
    'variant V1 reading unavailable' become one line listing the books.
    Run-wide blocks (book_id None) render as ALL, ungrouped."""
    by_reason: dict[str, list[str]] = {}
    run_wide: list[str] = []
    for entry in blocked:
        if entry.book_id is None:
            run_wide.append(f"Blocked: ALL: {entry.reason}")
        else:
            by_reason.setdefault(entry.reason, []).append(entry.book_id)
    lines = []
    for reason, books in sorted(by_reason.items()):
        if len(books) == 1:
            lines.append(f"Blocked: {books[0]}: {reason}")
        else:
            lines.append(f"Blocked ({reason}): {' '.join(sorted(books))}")
    return lines + run_wide


async def compose_executor_digest(
    session: AsyncSession, summary: ExecutorRunSummary, today: str | None = None, since: str | None = None
) -> tuple[str, str, str]:
    """Build (title, body, ntfy_priority) per the §6.4 section order.

    *today* is the run's MARKET date (America/New_York, #259) — used for the
    regime-reading lookup. *since* is the run's start timestamp — every event
    filter uses it, because a date-prefix match silently dropped everything
    written after a mid-run UTC midnight (every EST-season evening)."""
    today = today or market_today().isoformat()
    # #545 L2: f"{today}T00:00:00" mixes a MARKET date with UTC run_at rows —
    # yesterday's post-19:00-ET events (already past 00:00 UTC) re-enter
    # tonight's sections in EST season. The real fallback (manual/test paths
    # only; the executor always passes since=summary.run_started_at) is the
    # same evening-window start the duplicate-order check uses.
    since = since or market_evening_window_start(datetime.date.fromisoformat(today))

    banner = await _control_banner(session)
    fills = await _fills_section(session, since)
    books = await _books_section(session)
    gate_hits = await _gate_hits(session, since)

    lines: list[str] = []
    lines.extend(banner)
    regime = await _regime_line(session, today)
    if regime:
        lines.append(regime)
    if not summary.broker_ok:
        lines.append("⚠ IB Gateway unreachable — no orders were possible tonight")
    lines.extend(fills)
    if summary.positions_created:
        lines.append(f"{len(summary.positions_created)} position(s) opened from fills")
    for ref in summary.closes_placed:
        lines.append(f"Close submitted: {ref}")
    for ref in summary.entries_placed:
        lines.append(f"Entry submitted: {ref}")
    lines.extend(_grouped_blocked(summary.entries_blocked))
    for ref in summary.intents_expired:
        lines.append(f"Intent expired: {ref}")
    lines.extend(books)
    benchmark = await spy_benchmark_line(session)
    if benchmark:
        lines.append(benchmark)
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
