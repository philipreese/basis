"""CLI entry for the backtest replay (#796 PR-4): ``python -m backend.backtest``.

Two subcommands, both operating on the SEPARATE run-log DB (never the
production data directory — the caller supplies every path):

- ``run`` opens a run in the log (refusing an empty --what-changed,
  ADR-0015 §3 / #792), replays the production rules over the corpus,
  stamps the outcome, and prints the report — log position first.
- ``retire`` records the only verdict the schema can express (RETIRE),
  printing the RetirementRecord including its log-computed denominator.

A replay that raises leaves its opened run in the log with no finish
stamp — a crashed attempt is still an attempt, and the denominator counts
attempts, not successes (ADR-0015 §3: every run logged, unconditionally).
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

from backend.backtest.chain_store import ChainStore
from backend.backtest.closes_store import ClosesStore
from backend.backtest.driver import ReplayConfig, replay_config_from_seeds, run_replay
from backend.backtest.report import render_report
from backend.backtest.runlog import RunLog, assemble_declared_assumptions
from backend.book_gates import resolve_book_config
from backend.seeds import _config_hash


def _replay_config_hash(config: ReplayConfig) -> str:
    """Fingerprint of the exact config under test — the books, playbooks and
    portfolio config the replay races (seeds._config_hash reused; the date
    range is a separate run-log column, not part of the subject's config)."""
    return _config_hash(
        {
            "books": {b.book_id: b.config for b in config.books},
            "playbooks": [pb.model_dump() for pb in config.playbooks],
            "portfolio": config.portfolio,
        }
    )


def _cmd_run(args: argparse.Namespace) -> int:
    book_ids = tuple(b.strip() for b in args.books.split(",") if b.strip()) if args.books else None
    config = replay_config_from_seeds(args.start, args.end, book_ids=book_ids)
    chain_store = ChainStore(Path(args.chains))
    closes_store = ClosesStore(Path(args.closes))
    runlog = RunLog(Path(args.runlog))
    run_number = runlog.open_run(
        subject=args.subject,
        config_hash=_replay_config_hash(config),
        what_changed=args.what_changed,
        date_range=f"{args.start.isoformat()}..{args.end.isoformat()}",
        declared_assumptions=assemble_declared_assumptions(),
    )
    result = run_replay(config, chain_store, closes_store)
    starting = {b.book_id: resolve_book_config(b.config).envelope.basis for b in config.books}
    runlog.finish_run(run_number, result, starting_cash=starting)
    print(render_report(runlog, run_number))
    return 0


def _cmd_retire(args: argparse.Namespace) -> int:
    runlog = RunLog(Path(args.runlog))
    record = runlog.record_retirement(args.run, args.subject, args.rationale)
    print(
        f"{record.verdict} recorded for subject {record.subject} (run {record.run_number}); "
        f"this was variant {record.prior_variant_count} tried on this subject "
        f"(prior_variant_count={record.prior_variant_count}, computed by the log at {record.recorded_at})"
    )
    print(f"rationale: {record.rationale}")
    return 0


def _iso_date(text: str) -> datetime.date:
    return datetime.date.fromisoformat(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m backend.backtest", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="replay the production rules over the corpus, logged")
    run.add_argument("--start", type=_iso_date, required=True, help="first replay day (YYYY-MM-DD)")
    run.add_argument("--end", type=_iso_date, required=True, help="last replay day (YYYY-MM-DD)")
    run.add_argument("--subject", required=True, help="the book/playbook/knob under test, e.g. book:B18")
    run.add_argument(
        "--what-changed",
        required=True,
        help="what changed since the prior run on this subject ('first run on this subject' is valid)",
    )
    run.add_argument("--books", default=None, help="comma-separated seed book ids (default: all lab books)")
    run.add_argument("--chains", required=True, help="path to the chain-store SQLite DB")
    run.add_argument("--closes", required=True, help="directory of per-symbol date,close CSVs")
    run.add_argument("--runlog", required=True, help="path to the run-log DB (NEVER the production data dir)")
    run.set_defaults(func=_cmd_run)

    retire = sub.add_parser("retire", help="record a RETIRE verdict (the only verdict that exists)")
    retire.add_argument("--runlog", required=True, help="path to the run-log DB")
    retire.add_argument("--run", type=int, required=True, help="run number the verdict attaches to")
    retire.add_argument("--subject", required=True, help="the run's own subject (checked against the log)")
    retire.add_argument("--rationale", required=True, help="why this subject is being retired")
    retire.set_defaults(func=_cmd_retire)
    return parser


def main(argv: list[str] | None = None) -> int:
    # #805: reports carry the declared-assumption docstrings verbatim, which
    # legitimately contain non-cp1252 glyphs (U+2212 in fills.py). A default
    # Windows console would UnicodeEncodeError in print() AFTER the run was
    # replayed and logged — the operator loses the report, not the attempt.
    # Reconfigure rather than scrub: report content must never be hostage to
    # the console codepage.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
