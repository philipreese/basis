"""benchmark.py — the null hypothesis every lab book must beat (#211).

The 22-book matrix carries controls for its own gates (B12 no-regime, B16
no-IVR) but no market benchmark. This module supplies it: the same $10K
virtual basis left in SPY from the experiment's first fill onward. A book
that underperforms this line is losing to doing nothing.

Price return only — SPY dividends (~1.3%/yr) are excluded because
index_history stores bare closes. The benchmark therefore flatters the
books slightly; a book that only just beats it hasn't beaten SPY.

Anchored on the earliest row in the append-only fills table (the Live Gate's
own evidence), so the benchmark window is exactly the experiment window.
Until the first fill exists there is nothing to compare — no line.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.book_gates import Envelope
from backend.dates import market_date_of
from backend.models import FillModel, IndexHistoryModel

logger = logging.getLogger(__name__)

BENCHMARK_BASIS = Envelope().basis  # the books' own $10K virtual basis


def spy_window_return(spy_close_by_date: dict[str, float], start: str, end: str) -> tuple[str, str, float] | None:
    """SPY price return (a fraction, dividends excluded) between the first
    close on/after `start` and the last close on/before `end`, as
    (start_date, end_date, return). None below two closes in the window —
    a return needs two ends. One definition shared by the digest line below
    and the Live Gate's mechanical benchmark row (ADR-0010 condition 2,
    #215), so the two can never quietly measure different things."""
    dates = sorted(d for d in spy_close_by_date if start <= d <= end)
    if len(dates) < 2:
        return None
    first, last = dates[0], dates[-1]
    return first, last, spy_close_by_date[last] / spy_close_by_date[first] - 1.0


async def spy_benchmark_line(session: AsyncSession) -> str | None:
    """One digest line comparing $10K-in-SPY to the books, or None.

    None when the experiment hasn't started (no fills) or index_history
    lacks SPY closes spanning the window.
    """
    first_fill = (
        await session.execute(select(FillModel.exec_time, FillModel.fill_time).order_by(FillModel.fill_time).limit(1))
    ).first()
    if first_fill is None:
        return None
    exec_time, fill_time = first_fill
    # #539: the LEDGER'S CAPTURE timestamp isn't the broker's execution time —
    # a fill executed Friday evening ET can be captured after UTC midnight,
    # narrowing the benchmark window a session late. Anchor on the broker's
    # execution time (market-date), falling back to the capture stamp only
    # for rows backfilled before exec_time existed.
    inception = market_date_of(exec_time or fill_time).isoformat()

    rows = (
        (
            await session.execute(
                select(IndexHistoryModel)
                .filter(IndexHistoryModel.symbol == "SPY", IndexHistoryModel.date >= inception)
                .order_by(IndexHistoryModel.date)
            )
        )
        .scalars()
        .all()
    )
    window = spy_window_return({r.date: r.close for r in rows}, inception, "9999-12-31")
    if window is None:
        logger.info("Benchmark unavailable: %d SPY close(s) in index_history since %s", len(rows), inception)
        return None

    start_date, _end_date, ret = window
    value = BENCHMARK_BASIS * (1.0 + ret)
    pct = ret * 100.0
    return f"Benchmark: $10K in SPY → ${value:,.0f} ({pct:+.1f}%) since {start_date} (price return, excl. dividends)"
