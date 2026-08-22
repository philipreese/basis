"""empirical_null_drill.py — empirical-null bootstrap drill, v1 (#657).

The Live Gate leaderboard needs a measured null: at our correlations,
slippage haircut, and arm count, what haircut expectancy does the BEST book
show in a world with no edge? Assumed answers are guesses; this measures it.

## What null this constructs (read before trusting a number)

V1 is a **selection null**, not a no-edge-at-all null: it pools every real
closed trade's haircut P&L across books and resamples SYNTHETIC arms from
that pool (same arm count as the real matrix, same per-arm n as each real
book, sampling the pool WITH replacement). This destroys any arm-SPECIFIC
edge (an arm's own knob choices stop mattering — every synthetic arm draws
from the identical pool) but preserves whatever structural premium the pool
carries as a whole (e.g. a genuine short-vol risk premium across the system).
So the measured distribution answers "is the best arm distinguishable from a
random draw of THIS SYSTEM'S OWN trades" — arm SELECTION against
multiplicity — never "does the strategy work at all."

This is the intended complement to the ADR-0010 expectancy bar (#656):
expectancy − 1·SE ≥ 0 tests absolute profitability after the haircut; the
null-derived threshold this drill measures tests arm selection against
multiplicity across correlated arms. A POSITIVE max-per-book expectancy in
the null distribution is expected behavior under this construction, not a
broken drill — the pool's own structural premium, if any, rides through.

The no-edge-at-all alternative (shuffle regime signals through the pipeline,
killing timing edge — and even that construction retains structural
premium) is v2 territory: pooled resampling here understates cross-arm
correlation from shared dates/underlyings (arms are drawn independently),
so this null is somewhat conservative-to-liberal depending on regime
clustering. A block bootstrap by date, or the shuffled-signal construction,
can tighten it — tracked as a follow-up issue (#662, filed at v1
implementation time).

## Read-only, like the restore drill

This never mutates anything — it reads closed trades via the SAME literal
SQLite read-only URI connection restore_drill.py uses (mode=ro); a stray
write attempt raises at the driver. No broker connection at all (this is a
ledger-only drill, no Gateway needed).

## Threshold amendment (not yet — read this before acting on a report)

The measured null distribution here is NOT yet an ADR threshold. When this
drill first produces a real distribution against production data, file a
follow-up issue to write the ADR-0010 amendment that supersedes the interim
1-SE floor (#656) with a threshold derived from this measurement (citing
run count and the chosen percentile) — do not treat a drill report alone as
an authoritative bar change.
"""

import argparse
import bisect
import random
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.console import SLIPPAGE_HAIRCUT_PER_CONTRACT, realized_pnl
from backend.models import AuditEventModel, BookModel, FillModel, OrderModel, PositionModel
from backend.states import POSITION_CLOSED_STATUSES

# B00 is the manual/legacy book, excluded from the Books tab leaderboard
# itself (console.book_summaries filters it at the query). B32 is the
# tail-hedge sleeve: ADR-0012 excludes it from promotion PERMANENTLY and
# states "the Live Gate never applies to it, and no checklist ever lists it
# as a candidate" — console.py does not yet code that exclusion (it renders
# a checklist row like any other book, permanently ineligible by the
# ordinary criteria), but this drill's null is explicitly built from
# "promotion-eligible arms" (#657's spec), and pooling a sleeve that is
# EXPECTED to lose money most months (theta bleed is its premium, not its
# failure) into a "no genuine edge" null would misrepresent both the pool
# and the sleeve. Excluded here on that basis even though console.py's own
# leaderboard does not (yet) enforce it — a separate, latent gap outside
# this issue's scope.
EXCLUDED_BOOK_IDS = frozenset({"B00", "B32"})
# #674: re-exported alias — the vocabulary lives in backend/states.py now.
_CLOSED_STATUSES = POSITION_CLOSED_STATUSES

DEFAULT_ITERATIONS = 10_000
DEFAULT_SEED = 20260822  # fixed default so an unseeded run is still reproducible
REPORT_PERCENTILES = (50.0, 75.0, 90.0, 95.0, 99.0)


async def load_haircut_pnls_by_book(session: AsyncSession) -> dict[str, list[float]]:
    """Per-book haircut P&L lists for the CURRENT evidence era only (#534) —
    same scoping console.book_summaries applies before computing expectancy,
    so the pool reflects the arms as they exist today, not retired configs.
    Same per-trade metric the gate expectancy/SE are built from (haircut +
    ledgered commissions): the null is measured on the identical quantity
    the ADR-0010 bar judges."""
    books = (await session.execute(select(BookModel).filter(BookModel.id.not_in(EXCLUDED_BOOK_IDS)))).scalars().all()
    sync_rows = (
        (await session.execute(select(AuditEventModel).filter_by(event_type="BOOK_CONFIG_SYNCED"))).scalars().all()
    )
    era_start_by_book: dict[str, str] = {}
    for row in sync_rows:
        if row.book_id and row.run_at > era_start_by_book.get(row.book_id, ""):
            era_start_by_book[row.book_id] = row.run_at

    commission_rows = (
        await session.execute(
            select(OrderModel.position_id, FillModel.commission)
            .join(FillModel, FillModel.order_id == OrderModel.id)
            .filter(OrderModel.position_id.is_not(None))
        )
    ).all()
    commissions_by_pos: dict[str, float] = {}
    for pos_id, commission in commission_rows:
        commissions_by_pos[pos_id] = commissions_by_pos.get(pos_id, 0.0) + (commission or 0.0)

    by_book: dict[str, list[float]] = {}
    for book in books:
        never_synced = book.id not in era_start_by_book
        positions = (await session.execute(select(PositionModel).filter_by(book_id=book.id))).scalars().all()
        era_positions = [
            p
            for p in positions
            if p.status in _CLOSED_STATUSES
            and (p.config_hash == book.config_hash or (p.config_hash is None and never_synced))
        ]
        pnls = [
            realized_pnl(p) - SLIPPAGE_HAIRCUT_PER_CONTRACT * p.contracts - commissions_by_pos.get(p.id, 0.0)
            for p in era_positions
        ]
        if pnls:
            by_book[book.id] = pnls
    return by_book


def _sample_se(values: list[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    return statistics.stdev(values) / (n**0.5)  # statistics.stdev is n-1 denominator


@dataclass(frozen=True)
class BookNullComparison:
    book_id: str
    n_trades: int
    expectancy: float
    expectancy_se: float | None
    expectancy_percentile_in_null: float  # % of null-max-expectancy iterations <= this book's own expectancy
    expectancy_minus_se_percentile_in_null: float | None


@dataclass
class NullDrillReport:
    n_books: int
    n_pooled_trades: int
    n_iterations: int
    seed: int
    null_max_expectancy: list[float] = field(default_factory=list)
    null_max_expectancy_minus_se: list[float] = field(default_factory=list)
    books: list[BookNullComparison] = field(default_factory=list)


def _percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile — adequate for a drill report, not a
    statistics library replacement."""
    if not sorted_values:
        return float("nan")
    idx = min(len(sorted_values) - 1, max(0, round(p / 100 * (len(sorted_values) - 1))))
    return sorted_values[idx]


def _percentile_rank(sorted_values: list[float], value: float) -> float:
    """% of sorted_values <= value."""
    if not sorted_values:
        return float("nan")
    idx = bisect.bisect_right(sorted_values, value)
    return 100.0 * idx / len(sorted_values)


def run_bootstrap(
    pooled: list[float], per_arm_n: list[int], n_iterations: int, seed: int
) -> tuple[list[float], list[float]]:
    """The core resample loop (#657): per iteration, draw a synthetic arm
    (same n as each real book) from the pooled distribution WITH
    replacement, for every real arm; record this iteration's max synthetic
    expectancy and max synthetic (expectancy − 1·SE). Arms with n<2 have no
    defined SE and are excluded from the second metric only, per iteration —
    if every arm that iteration has n<2, that iteration contributes
    float('-inf') to the max-minus-SE series (excluded on report, not a
    silent zero)."""
    rng = random.Random(seed)
    max_expectancy: list[float] = []
    max_expectancy_minus_se: list[float] = []
    active_n = [n for n in per_arm_n if n > 0]
    for _ in range(n_iterations):
        arm_expectancies: list[float] = []
        arm_expectancy_minus_se: list[float] = []
        for n in active_n:
            sample = rng.choices(pooled, k=n)
            mean = sum(sample) / n
            arm_expectancies.append(mean)
            se = _sample_se(sample)
            if se is not None:
                arm_expectancy_minus_se.append(mean - se)
        max_expectancy.append(max(arm_expectancies) if arm_expectancies else float("-inf"))
        max_expectancy_minus_se.append(max(arm_expectancy_minus_se) if arm_expectancy_minus_se else float("-inf"))
    return max_expectancy, max_expectancy_minus_se


async def run_empirical_null_drill(
    session_maker: async_sessionmaker, n_iterations: int = DEFAULT_ITERATIONS, seed: int = DEFAULT_SEED
) -> NullDrillReport:
    async with session_maker() as session:
        by_book = await load_haircut_pnls_by_book(session)

    pooled: list[float] = [pnl for pnls in by_book.values() for pnl in pnls]
    per_arm_n = [len(pnls) for pnls in by_book.values()]

    max_expectancy, max_expectancy_minus_se = run_bootstrap(pooled, per_arm_n, n_iterations, seed)
    sorted_max_expectancy = sorted(max_expectancy)
    sorted_max_expectancy_minus_se = sorted(v for v in max_expectancy_minus_se if v != float("-inf"))

    books: list[BookNullComparison] = []
    for book_id in sorted(by_book):
        pnls = by_book[book_id]
        n = len(pnls)
        expectancy = sum(pnls) / n
        se = _sample_se(pnls)
        books.append(
            BookNullComparison(
                book_id=book_id,
                n_trades=n,
                expectancy=round(expectancy, 2),
                expectancy_se=round(se, 2) if se is not None else None,
                expectancy_percentile_in_null=round(_percentile_rank(sorted_max_expectancy, expectancy), 1),
                expectancy_minus_se_percentile_in_null=(
                    round(_percentile_rank(sorted_max_expectancy_minus_se, expectancy - se), 1)
                    if se is not None and sorted_max_expectancy_minus_se
                    else None
                ),
            )
        )

    return NullDrillReport(
        n_books=len(by_book),
        n_pooled_trades=len(pooled),
        n_iterations=n_iterations,
        seed=seed,
        null_max_expectancy=sorted_max_expectancy,
        null_max_expectancy_minus_se=sorted_max_expectancy_minus_se,
        books=books,
    )


_HEADER = """\
basis empirical-null drill (v1, ledger-only bootstrap) — #657

NULL CONSTRUCTED: selection null, pooled-ledger bootstrap, arm-independent
resampling. Every real closed trade's haircut P&L is pooled across books,
then synthetic arms (same count and per-arm n as the real matrix) are drawn
from that pool WITH replacement, destroying arm-specific edge while
preserving whatever structural premium the pool carries as a whole. This
answers "is the best arm distinguishable from a random draw of this
system's own trades" (arm selection vs. multiplicity) — NOT "does the
strategy work at all." A positive max-per-book value in the null
distribution below is expected under this construction, not a broken drill.

v1 limitation: pooled resampling understates cross-arm correlation from
shared dates/underlyings (arms are drawn independently) — somewhat
conservative-to-liberal depending on regime clustering. A no-edge-at-all
null (shuffled regime signals through the pipeline) and a block bootstrap
by date are v2 territory (#662, filed at v1 implementation time).

This report is a MEASUREMENT, not yet a threshold. Filing the ADR-0010
amendment that supersedes the interim 1-SE floor (#656) with a threshold
derived from this distribution is a separate, deliberate act — do the
follow-up issue for that (citing this run's count and chosen percentile)
the first time this drill runs against real production data.
"""


def format_report(report: NullDrillReport) -> str:
    lines = [_HEADER]
    lines.append(
        f"pooled trades: {report.n_pooled_trades} across {report.n_books} book(s) | "
        f"iterations: {report.n_iterations} | seed: {report.seed}"
    )
    lines.append("")
    lines.append("null distribution — max per-arm expectancy (after haircut):")
    for p in REPORT_PERCENTILES:
        lines.append(f"  p{p:g}: {_percentile(report.null_max_expectancy, p):.2f}")
    lines.append("")
    lines.append("null distribution — max per-arm (expectancy − 1·SE):")
    if report.null_max_expectancy_minus_se:
        for p in REPORT_PERCENTILES:
            lines.append(f"  p{p:g}: {_percentile(report.null_max_expectancy_minus_se, p):.2f}")
    else:
        lines.append("  no iteration had an arm with n>=2 — undefined")
    lines.append("")
    lines.append("real books vs. the null:")
    for b in report.books:
        se_str = f" ± {b.expectancy_se:.2f}" if b.expectancy_se is not None else " (SE undefined, n<2)"
        rank_str = (
            f", exp−SE at p{b.expectancy_minus_se_percentile_in_null:g}"
            if b.expectancy_minus_se_percentile_in_null is not None
            else ""
        )
        lines.append(
            f"  {b.book_id}: n={b.n_trades} exp={b.expectancy:.2f}{se_str} "
            f"— exp at p{b.expectancy_percentile_in_null:g} of the null max{rank_str}"
        )
    return "\n".join(lines)


def _default_production_db_path() -> Path:
    from backend.restore_drill import _default_production_db_path as _drill_default

    return _drill_default()


def main(argv: list[str] | None = None) -> int:
    from backend.restore_drill import readonly_session_maker
    from backend.run_logging import setup_run_logging

    setup_run_logging("empirical_null_drill")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path, default=None, help="Explicit DB path (default: production DATABASE_URL)"
    )
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)

    db_path = args.database or _default_production_db_path()
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 2

    import asyncio

    with readonly_session_maker(db_path) as maker:
        report = asyncio.run(run_empirical_null_drill(maker, n_iterations=args.iterations, seed=args.seed))
    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
