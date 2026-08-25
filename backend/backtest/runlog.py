"""runlog.py — the ADR-0015 §3 run log, made structural (#796 PR-4).

ADR-0015 separation (spec/decisions.md, ADR-0015 §2): this module imports
NOTHING from backend.console, backend.evidence, or backend.database. It
takes an explicit filesystem path, opens its own stdlib sqlite3
connections, and never touches the production DB or its data directory —
backtest verdicts live entirely outside the evidence ledger.

What this log enforces, structurally rather than by discipline:

- **Every run is logged with its reason.** ``open_run`` REFUSES an empty
  ``what_changed`` — the free-text "what changed since the prior run on
  this subject" is the denominator's meaning (#792): without it the run
  count is a number, not an iteration history. "first run on this subject"
  is a valid value; silence is not.
- **Verdicts are RETIRE-only** (ADR-0015 §1). The ``backtest_verdicts``
  schema carries ``CHECK(verdict = 'RETIRE')`` — the table is structurally
  incapable of expressing promotion or confidence; there is no other
  verdict value to write.
- **The verdict embeds its own denominator** (#792 item 1). At verdict
  time ``record_retirement`` COMPUTES ``prior_variant_count`` — the count
  of runs on the same subject up to and INCLUDING the run being retired,
  i.e. "this was variant N" — and refuses to accept it from the caller.
  The query nobody thinks to write is run by code, every time.
- **Assumptions are stamped per run** (ADR-0015 §4). The full
  declared-assumption set (the fills/driver/settlement module docstrings,
  ``assemble_declared_assumptions``) is stored on each run, so every
  historical run carries what was assumed AT THE TIME it ran, not what the
  current code happens to assume.
- **No orphan verdicts.** A retirement for a run_number/subject pair that
  does not exist in ``backtest_runs`` is refused.

Run numbers are monotonic per database file and assigned by the log
(SQLite AUTOINCREMENT), never by the caller.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.backtest.driver import ReplayResult

#: The only verdict the schema can express (ADR-0015 §1).
RETIRE = "RETIRE"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_number INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    what_changed TEXT NOT NULL CHECK (length(trim(what_changed)) > 0),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    date_range TEXT NOT NULL,
    declared_assumptions TEXT NOT NULL,
    counters TEXT,
    book_cash TEXT
);
CREATE TABLE IF NOT EXISTS backtest_trades (
    run_number INTEGER NOT NULL REFERENCES backtest_runs (run_number),
    position_id TEXT NOT NULL,
    book_id TEXT,
    underlying TEXT,
    strategy_type TEXT,
    status TEXT,
    entry_date TEXT,
    expiration_date TEXT,
    premium_direction TEXT,
    entry_premium REAL,
    current_value_per_share REAL,
    contracts INTEGER,
    stale_marks INTEGER,
    PRIMARY KEY (run_number, position_id)
);
CREATE TABLE IF NOT EXISTS backtest_verdicts (
    run_number INTEGER PRIMARY KEY REFERENCES backtest_runs (run_number),
    subject TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict = 'RETIRE'),
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    prior_variant_count INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


class RunLogError(Exception):
    """The log refused a write that would erode the denominator's meaning."""


@dataclass(frozen=True)
class RunRecord:
    """One logged run, declared assumptions and outcome JSON decoded."""

    run_number: int
    subject: str
    config_hash: str
    what_changed: str
    started_at: str
    finished_at: str | None
    date_range: str
    declared_assumptions: dict[str, str]
    counters: dict[str, int] | None
    book_cash: dict[str, dict[str, float]] | None


@dataclass(frozen=True)
class RetirementRecord:
    """A recorded RETIRE verdict, carrying its log-computed denominator.

    ``prior_variant_count`` counts the runs logged on the same subject up
    to and INCLUDING the retired run — "this was variant N on this
    subject". Runs on other subjects never enter the count.
    """

    run_number: int
    subject: str
    verdict: str
    rationale: str
    prior_variant_count: int
    recorded_at: str


def assemble_declared_assumptions() -> dict[str, str]:
    """The full assumption set, sourced from the module docstrings that ARE
    the declarations (ADR-0015 §4): fills.py's fill model, driver.py's
    orchestration assumptions, settlement.py's dating rule. Imported lazily
    so the log itself stays importable without the engine's dependencies."""
    from backend.backtest import driver, fills, settlement

    return {
        "backend/backtest/fills.py": fills.__doc__ or "",
        "backend/backtest/driver.py": driver.__doc__ or "",
        "backend/backtest/settlement.py": settlement.__doc__ or "",
    }


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


class RunLog:
    """The append-only backtest run log — a separate SQLite file, NEVER in
    the production data directory (the caller supplies the path)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def open_run(
        self,
        subject: str,
        config_hash: str,
        what_changed: str,
        date_range: str,
        declared_assumptions: dict[str, str],
    ) -> int:
        """Log a run's start; the log assigns and returns the run number.

        Refuses an empty/whitespace ``what_changed`` — the reason-for-this-
        run is what makes the run count a denominator (#792). "first run on
        this subject" is a valid reason.
        """
        if not what_changed.strip():
            raise RunLogError(
                "what_changed is required and non-empty — the reason for this run IS the "
                "denominator's meaning (ADR-0015 §3 / #792); 'first run on this subject' is valid"
            )
        if not subject.strip():
            raise RunLogError("subject is required — the denominator partitions by subject")
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO backtest_runs (subject, config_hash, what_changed, started_at, "
                "date_range, declared_assumptions) VALUES (?, ?, ?, ?, ?, ?)",
                (subject, config_hash, what_changed, _now(), date_range, json.dumps(declared_assumptions)),
            )
            run_number = cursor.lastrowid
        assert run_number is not None
        return run_number

    def finish_run(
        self,
        run_number: int,
        result: ReplayResult,
        starting_cash: dict[str, float] | None = None,
    ) -> None:
        """Stamp a run's outcome: counters, per-book cash (start + final when
        the caller supplies the starting basis) and the positions dump."""
        if self.run(run_number) is None:
            raise RunLogError(f"run {run_number} does not exist — cannot finish an unlogged run")
        book_cash = {
            book_id: (
                {"start": starting_cash[book_id], "final": final}
                if starting_cash is not None and book_id in starting_cash
                else {"final": final}
            )
            for book_id, final in result.book_cash.items()
        }
        with self._connect() as conn:
            conn.execute(
                "UPDATE backtest_runs SET finished_at = ?, counters = ?, book_cash = ? WHERE run_number = ?",
                (_now(), json.dumps(asdict(result.counters)), json.dumps(book_cash), run_number),
            )
            for pos in result.positions:
                conn.execute(
                    "INSERT INTO backtest_trades (run_number, position_id, book_id, underlying, "
                    "strategy_type, status, entry_date, expiration_date, premium_direction, "
                    "entry_premium, current_value_per_share, contracts, stale_marks) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_number,
                        pos["id"],
                        pos["book_id"],
                        pos["underlying"],
                        pos["strategy_type"],
                        pos["status"],
                        pos["entry_date"],
                        pos["expiration_date"],
                        pos["premium_direction"],
                        pos["entry_premium"],
                        pos["current_value_per_share"],
                        pos["contracts"],
                        pos["stale_marks"],
                    ),
                )

    def record_retirement(self, run_number: int, subject: str, rationale: str) -> RetirementRecord:
        """Record a RETIRE verdict — the only verdict the schema can express.

        The denominator is computed HERE, by the log, at verdict time:
        ``prior_variant_count`` = count of runs on the same subject with
        run_number <= the retired run's ("this was variant N"). The caller
        cannot supply it, and a verdict for a run/subject pair not present
        in backtest_runs is refused (no orphan verdicts).
        """
        if not rationale.strip():
            raise RunLogError("rationale is required and non-empty — a verdict without a reason is not evidence")
        with self._connect() as conn:
            row = conn.execute("SELECT subject FROM backtest_runs WHERE run_number = ?", (run_number,)).fetchone()
            if row is None:
                raise RunLogError(f"run {run_number} does not exist — a verdict must attach to a logged run")
            if row["subject"] != subject:
                raise RunLogError(
                    f"run {run_number} was logged for subject {row['subject']!r}, not {subject!r} — "
                    "a verdict must attach to the run's own subject"
                )
            count_row = conn.execute(
                "SELECT COUNT(*) AS n FROM backtest_runs WHERE subject = ? AND run_number <= ?",
                (subject, run_number),
            ).fetchone()
            prior_variant_count = int(count_row["n"])
            recorded_at = _now()
            try:
                conn.execute(
                    "INSERT INTO backtest_verdicts (run_number, subject, verdict, rationale, "
                    "prior_variant_count, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (run_number, subject, RETIRE, rationale, prior_variant_count, recorded_at),
                )
            except sqlite3.IntegrityError as exc:
                raise RunLogError(f"run {run_number} already carries a verdict") from exc
        return RetirementRecord(
            run_number=run_number,
            subject=subject,
            verdict=RETIRE,
            rationale=rationale,
            prior_variant_count=prior_variant_count,
            recorded_at=recorded_at,
        )

    def run(self, run_number: int) -> RunRecord | None:
        """One run by number, or None."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM backtest_runs WHERE run_number = ?", (run_number,)).fetchone()
        return None if row is None else _run_record(row)

    def runs_for_subject(self, subject: str) -> list[RunRecord]:
        """Every logged run on a subject, in run-number order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM backtest_runs WHERE subject = ? ORDER BY run_number", (subject,)
            ).fetchall()
        return [_run_record(row) for row in rows]

    def prior_run_count(self, run_number: int, subject: str) -> int:
        """Runs logged on the subject BEFORE this one (the report header's
        "M prior runs"; the verdict denominator is this + 1)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM backtest_runs WHERE subject = ? AND run_number < ?",
                (subject, run_number),
            ).fetchone()
        return int(row["n"])

    def trades_for_run(self, run_number: int) -> list[dict[str, object]]:
        """The positions dump stamped by finish_run, in position-id order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM backtest_trades WHERE run_number = ? ORDER BY position_id", (run_number,)
            ).fetchall()
        return [dict(row) for row in rows]


def _run_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_number=row["run_number"],
        subject=row["subject"],
        config_hash=row["config_hash"],
        what_changed=row["what_changed"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        date_range=row["date_range"],
        declared_assumptions=json.loads(row["declared_assumptions"]),
        counters=json.loads(row["counters"]) if row["counters"] else None,
        book_cash=json.loads(row["book_cash"]) if row["book_cash"] else None,
    )
