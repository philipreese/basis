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
- Expectancy SE (#656) is the sample standard error of that same per-trade
  haircut P&L: stdev(haircut P&Ls, n-1 denominator) / sqrt(n). The gate bar
  is expectancy − 1·SE ≥ 0, not a bare point estimate against 0.0 — see the
  ADR-0010 amendment (spec/decisions.md) for why a point estimate at n≈30
  is a coin flip for a true-zero-edge book, and why this is an interim
  floor rather than the final threshold.
- Max drawdown is peak-to-trough on the CUMULATIVE realized P&L of closed
  trades in entry-date order. There is no per-book equity history table yet
  (pre-launch schema policy, #94), so open-position marks are not included.
- "Zero breaches" counts ENVELOPE_BREACH_POSTHOC audit events for the book —
  a post-hoc envelope violation is the breach the Live Gate cares about.
"""

import json
import logging
import math
import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.book_gates import LIVE_GATE_TRADES, resolve_book_config
from backend.calendars import snap_to_trading_day
from backend.dates import MARKET_TZ, market_date_of
from backend.models import (
    AuditEventModel,
    BookModel,
    BookSummarySchema,
    ExecutorStatusSchema,
    FillModel,
    LiveGateChecklistSchema,
    LiveGateConditionSchema,
    OrderModel,
    PositionModel,
    TradingControlModel,
)
from backend.pricing import capital_at_risk

logger = logging.getLogger(__name__)

# $0.05/share per combo round trip = $5 per contract per closed trade.
# Applied to paper expectancy before it can satisfy the Live Gate (ADR-0007).
SLIPPAGE_HAIRCUT_PER_CONTRACT = 5.0

LIVE_GATE_MONTHS = 3.0  # ADR-0006: ≥3 months of paper history per book
_DAYS_PER_MONTH = 30.44

# ADR-0010 promotion conditions beyond the original ADR-0006 four (#655):
# none has detection machinery yet, so every book's checklist carries all
# four as 'not_yet_evaluated' — eligible must be un-claimable until each is
# either implemented or explicitly evaluated (#215 tracks the stress-episode
# and SPY-benchmark machinery; the baseline/composition rules have no
# detection issue yet). key values match the detection machinery's eventual
# naming so a future PR's schema change is a status flip, not a rename.
ADR_0010_PENDING_CONDITIONS: tuple[LiveGateConditionSchema, ...] = (
    LiveGateConditionSchema(
        key="stress_episode_observed",
        label="stress episode",
        status="not_yet_evaluated",
        detail="VIX≥25 or ≥5% SPY drawdown while a position was open — detection not yet implemented (#215)",
    ),
    LiveGateConditionSchema(
        key="beats_spy_benchmark",
        label="beats SPY",
        status="not_yet_evaluated",
        detail="mechanical comparison against the SPY price return over the gate window — not yet implemented (#215)",
    ),
    LiveGateConditionSchema(
        key="beats_same_engine_baseline",
        label="beats baseline",
        status="not_yet_evaluated",
        detail="ADR-0009 same-engine-baseline comparison — not yet implemented",
    ),
    LiveGateConditionSchema(
        key="composition_limit_respected",
        label="composition limit",
        status="not_yet_evaluated",
        detail="ADR-0010 at-most-one-single-knob-amendment rule — not yet implemented",
    ),
)

# The console paints the last-run timestamp red beyond this (design §6.5).
# Retained for the displayed age, but no longer the staleness VERDICT (#545
# L3): a flat 24h against a Mon-Fri task painted the console red every
# Saturday evening through Monday ~18:45, making a genuinely dead Friday
# run indistinguishable from ordinary weekend staleness. See _is_stale().
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


def _expectancy_se(haircut_pnls: list[float]) -> float | None:
    """Sample standard error of the per-trade haircut P&L (#656): sample
    stdev (n-1 denominator) / sqrt(n). None below n=2 — a standard error
    from one trade, or zero, is not a number, and the gate condition below
    treats an undefined SE as not-passable rather than silently ok."""
    n = len(haircut_pnls)
    if n < 2:
        return None
    mean = sum(haircut_pnls) / n
    variance = sum((x - mean) ** 2 for x in haircut_pnls) / (n - 1)
    return math.sqrt(variance) / math.sqrt(n)


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
    # Config-era boundaries (#534): the Live Gate attaches to
    # (book, config_hash) — a seed-sync starts a NEW evidence era, and
    # pooling eras would let eligibility trip on trades from a config that
    # no longer exists. The last BOOK_CONFIG_SYNCED per book marks the
    # current era's start; never-synced books run from created_at.
    sync_rows = (
        (await session.execute(select(AuditEventModel).filter_by(event_type="BOOK_CONFIG_SYNCED"))).scalars().all()
    )
    era_start_by_book: dict[str, str] = {}
    for row in sync_rows:
        if row.book_id and row.run_at > era_start_by_book.get(row.book_id, ""):
            era_start_by_book[row.book_id] = row.run_at
    breach_rows = (
        (await session.execute(select(AuditEventModel).filter_by(event_type="ENVELOPE_BREACH_POSTHOC"))).scalars().all()
    )
    breaches_by_book: dict[str, int] = {}
    for row in breach_rows:
        # Only the current era's breaches count (#533): a breach row written
        # before the last config sync belongs to a retired era — including
        # any FALSE rows a pre-#533 sweep wrote by judging old-era positions
        # against a reduced envelope. Era-scoping the count un-poisons them
        # without touching the append-only table. (Both timestamps come from
        # datetime.now(UTC).isoformat() — same format family, so the string
        # compare is sound.)
        if row.book_id and row.run_at >= era_start_by_book.get(row.book_id, ""):
            breaches_by_book[row.book_id] = breaches_by_book.get(row.book_id, 0) + 1

    summaries: list[BookSummarySchema] = []
    for book in sorted(books, key=lambda b: b.id):
        config = resolve_book_config(book.config)
        positions = (await session.execute(select(PositionModel).filter_by(book_id=book.id))).scalars().all()
        open_positions = [p for p in positions if p.status == "OPEN"]
        # Current-era evidence only (#534): positions stamped with the
        # book's CURRENT config_hash. NULL-hash rows (pre-#284 legacy) count
        # only while the book has never been synced — after a sync their era
        # is unknowable and they must not top up the new era's counts.
        never_synced = book.id not in era_start_by_book
        era_positions = [
            p for p in positions if p.config_hash == book.config_hash or (p.config_hash is None and never_synced)
        ]
        closed = sorted((p for p in era_positions if p.status in _CLOSED_STATUSES), key=lambda p: p.entry_date)

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
        haircut_pnls = [
            pnl - SLIPPAGE_HAIRCUT_PER_CONTRACT * p.contracts - commissions_by_pos.get(p.id, 0.0)
            for pnl, p in zip(closed_pnls, closed, strict=True)
        ]
        expectancy = sum(haircut_pnls) / len(haircut_pnls) if haircut_pnls else None
        expectancy_se = _expectancy_se(haircut_pnls)
        # #656: the bar is expectancy − 1·SE ≥ 0, not a point estimate
        # against 0.0 — see the ADR-0010 amendment for why. n<2 (SE
        # undefined) is not passable, not silently ok.
        expectancy_ok = expectancy is not None and expectancy_se is not None and (expectancy - expectancy_se) >= 0.0

        deployed = sum(capital_at_risk(p.max_loss, p.contracts) for p in open_positions)
        deployed_pct = deployed / config.envelope.basis * 100.0
        pnl = (book.last_mtm - book.starting_capital) if book.last_mtm is not None else 0.0

        breaches = breaches_by_book.get(book.id, 0)
        # The months clock restarts with the era (#534): three months of
        # evidence under a RETIRED config is not three months under this one.
        months = _months_since(era_start_by_book.get(book.id, book.created_at), now)
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
            expectancy_se=round(expectancy_se, 2) if expectancy_se is not None else None,
            expectancy_ok=expectancy_ok,
            additional_conditions=list(ADR_0010_PENDING_CONDITIONS),
            eligible=(
                len(closed) >= LIVE_GATE_TRADES
                and months >= LIVE_GATE_MONTHS
                and breaches == 0
                and expectancy_ok
                # #655: a materially weaker standard than ADR-0010 grants must
                # never render green — every additional condition must be
                # explicitly evaluated 'ok', not merely absent from the AND.
                and all(c.status == "ok" for c in ADR_0010_PENDING_CONDITIONS)
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
                expectancy_se=gate.expectancy_se,
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


def _is_stale(heartbeat_at: str | None, now: datetime) -> bool:
    """Fresh iff the heartbeat's MARKET date is on or after the last trading
    day as of *now* (#545 L3) — a task that only runs Mon-Fri, measured in
    trading days rather than a flat hour count, so a Friday-evening
    heartbeat stays fresh all weekend and only goes stale once Monday
    itself becomes the last trading day (roughly the run's usual gap plus
    the weekend it silently skips)."""
    if heartbeat_at is None:
        return True
    last_trading_day = snap_to_trading_day(now.astimezone(MARKET_TZ).date())
    try:
        return market_date_of(heartbeat_at) < last_trading_day
    except ValueError:
        return True


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

    # Shared with /api/reconciliation/latest (#474, #478): the newest
    # UNRESOLVED drift run wins over a later CLEAN snapshot, so the strip
    # badge and the reconciliation panel can never disagree about "the
    # latest run".
    from backend.reconciliation import latest_reconciliation_run

    last_recon = await latest_reconciliation_run(session)
    # Digest delivery status (#277, audit H2): a total ntfy outage must be
    # visible SOMEWHERE — this is the somewhere.
    last_digest = (
        await session.execute(
            select(AuditEventModel).filter_by(event_type="DIGEST_COMPOSED").order_by(AuditEventModel.id.desc()).limit(1)
        )
    ).scalar_one_or_none()

    return ExecutorStatusSchema(
        # Missing or unparseable heartbeat reads as stale — silence is never
        # health. The verdict is trading-day-based (#545 L3, see _is_stale);
        # heartbeat_age_hours below is still the raw hour count for display.
        stale=_is_stale(heartbeat_at, now),
        heartbeat_at=heartbeat_at,
        heartbeat_age_hours=age_hours,
        broker_ok=broker_ok,
        entries_placed=entries,
        closes_placed=closes,
        last_reconciliation_at=last_recon.run_at if last_recon else None,
        last_reconciliation_result=last_recon.result if last_recon else None,
        last_reconciliation_resolved=(bool(last_recon.resolved_at) if last_recon else None),
        last_digest_at=last_digest.run_at if last_digest else None,
        last_digest_pushed=bool(last_digest.payload.get("pushed")) if last_digest else None,
        # None when the digest had no urgent lines to push (not a failure) —
        # preserve that tri-state rather than coercing to bool (#478).
        last_urgent_pushed=(last_digest.payload.get("urgent_pushed") if last_digest else None),
        trading_mode=_trading_mode(),
    )


def _trading_mode() -> str:
    """The process's real trading mode (#361) — sourced from the same value
    the mode isolation enforces on, never a config field."""
    from backend.database import TRADING_MODE

    return TRADING_MODE
