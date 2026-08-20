"""console.py — read-model aggregation for the supervision console (#73).

Backs the Books tab and status strip (design §6.5): per-book summaries with
the ADR-0006 Live Gate checklist, and the executor status (heartbeat +
last reconciliation). Pure read paths — nothing here mutates ledgers.

Metric definitions:
- Realized P&L per closed trade uses the same formula as the manual close
  flow (credit: entry − exit; debit: exit − entry; ×100×contracts).
- Expectancy is the mean realized P&L per closed trade minus the slippage
  haircut — IBKR paper combo fills are optimistic (ADR-0007), so raw paper
  expectancy is never trusted.
- Max drawdown is peak-to-trough on the CUMULATIVE realized P&L of closed
  trades in entry-date order. There is no per-book equity history table yet
  (pre-launch schema policy, #94), so open-position marks are not included.
- "Zero breaches" counts ENVELOPE_BREACH_POSTHOC audit events for the book —
  a post-hoc envelope violation is the breach the Live Gate cares about.
"""

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.book_gates import LIVE_GATE_TRADES, resolve_book_config
from backend.models import (
    AuditEventModel,
    BookModel,
    BookSummarySchema,
    ExecutorStatusSchema,
    FillModel,
    LiveGateChecklistSchema,
    OrderModel,
    PositionModel,
    ReconciliationRunModel,
    TradingControlModel,
)
from backend.pricing import capital_at_risk

logger = logging.getLogger(__name__)

# $0.05/share per combo round trip = $5 per contract per closed trade.
# Applied to paper expectancy before it can satisfy the Live Gate (ADR-0007).
SLIPPAGE_HAIRCUT_PER_CONTRACT = 5.0

LIVE_GATE_MONTHS = 3.0  # ADR-0006: ≥3 months of paper history per book
_DAYS_PER_MONTH = 30.44

# The console paints the last-run timestamp red beyond this (design §6.5)
STALE_AFTER_HOURS = 24.0

_CLOSED_STATUSES = ("CLOSED", "EXPIRED")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def heartbeat_path() -> Path:
    """Where the executor's dead-man heartbeat lives (#72). The executor writes
    it; this module and the watchdog read it. Lives here so the API process
    never has to import the broker stack just to show a timestamp."""
    return Path(os.getenv("EXECUTOR_HEARTBEAT_FILE", str(_PROJECT_ROOT / "executor_heartbeat.json")))


def realized_pnl(position: PositionModel) -> float:
    """Same math as the manual close flow in main.py — one definition of P&L."""
    if position.premium_direction == "DEBIT":
        pnl = (position.current_value_per_share - position.entry_premium) * 100 * position.contracts
    else:
        pnl = (position.entry_premium - position.current_value_per_share) * 100 * position.contracts
    return round(pnl, 2)


def _months_since(iso_timestamp: str, now: datetime) -> float:
    try:
        started = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0.0, (now - started).total_seconds() / 86400.0 / _DAYS_PER_MONTH)


def _max_drawdown(pnls_in_order: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    drawdown = 0.0
    for pnl in pnls_in_order:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return round(drawdown, 2)


async def book_summaries(session: AsyncSession, now: datetime | None = None) -> list[BookSummarySchema]:
    """One row per lab book for the Books tab (B00 legacy excluded)."""
    now = now or datetime.now(UTC)
    books = (await session.execute(select(BookModel).filter(BookModel.id != "B00"))).scalars().all()
    controls = {row.scope: row.state for row in (await session.execute(select(TradingControlModel))).scalars().all()}
    breach_rows = (
        (await session.execute(select(AuditEventModel).filter_by(event_type="ENVELOPE_BREACH_POSTHOC"))).scalars().all()
    )
    breaches_by_book: dict[str, int] = {}
    for row in breach_rows:
        if row.book_id:
            breaches_by_book[row.book_id] = breaches_by_book.get(row.book_id, 0) + 1

    summaries: list[BookSummarySchema] = []
    for book in sorted(books, key=lambda b: b.id):
        config = resolve_book_config(book.config)
        positions = (await session.execute(select(PositionModel).filter_by(book_id=book.id))).scalars().all()
        open_positions = [p for p in positions if p.status == "OPEN"]
        closed = sorted((p for p in positions if p.status in _CLOSED_STATUSES), key=lambda p: p.entry_date)

        closed_pnls = [realized_pnl(p) for p in closed]
        wins = sum(1 for pnl in closed_pnls if pnl > 0.01)
        win_rate = wins / len(closed) if closed else None
        # Commissions are real (#276, audit H1): the gate expectancy nets out
        # each trade's ledgered commissions ON TOP of the slippage haircut —
        # the haircut proxies fill quality (ADR-0007), never broker fees.
        commission_rows = (
            await session.execute(
                select(OrderModel.position_id, FillModel.commission)
                .join(FillModel, FillModel.order_id == OrderModel.id)
                .filter(OrderModel.book_id == book.id, OrderModel.position_id.is_not(None))
            )
        ).all()
        commissions_by_pos: dict[str, float] = {}
        for pos_id, commission in commission_rows:
            commissions_by_pos[pos_id] = commissions_by_pos.get(pos_id, 0.0) + (commission or 0.0)
        expectancy = (
            sum(
                pnl - SLIPPAGE_HAIRCUT_PER_CONTRACT * p.contracts - commissions_by_pos.get(p.id, 0.0)
                for pnl, p in zip(closed_pnls, closed, strict=True)
            )
            / len(closed)
            if closed
            else None
        )

        deployed = sum(capital_at_risk(p.max_loss, p.contracts) for p in open_positions)
        deployed_pct = deployed / config.envelope.basis * 100.0
        pnl = (book.last_mtm - book.starting_capital) if book.last_mtm is not None else 0.0

        breaches = breaches_by_book.get(book.id, 0)
        months = _months_since(book.created_at, now)
        gate = LiveGateChecklistSchema(
            closed_trades=len(closed),
            closed_trades_required=LIVE_GATE_TRADES,
            trades_ok=len(closed) >= LIVE_GATE_TRADES,
            months_elapsed=round(months, 2),
            months_required=LIVE_GATE_MONTHS,
            months_ok=months >= LIVE_GATE_MONTHS,
            breaches=breaches,
            breaches_ok=breaches == 0,
            expectancy_after_haircut=round(expectancy, 2) if expectancy is not None else None,
            expectancy_ok=expectancy is not None and expectancy >= 0.0,
            eligible=(
                len(closed) >= LIVE_GATE_TRADES
                and months >= LIVE_GATE_MONTHS
                and breaches == 0
                and expectancy is not None
                and expectancy >= 0.0
            ),
        )

        summaries.append(
            BookSummarySchema(
                id=book.id,
                name=book.name,
                status=book.status,
                engine_variant=config.variant or "?",
                underlying=config.underlying or "?",
                config_hash=book.config_hash,
                config_version=book.config_version,
                starting_capital=book.starting_capital,
                cash_balance=book.cash_balance,
                last_mtm=book.last_mtm,
                pnl=round(pnl, 2),
                closed_trades=len(closed),
                win_rate=round(win_rate, 4) if win_rate is not None else None,
                expectancy_after_haircut=gate.expectancy_after_haircut,
                max_drawdown=_max_drawdown(closed_pnls),
                deployed_pct=round(deployed_pct, 2),
                open_positions=len(open_positions),
                max_positions=config.envelope.max_positions,
                # Fail-closed mirror of trading_control: a book without a row is halted
                control_state=controls.get(book.id, "HALT_ENTRIES"),  # type: ignore[arg-type]
                live_gate=gate,
            )
        )
    return summaries


async def executor_status(session: AsyncSession, now: datetime | None = None) -> ExecutorStatusSchema:
    """Heartbeat + last reconciliation for the status strip (design §6.5)."""
    now = now or datetime.now(UTC)
    heartbeat_at: str | None = None
    age_hours: float | None = None
    broker_ok: bool | None = None
    entries: int | None = None
    closes: int | None = None

    path = heartbeat_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            heartbeat_at = data.get("at")
            broker_ok = data.get("broker_ok")
            entries = data.get("entries_placed")
            closes = data.get("closes_placed")
        except (json.JSONDecodeError, OSError):
            logger.warning("Unreadable heartbeat file at %s", path)
        if heartbeat_at:
            try:
                written = datetime.fromisoformat(heartbeat_at)
                if written.tzinfo is None:
                    written = written.replace(tzinfo=UTC)
                age_hours = round((now - written).total_seconds() / 3600.0, 2)
            except ValueError:
                heartbeat_at = None

    last_recon = (
        await session.execute(select(ReconciliationRunModel).order_by(ReconciliationRunModel.id.desc()).limit(1))
    ).scalar_one_or_none()
    # Digest delivery status (#277, audit H2): a total ntfy outage must be
    # visible SOMEWHERE — this is the somewhere.
    last_digest = (
        await session.execute(
            select(AuditEventModel).filter_by(event_type="DIGEST_COMPOSED").order_by(AuditEventModel.id.desc()).limit(1)
        )
    ).scalar_one_or_none()

    return ExecutorStatusSchema(
        # Missing or unparseable heartbeat reads as stale — silence is never health
        stale=age_hours is None or age_hours > STALE_AFTER_HOURS,
        heartbeat_at=heartbeat_at,
        heartbeat_age_hours=age_hours,
        broker_ok=broker_ok,
        entries_placed=entries,
        closes_placed=closes,
        last_reconciliation_at=last_recon.run_at if last_recon else None,
        last_reconciliation_result=last_recon.result if last_recon else None,
        last_digest_at=last_digest.run_at if last_digest else None,
        last_digest_pushed=bool(last_digest.payload.get("pushed")) if last_digest else None,
    )
