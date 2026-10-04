"""stage1.py — ADR-0006's stage 1 ("live, small"), the parts that run on paper (#1059).

Stage 1 lets a book trade real money capped at a stake. Three pieces live here,
none of which places an order (the live executor is #1065):

1. The stake-scaled envelope itself is book_gates.BookConfig.stage1_stake —
   when set, it IS the envelope basis. This module only reads it.

2. The -30% stake drawdown halt (ADR-0014's live-scale drawdown trigger).
   evaluate_stake_drawdown is the pure verdict; anomaly.check_stake_drawdown
   wires it into the nightly sweep, which latches a book-scoped HALT_ENTRIES,
   writes live_authority = REVOKED and fires an urgent push.

   The measurement, decided here and documented in spec/supervision.md:

   - Equity is the book's mark-to-market (anomaly.book_mtm: cash plus the
     signed liquidation value of open positions), so realized AND marked P&L
     both count, read from book_mtm_history.
   - The window opens at the stage-1 grant (promoted_at) for a LIVE book,
     else at the book's evidence-era start (era_start). Setting the stake
     moves config_hash, so on paper the era start IS the stake start.
   - The baseline is the last mark dated strictly BEFORE the window-start
     market date (the equity the book carried into the window). A book with
     no mark before its window falls back to starting_capital only when the
     window is the book's whole life (never synced, never granted); any
     other missing baseline reads as halted.
   - Drawdown = baseline - latest mark, in dollars, measured from the start
     of the window, NOT peak-to-trough. It halts at drawdown >= 30% of the
     stake ("so a failure gets examined before the stake is gone", ADR-0006).
     Peak-to-trough was considered and rejected: it would halt a book that
     doubled its stake and then gave back 30% of it while still far above
     water, which is not the failure the ADR is guarding against.
   - FAIL CLOSED: no marks at all, a missing baseline, a non-finite number,
     a latest mark older than the prior trading day, or any open position
     whose quote is missing or older than MARK_MAX_AGE_HOURS all read as
     halted. A staked book we cannot see is not a staked book that is fine.

3. The stage-1 entry bar's console rows (ADR-0006's table, column "Entry
   bar"): not retired, 15 trading days of paper in the era with a fill, zero
   breaches, operator sign-off. The sign-off row has no workflow yet, so the
   bar is never claimable from this module.
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.calendars import is_trading_day, trading_days_between
from backend.dates import market_date_of
from backend.models import AuditEventModel, BookModel, LiveGateConditionSchema, Stage1EntryBarSchema
from backend.states import BOOK_RETIRED_STATUS

# ADR-0006 stage 1: the halt fires at a -30% drawdown of the stake.
STAGE1_DRAWDOWN_HALT_PCT = 30.0
# ADR-0006 stage 1 entry bar (b): at least this many trading days of paper.
STAGE1_PAPER_TRADING_DAYS = 15
# A position mark older than this is stale for the drawdown verdict. Same
# number as executor.STALE_MARK_MAX_HOURS (one missed session); duplicated,
# not imported, because executor imports anomaly, which imports this module.
# test_stage1.py pins the two equal.
MARK_MAX_AGE_HOURS = 30.0


@dataclass(frozen=True)
class DrawdownVerdict:
    """The halt decision for one staked book, plus the numbers behind it."""

    halted: bool
    detail: str
    threshold: float
    drawdown: float | None = None
    evidence: dict = field(default_factory=dict)


def evaluate_stake_drawdown(
    *,
    stake: float,
    marks: list[tuple[str, float]],
    window_start: str | None,
    fallback_baseline: float | None,
    today: str,
    position_priced_at: list[str | None],
    now: datetime,
) -> DrawdownVerdict:
    """Pure verdict for the -30% stake drawdown halt (see module docstring).

    *marks* are the book's (market date, mtm) rows in any order; *window_start*
    the market date the window opened (None = it cannot be placed, e.g. a
    LIVE book with no promoted_at: fail closed); *fallback_baseline* the
    equity to use when no mark precedes the window (None = no fallback, fail
    closed); *position_priced_at* each open position's last_priced_at."""
    threshold = round(stake * STAGE1_DRAWDOWN_HALT_PCT / 100.0, 2)

    def halt(detail: str, **evidence: object) -> DrawdownVerdict:
        return DrawdownVerdict(True, detail, threshold, evidence={"window_start": window_start, **evidence})

    if window_start is None:
        return halt("the window start is unknown (a LIVE book with no grant timestamp) — reads as halted")
    if not marks:
        return halt("no mark history — a staked book with no equity curve reads as halted")
    ordered = sorted(marks)
    latest_date, current = ordered[-1]
    if not math.isfinite(current):
        return halt(f"latest mark ({latest_date}) is not a finite number", latest_mark_date=latest_date)
    try:
        lag = trading_days_between(date.fromisoformat(latest_date), date.fromisoformat(today))
    except ValueError:
        return halt(f"unreadable mark date {latest_date!r}", latest_mark_date=latest_date)
    if lag > 1:
        return halt(
            f"latest mark is {latest_date}, {lag} trading days before {today} — stale equity reads as halted",
            latest_mark_date=latest_date,
        )
    for priced_at in position_priced_at:
        if priced_at is None:
            return halt("an open position has never been priced — its mark is unknown")
        try:
            age_hours = (now - datetime.fromisoformat(priced_at)).total_seconds() / 3600
        except (TypeError, ValueError):
            return halt(f"an open position's last_priced_at {priced_at!r} is unreadable")
        if age_hours > MARK_MAX_AGE_HOURS:
            return halt(
                f"an open position was last priced {age_hours:.0f}h ago (limit {MARK_MAX_AGE_HOURS:.0f}h) — "
                "the book's equity is not known tonight",
                stale_priced_at=priced_at,
            )
    before = [mtm for mark_date, mtm in ordered if mark_date < window_start]
    baseline = before[-1] if before else fallback_baseline
    if baseline is None or not math.isfinite(baseline):
        return halt(
            "no usable baseline mark before the window opened — the drawdown cannot be measured",
            latest_mark_date=latest_date,
        )
    drawdown = round(baseline - current, 2)
    evidence = {
        "window_start": window_start,
        "baseline": round(baseline, 2),
        "current": round(current, 2),
        "latest_mark_date": latest_date,
        "stake": stake,
    }
    pct = drawdown / stake * 100.0
    if drawdown >= threshold:
        return DrawdownVerdict(
            True,
            f"stake drawdown {pct:.1f}% since {window_start} reached the {STAGE1_DRAWDOWN_HALT_PCT:.0f}% halt",
            threshold,
            drawdown,
            evidence,
        )
    return DrawdownVerdict(False, f"stake drawdown {pct:.1f}% since {window_start}", threshold, drawdown, evidence)


async def era_start_at(session: AsyncSession, book: BookModel) -> str:
    """The book's evidence-era start: its last BOOK_CONFIG_SYNCED run_at, else
    created_at. The same rule console.book_summaries applies (#534, #984)."""
    rows = (
        (
            await session.execute(
                select(AuditEventModel.run_at).filter(
                    AuditEventModel.event_type == "BOOK_CONFIG_SYNCED", AuditEventModel.book_id == book.id
                )
            )
        )
        .scalars()
        .all()
    )
    return max(rows) if rows else book.created_at


def market_date_or_prefix(iso_timestamp: str) -> str:
    """The market date of a UTC ISO timestamp; a date-only or unparseable
    string is taken by its first ten characters."""
    try:
        return market_date_of(iso_timestamp).isoformat()
    except ValueError:
        return iso_timestamp[:10]


def stage1_entry_bar(
    *,
    book: BookModel,
    stake: float | None,
    era_start: str,
    mark_dates: list[str],
    filled_orders: int,
    breaches: int,
    excluded: bool,
) -> Stage1EntryBarSchema:
    """ADR-0006's stage-1 entry bar as console rows. Pure.

    *era_start* is the era's market date; *mark_dates* the book's
    book_mtm_history dates; *filled_orders* the FILLED orders completed in
    the era; *breaches* the era's breach count (the Live Gate's own row);
    *excluded* forces claimable False for books barred from promotion.

    Row (b) counts trading days with a nightly mark in the era — evidence
    the book actually ran, not calendar time elapsed. A mark is written by
    every completed nightly sweep, so a dead executor stops the count."""
    trading_days = len({d for d in mark_dates if d >= era_start and _is_trading_day_iso(d)})
    retired = book.status == BOOK_RETIRED_STATUS
    paper_ok = trading_days >= STAGE1_PAPER_TRADING_DAYS and filled_orders >= 1
    conditions = [
        LiveGateConditionSchema(
            key="stage1_not_retired",
            label="not retired",
            status="fail" if retired else "ok",
            detail=(
                "book is RETIRED"
                if retired
                else "book is not retired. A backtest RETIRE verdict reaches a book only by the operator "
                "retiring it (ADR-0015 §2); backtest.db is never read here"
            ),
        ),
        LiveGateConditionSchema(
            key="stage1_paper_days",
            label=f"{STAGE1_PAPER_TRADING_DAYS}d paper",
            status="ok" if paper_ok else "fail",
            detail=(
                f"{trading_days}/{STAGE1_PAPER_TRADING_DAYS} trading days marked since {era_start}, "
                f"{filled_orders} filled order{'' if filled_orders == 1 else 's'} (needs at least 1)"
            ),
        ),
        LiveGateConditionSchema(
            key="stage1_zero_breaches",
            label="0 breach",
            status="ok" if breaches == 0 else "fail",
            detail=f"{breaches} envelope breach{'' if breaches == 1 else 'es'} since {era_start}",
        ),
        LiveGateConditionSchema(
            key="stage1_operator_sign_off",
            label="sign-off",
            status="not_yet_evaluated",
            detail="operator sign-off has no workflow yet — the stage-1 bar cannot be claimed",
        ),
    ]
    return Stage1EntryBarSchema(
        stake=stake,
        live_authority=book.live_authority,
        era_start=era_start,
        trading_days=trading_days,
        trading_days_required=STAGE1_PAPER_TRADING_DAYS,
        filled_orders=filled_orders,
        conditions=conditions,
        claimable=not excluded and all(c.status == "ok" for c in conditions),
    )


def _is_trading_day_iso(iso_date: str) -> bool:
    try:
        return is_trading_day(date.fromisoformat(iso_date))
    except ValueError:
        return False
