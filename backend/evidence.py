"""evidence.py — the project's "Why should I believe this?" verdict (#716).

One boring page answering the project's actual question, computed by ONE
PURE FUNCTION over the evidence ledger (closed trades via
ClosurePostMortemModel, breach/anomaly audit events, book variants) —
never a hand-picked dashboard query that can drift under it. The function
composes ONLY existing pre-registered judgments: the Live Gate checklist's
own conditions (console.book_summaries), the SPY benchmark line
(benchmark.spy_benchmark_line), and the #657 empirical-null-drill result
when supplied. The verdict enum's precedence order (see
_compose_verdict below) is the only new logic this module adds, and it
composes rather than invents thresholds — no number here is judged
against a bar that doesn't already exist somewhere else in the codebase.

Reproducibility (the issue's hard requirement): every query here is
cut off at *evidence_through* (an ISO timestamp/date string, defaulting to
`now`). Re-running evidence_verdict_report with the SAME cutoff and the
SAME (or no) null_drill snapshot returns byte-identical output — the
historical verdict at any past date is reconstructed by re-running the
function with that date, not by trusting a persisted number that could
silently drift from the ledger underneath it. Nothing here needs its own
table.
"""

import math
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.benchmark import spy_benchmark_line
from backend.book_gates import LIVE_GATE_TRADES
from backend.console import SLIPPAGE_HAIRCUT_PER_CONTRACT, book_summaries, realized_pnl
from backend.digest import is_urgent_event_type
from backend.empirical_null_drill import NullDrillReport
from backend.models import (
    AuditEventModel,
    BookModel,
    ClosurePostMortemModel,
    EvidenceVerdictSchema,
    FillModel,
    OrderModel,
    PositionModel,
)
from backend.states import POSITION_CLOSED_STATUSES

# This function's OWN composition-policy version — bump when the verdict's
# precedence rules or which existing machinery it composes change. Distinct
# from #713's (reserved, unimplemented) demotion policy version: that one
# versions whether a LIVE book keeps its trading authority; this one
# versions how THIS REPORT reads the ledger.
EVIDENCE_VERDICT_POLICY_VERSION = 1

# B00 (manual/legacy) never raced a configuration; B32 (tail hedge) is
# excluded from expectancy judgment permanently (ADR-0012) — same exclusion
# empirical_null_drill.py's selection null already applies, reused here for
# the same reason: pooling a sleeve EXPECTED to lose money into a "why
# should I believe this" verdict would misrepresent both.
_EXCLUDED_BOOK_IDS = frozenset({"B00", "B32"})

_DAYS_PER_MONTH = 30.44

VerdictLiteral = Literal["insufficient", "promising", "compelling", "failed"]


def _months_between(start_iso: str, cutoff: datetime) -> float:
    try:
        started = datetime.fromisoformat(start_iso)
    except ValueError:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0.0, (cutoff - started).total_seconds() / 86400.0 / _DAYS_PER_MONTH)


def _sample_se(values: list[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
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


def _compose_verdict(
    *,
    closed_trades: int,
    expected_net_profit_ci_high: float | None,
    live_gate_summaries: list,
    null_drill: NullDrillReport | None,
) -> tuple[VerdictLiteral, str]:
    """Composes the verdict from EXISTING signals only — see module
    docstring. Precedence, high to low:

    1. FAILED: enough pooled evidence to trust a negative result (at least
       LIVE_GATE_TRADES closed trades) AND the 95% CI on expected net
       profit sits entirely below zero — the whole-lab pooled evidence
       says, with the same confidence bar ADR-0010 already uses
       elsewhere, that the project has lost money. Overrides any single
       book's own gate status: one book clearing its own bar while the
       pooled ledger is a confident loser is not a reason to look away
       from the pooled number.
    2. COMPELLING: at least one book is fully Live-Gate `eligible`
       (console.book_summaries' own computation, unchanged) AND — only
       when a null-drill snapshot was supplied — that book's own
       expectancy-minus-SE clears the null distribution's 95th percentile
       (empirical_null_drill's own percentile field, unchanged). Without a
       null_drill snapshot, `eligible` alone is sufficient — "when
       available" per the issue, not a hard requirement.
    3. PROMISING: no book is fully eligible yet, but at least one clears
       the four ORIGINAL ADR-0006 criteria (trades_ok, months_ok,
       breaches_ok, expectancy_ok) even with ADR-0010's further conditions
       still pending detection machinery (#215).
    4. INSUFFICIENT: the default, expected to hold for a long time,
       rendered without apology.
    """
    if (
        closed_trades >= LIVE_GATE_TRADES
        and expected_net_profit_ci_high is not None
        and expected_net_profit_ci_high < 0.0
    ):
        basis = (
            f"{closed_trades} pooled closed trades, 95% CI on expected net profit entirely below $0 "
            f"(upper bound ${expected_net_profit_ci_high:,.2f})"
        )
        return ("failed", basis)

    eligible_books = [s for s in live_gate_summaries if s.live_gate.eligible]
    if eligible_books:
        if null_drill is not None:
            by_id = {b.book_id: b for b in null_drill.books}
            cleared = [
                s
                for s in eligible_books
                if (nb := by_id.get(s.id)) is not None
                and nb.expectancy_minus_se_percentile_in_null is not None
                and nb.expectancy_minus_se_percentile_in_null >= 95.0
            ]
            if cleared:
                return (
                    "compelling",
                    f"{cleared[0].id} is Live-Gate eligible and clears the #657 null drill's 95th percentile",
                )
        else:
            return ("compelling", f"{eligible_books[0].id} is fully Live-Gate eligible (ADR-0006/ADR-0010)")

    core_pass = [
        s
        for s in live_gate_summaries
        if s.live_gate.trades_ok and s.live_gate.months_ok and s.live_gate.breaches_ok and s.live_gate.expectancy_ok
    ]
    if core_pass:
        return (
            "promising",
            f"{core_pass[0].id} clears the four ADR-0006 Live Gate criteria; ADR-0010 conditions still pending (#215)",
        )

    return ("insufficient", "no book yet clears the ADR-0006 Live Gate's four base criteria")


async def evidence_verdict_report(
    session: AsyncSession,
    now: datetime | None = None,
    evidence_through: str | None = None,
    null_drill: NullDrillReport | None = None,
) -> EvidenceVerdictSchema:
    """The pure function itself. *evidence_through* (ISO date/timestamp,
    defaults to *now*) is the ledger cutoff — every closed trade and audit
    event is filtered to it (string comparison: ISO timestamps sort
    lexically, the same convention run_at comparisons already use
    elsewhere in this codebase). *null_drill* is an OPTIONAL, separately
    computed #657 snapshot — this function never runs the bootstrap
    itself, it only reads a report handed to it, keeping this function
    cheap and pure."""
    now = now or datetime.now(UTC)
    as_of = now.isoformat()
    cutoff = evidence_through or as_of
    # #764: every date-driven computation below uses cutoff_dt, not now —
    # this function's whole reproducibility claim is that a historical
    # verdict is reconstructed by re-running with the SAME evidence_through,
    # and elapsed-months/Live-Gate-eligibility math driven by the WALL-CLOCK
    # now instead of the cutoff being reconstructed contradicts that the
    # moment evidence_through names a past date. Normalized the same way
    # console._months_since already normalizes a naive timestamp, so a
    # bare-date evidence_through (no time component) can't raise on the
    # subtraction inside it.
    cutoff_dt = datetime.fromisoformat(cutoff)
    if cutoff_dt.tzinfo is None:
        cutoff_dt = cutoff_dt.replace(tzinfo=UTC)

    books = (await session.execute(select(BookModel).filter(BookModel.status != "LEGACY"))).scalars().all()
    raced_books = [b for b in books if b.status in ("ACTIVE", "RETIRED")]
    books_raced = len(raced_books)
    variants_abandoned = sum(1 for b in raced_books if b.status == "RETIRED")
    elapsed_months = (
        max((_months_between(b.created_at, cutoff_dt) for b in raced_books), default=0.0) if raced_books else 0.0
    )

    rows = (
        await session.execute(
            select(PositionModel, ClosurePostMortemModel.exit_date)
            .join(ClosurePostMortemModel, ClosurePostMortemModel.position_id == PositionModel.id)
            .filter(PositionModel.status.in_(POSITION_CLOSED_STATUSES))
            .filter(PositionModel.book_id.not_in(_EXCLUDED_BOOK_IDS))
            .filter(ClosurePostMortemModel.exit_date <= cutoff)
        )
    ).all()

    # #764: cutoff-filtered like every other query in this function — FillModel
    # is append-only but corrections/backfills land as NEW rows with a later
    # timestamp (#704's commission-backfill precedent), so an unfiltered read
    # here broke the function's own byte-identical-reproducibility guarantee:
    # re-running with the SAME evidence_through could pick up a commission
    # correction that landed AFTER the first run. coalesce(exec_time,
    # fill_time) matches the fallback every other exec_time reader already
    # uses (benchmark.py) — exec_time is NULL on rows backfilled before that
    # column existed.
    commission_rows = (
        await session.execute(
            select(OrderModel.position_id, FillModel.commission)
            .join(FillModel, FillModel.order_id == OrderModel.id)
            .filter(OrderModel.position_id.is_not(None))
            .filter(func.coalesce(FillModel.exec_time, FillModel.fill_time) <= cutoff)
        )
    ).all()
    commissions_by_pos: dict[str, float] = {}
    for pos_id, commission in commission_rows:
        commissions_by_pos[pos_id] = commissions_by_pos.get(pos_id, 0.0) + (commission or 0.0)

    # Chronological order (exit_date) for the drawdown walk — a pooled,
    # whole-lab equity curve, not any single book's.
    rows_sorted = sorted(rows, key=lambda r: r[1])
    haircut_pnls = [
        realized_pnl(pos) - SLIPPAGE_HAIRCUT_PER_CONTRACT * pos.contracts - commissions_by_pos.get(pos.id, 0.0)
        for pos, _exit_date in rows_sorted
    ]
    closed_trades = len(haircut_pnls)

    expected_net_profit = round(sum(haircut_pnls), 2) if haircut_pnls else None
    se = _sample_se(haircut_pnls)
    ci_low = ci_high = None
    if expected_net_profit is not None and se is not None:
        margin = 1.96 * se * closed_trades  # SE is per-trade; scale to the pooled total
        ci_low = round(expected_net_profit - margin, 2)
        ci_high = round(expected_net_profit + margin, 2)

    max_drawdown = _max_drawdown(haircut_pnls)
    worst_observed_loss = round(min(haircut_pnls), 2) if haircut_pnls else 0.0

    breach_events = (
        (
            await session.execute(
                select(AuditEventModel).filter(
                    AuditEventModel.event_type == "ENVELOPE_BREACH_POSTHOC", AuditEventModel.run_at <= cutoff
                )
            )
        )
        .scalars()
        .all()
    )
    all_events_through_cutoff = (
        (await session.execute(select(AuditEventModel).filter(AuditEventModel.run_at <= cutoff))).scalars().all()
    )
    anomaly_events = sum(1 for e in all_events_through_cutoff if is_urgent_event_type(e.event_type))

    benchmark_line = await spy_benchmark_line(session)
    # #764: book_summaries' own `now` drives its months-elapsed Live Gate
    # eligibility math (console._months_since) — passing the real `now`
    # here silently contradicted this function's own reconstruction claim
    # whenever evidence_through named a PAST cutoff: the historical verdict
    # would read TODAY's book eligibility, not the eligibility as of the
    # date being reconstructed.
    live_gate_summaries = await book_summaries(session, now=cutoff_dt)

    verdict, verdict_basis = _compose_verdict(
        closed_trades=closed_trades,
        expected_net_profit_ci_high=ci_high,
        live_gate_summaries=live_gate_summaries,
        null_drill=null_drill,
    )

    return EvidenceVerdictSchema(
        as_of=as_of,
        evidence_through=cutoff,
        policy_version=EVIDENCE_VERDICT_POLICY_VERSION,
        closed_trades=closed_trades,
        elapsed_months=round(elapsed_months, 2),
        books_raced=books_raced,
        variants_tested=books_raced,
        variants_abandoned=variants_abandoned,
        expected_net_profit=expected_net_profit,
        expected_net_profit_ci_low=ci_low,
        expected_net_profit_ci_high=ci_high,
        max_drawdown=max_drawdown,
        worst_observed_loss=worst_observed_loss,
        spy_benchmark_line=benchmark_line,
        envelope_breaches=len(breach_events),
        anomaly_events=anomaly_events,
        verdict=verdict,
        verdict_basis=verdict_basis,
    )
