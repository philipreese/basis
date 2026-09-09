"""catalyst_window_sweep.py — the #990 knob sweep over the backtest corpus.

Replays the seeded lab books with (a) the catalyst entry block set to a
given trading-day window and/or (b) the Short-DTE books B07/B08 set to a
given (target_dte, mandatory_exit_dte) pair, one replay per calendar year,
and reports the three numbers the operator ruled on: haircut expectancy per
closed trade, closes per book-week, and max drawdown of the daily marked
equity curve (the EQUITY event the driver emits per book per day).

Every year-run is logged in the run-log DB first (ADR-0015 §3 — a number
without its log position is not evidence); the metrics are computed from
the in-memory ReplayResult the same run produced. The script never opens
the production database and never touches seeds.py: the knob is applied to
a copy of the seed configuration in memory.

Usage (paths are the operator's local corpus, never committed):

    pixi run python scripts/catalyst_window_sweep.py \
        --years 2018,2019,2020 --books B01,B04,B07,B08 --window 3 --jobs 3 \
        --chains <chains.db> --closes <closes dir> --runlog <backtest.db> \
        --what-changed "catalyst window 3 td" --out-dir <dir>

Omit ``--window`` to replay the seeds exactly as they are (the baseline);
``--dte-pair 14/5`` overrides B07/B08's target and mandatory-exit DTE.
Years run in parallel worker processes (``--jobs``); each year starts every
book at its seed basis, so the drawdown figures are per year, not chained.

Haircut note: replay fills are already worst-side (fills.py assumption 1),
so the $5/contract ADR-0006 haircut stacked on top double-counts the spread
for a paper-vs-live comparison. It is applied anyway because the Live Gate
metric the operator reads is defined after that haircut; both the raw and
the haircut expectancy are reported so the gap is visible.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.backtest.__main__ import _replay_config_hash  # noqa: E402
from backend.backtest.chain_store import ChainStore  # noqa: E402
from backend.backtest.closes_store import ClosesStore  # noqa: E402
from backend.backtest.driver import ReplayBook, ReplayConfig, ReplayResult, replay_config_from_seeds, run_replay  # noqa: E402
from backend.backtest.runlog import RunLog, assemble_declared_assumptions  # noqa: E402
from backend.book_gates import resolve_book_config  # noqa: E402
from backend.models import PlaybookDefinitionSchema  # noqa: E402

HAIRCUT_PER_CONTRACT = 5.0  # ADR-0006 / console.SLIPPAGE_HAIRCUT_PER_CONTRACT
DTE_BOOKS = ("B07", "B08")


@dataclass(frozen=True)
class Job:
    run_number: int
    start: datetime.date
    end: datetime.date
    books: tuple[str, ...]
    window: int | None
    dte_pair: str | None
    chains: str
    closes: str
    runlog: str
    out: str


@dataclass(frozen=True)
class BookMetrics:
    book_id: str
    closed_trades: int
    wins: int
    raw_expectancy: float | None
    haircut_expectancy: float | None
    haircut_se: float | None
    closes_per_week: float
    max_drawdown: float
    final_pnl: float


def _with_window(playbooks: tuple[PlaybookDefinitionSchema, ...], window: int) -> tuple[PlaybookDefinitionSchema, ...]:
    """Set the catalyst block window on every playbook that blocks at all.
    A playbook whose block is off (the tail put, the long-vol plays) stays
    off — the sweep measures the window, not whether to block."""
    adjusted = []
    for pb in playbooks:
        data = pb.model_dump()
        filters = data["entry_filters"]
        if filters.get("catalyst_block_trading_days", 0) > 0:
            filters["catalyst_block_trading_days"] = window
        adjusted.append(PlaybookDefinitionSchema(**data))
    return tuple(adjusted)


def _with_dte_pair(books: tuple[ReplayBook, ...], target: int, exit_dte: int) -> tuple[ReplayBook, ...]:
    adjusted = []
    for book in books:
        if book.book_id not in DTE_BOOKS:
            adjusted.append(book)
            continue
        config = json.loads(json.dumps(book.config))
        overrides = dict(config.get("playbook_overrides") or {})
        overrides["execution_specs.target_dte"] = target
        overrides["exit_rules.mandatory_exit_dte"] = exit_dte
        config["playbook_overrides"] = overrides
        adjusted.append(ReplayBook(book_id=book.book_id, underlying=book.underlying, config=config))
    return tuple(adjusted)


def build_config(
    start: datetime.date, end: datetime.date, books: tuple[str, ...], window: int | None, dte_pair: str | None
) -> ReplayConfig:
    config = replay_config_from_seeds(start, end, book_ids=books)
    playbooks = config.playbooks if window is None else _with_window(config.playbooks, window)
    replay_books = config.books
    if dte_pair:
        target_text, exit_text = dte_pair.split("/")
        replay_books = _with_dte_pair(config.books, int(target_text), int(exit_text))
    return ReplayConfig(start=start, end=end, books=replay_books, playbooks=playbooks, portfolio=config.portfolio)


def subject_for(books: tuple[str, ...], window: int | None, dte_pair: str | None) -> str:
    parts = []
    if window is not None:
        parts.append(f"catalyst_block_trading_days={window}")
    if dte_pair:
        parts.append(f"short_dte={dte_pair}")
    return "knob:" + ("+".join(parts) if parts else "seeds-as-is") + ":" + ",".join(books)


def _max_drawdown(curve: list[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for value in curve:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return worst


def book_metrics(result: ReplayResult, config: ReplayConfig) -> list[BookMetrics]:
    weeks = (config.end - config.start).days / 7.0
    commissions: dict[str, float] = {}
    equity: dict[str, list[float]] = {}
    for event in result.events:
        if event.kind in ("ENTRY_FILLED", "CLOSE_FILLED"):
            pid = str(event.detail["position_id"])
            commissions[pid] = commissions.get(pid, 0.0) + float(event.detail.get("commission", 0.0))
        elif event.kind == "EQUITY" and event.book_id is not None:
            equity.setdefault(event.book_id, []).append(float(event.detail["equity"]))

    starting = {b.book_id: resolve_book_config(b.config).envelope.basis for b in config.books}
    out = []
    for book in config.books:
        realized: list[float] = []
        haircut: list[float] = []
        for pos in result.positions:
            if pos["book_id"] != book.book_id or pos["status"] not in ("CLOSED", "EXPIRED"):
                continue
            entry = float(pos["entry_premium"])
            current = float(pos["current_value_per_share"])
            per_share = entry - current if pos["premium_direction"] == "CREDIT" else current - entry
            contracts = int(pos["contracts"])
            dollars = per_share * 100 * contracts - commissions.get(str(pos["id"]), 0.0)
            realized.append(dollars)
            haircut.append(dollars - HAIRCUT_PER_CONTRACT * contracts)
        n = len(realized)
        se = statistics.stdev(haircut) / math.sqrt(n) if n >= 2 else None
        out.append(
            BookMetrics(
                book_id=book.book_id,
                closed_trades=n,
                wins=sum(1 for r in haircut if r > 0),
                raw_expectancy=statistics.fmean(realized) if n else None,
                haircut_expectancy=statistics.fmean(haircut) if n else None,
                haircut_se=se,
                closes_per_week=n / weeks if weeks > 0 else 0.0,
                max_drawdown=_max_drawdown(equity.get(book.book_id, [])),
                final_pnl=result.book_cash.get(book.book_id, starting[book.book_id]) - starting[book.book_id],
            )
        )
    return out


def fleet_drawdown(result: ReplayResult) -> float:
    curve: dict[str, float] = {}
    for event in result.events:
        if event.kind == "EQUITY":
            curve[event.date] = curve.get(event.date, 0.0) + float(event.detail["equity"])
    return _max_drawdown([curve[d] for d in sorted(curve)])


def run_job(job: Job) -> dict[str, object]:
    """Worker: replay one year, stamp its run, write its JSON, return it."""
    config = build_config(job.start, job.end, job.books, job.window, job.dte_pair)
    started = datetime.datetime.now(datetime.UTC)
    result = run_replay(config, ChainStore(Path(job.chains)), ClosesStore(Path(job.closes)))
    starting = {b.book_id: resolve_book_config(b.config).envelope.basis for b in config.books}
    RunLog(Path(job.runlog)).finish_run(job.run_number, result, starting_cash=starting)
    elapsed = (datetime.datetime.now(datetime.UTC) - started).total_seconds()
    rows = book_metrics(result, config)
    report: dict[str, object] = {
        "run_number": job.run_number,
        "subject": subject_for(job.books, job.window, job.dte_pair),
        "start": job.start.isoformat(),
        "end": job.end.isoformat(),
        "weeks": (job.end - job.start).days / 7.0,
        "window": job.window,
        "dte_pair": job.dte_pair,
        "elapsed_seconds": elapsed,
        "counters": dict(result.counters.__dict__),
        "fleet_max_drawdown": fleet_drawdown(result),
        "books": [asdict(r) for r in rows],
    }
    Path(job.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def pooled(reports: list[dict[str, object]]) -> dict[str, object]:
    """Pool the year-runs: trade-weighted expectancy, closes per book-week
    over the total weeks, and the worst single-year drawdown (each year
    restarts at the seed basis, so drawdowns do not chain across years)."""
    per_book: dict[str, dict[str, float]] = {}
    total_weeks = 0.0
    worst_fleet_dd = 0.0
    for report in reports:
        total_weeks += float(report["weeks"])  # type: ignore[arg-type]
        worst_fleet_dd = min(worst_fleet_dd, float(report["fleet_max_drawdown"]))  # type: ignore[arg-type]
        for row in report["books"]:  # type: ignore[union-attr]
            acc = per_book.setdefault(
                str(row["book_id"]), {"n": 0.0, "wins": 0.0, "raw_sum": 0.0, "hc_sum": 0.0, "dd": 0.0, "pnl": 0.0}
            )
            n = float(row["closed_trades"])
            acc["n"] += n
            acc["wins"] += float(row["wins"])
            acc["raw_sum"] += float(row["raw_expectancy"] or 0.0) * n
            acc["hc_sum"] += float(row["haircut_expectancy"] or 0.0) * n
            acc["dd"] = min(acc["dd"], float(row["max_drawdown"]))
            acc["pnl"] += float(row["final_pnl"])
    books = {}
    for book_id, acc in sorted(per_book.items()):
        n = acc["n"]
        books[book_id] = {
            "closed_trades": int(n),
            "win_rate": acc["wins"] / n if n else None,
            "raw_expectancy": acc["raw_sum"] / n if n else None,
            "haircut_expectancy": acc["hc_sum"] / n if n else None,
            "closes_per_book_week": n / total_weeks if total_weeks else 0.0,
            "worst_year_max_drawdown": acc["dd"],
            "total_pnl": acc["pnl"],
        }
    total_n = sum(acc["n"] for acc in per_book.values())
    return {
        "years": len(reports),
        "closed_trades": int(total_n),
        "haircut_expectancy": (sum(acc["hc_sum"] for acc in per_book.values()) / total_n) if total_n else None,
        "raw_expectancy": (sum(acc["raw_sum"] for acc in per_book.values()) / total_n) if total_n else None,
        "closes_per_book_week": (total_n / (len(per_book) * total_weeks)) if per_book and total_weeks else 0.0,
        "worst_year_fleet_max_drawdown": worst_fleet_dd,
        "total_pnl": sum(acc["pnl"] for acc in per_book.values()),
        "books": books,
    }


def _fmt(value: object, digits: int = 2) -> str:
    return "n/a" if value is None else f"{float(value):+.{digits}f}"  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--years", required=True, help="comma-separated calendar years, one replay each")
    parser.add_argument("--books", required=True, help="comma-separated seed book ids")
    parser.add_argument("--window", type=int, default=None, help="catalyst block window in trading days")
    parser.add_argument("--dte-pair", default=None, help="B07/B08 target_dte/mandatory_exit_dte, e.g. 14/5")
    parser.add_argument("--jobs", type=int, default=2, help="parallel worker processes")
    parser.add_argument("--chains", required=True)
    parser.add_argument("--closes", required=True)
    parser.add_argument("--runlog", required=True, help="run-log DB (NEVER the production data dir)")
    parser.add_argument("--what-changed", required=True)
    parser.add_argument("--out-dir", required=True, help="directory for the per-year JSON reports")
    args = parser.parse_args(argv)

    books = tuple(b.strip() for b in args.books.split(",") if b.strip())
    years = [int(y) for y in args.years.split(",") if y.strip()]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"w{args.window if args.window is not None else 'base'}" + (
        f"_dte{args.dte_pair.replace('/', '-')}" if args.dte_pair else ""
    )

    # Open every run in the log up front, sequentially — one writer at a time.
    runlog = RunLog(Path(args.runlog))
    jobs = []
    for year in years:
        start, end = datetime.date(year, 1, 2), datetime.date(year, 12, 31)
        config = build_config(start, end, books, args.window, args.dte_pair)
        run_number = runlog.open_run(
            subject=subject_for(books, args.window, args.dte_pair),
            config_hash=_replay_config_hash(config),
            what_changed=args.what_changed,
            date_range=f"{start.isoformat()}..{end.isoformat()}",
            declared_assumptions=assemble_declared_assumptions(),
        )
        jobs.append(
            Job(
                run_number=run_number,
                start=start,
                end=end,
                books=books,
                window=args.window,
                dte_pair=args.dte_pair,
                chains=args.chains,
                closes=args.closes,
                runlog=args.runlog,
                out=str(out_dir / f"{tag}_{year}.json"),
            )
        )

    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        reports = list(pool.map(run_job, jobs))

    for report in reports:
        print(f"run {report['run_number']} {report['subject']} {report['start']}..{report['end']} ({report['elapsed_seconds']:.0f}s)")
        print("  book  n  wins  raw_exp  haircut_exp  se  closes/wk  maxDD  pnl")
        for row in report["books"]:  # type: ignore[union-attr]
            print(
                f"  {row['book_id']}  {row['closed_trades']}  {row['wins']}  {_fmt(row['raw_expectancy'])}  "
                f"{_fmt(row['haircut_expectancy'])}  {_fmt(row['haircut_se'])}  {float(row['closes_per_week']):.3f}  "
                f"{float(row['max_drawdown']):+.2f}  {float(row['final_pnl']):+.2f}"
            )
        print(f"  fleet maxDD {float(report['fleet_max_drawdown']):+.2f}")  # type: ignore[arg-type]
    summary = pooled(reports)
    (out_dir / f"{tag}_pooled.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("pooled: " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
