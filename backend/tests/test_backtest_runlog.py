"""Tests for the backtest run log, report and CLI (#796 PR-4).

Denominator semantics under test (the documented choice, runlog.py):
``prior_variant_count`` = count of runs on the same subject up to and
INCLUDING the run being retired — "this was variant N on this subject".
The report header's "M prior runs" excludes the run itself (M = N - 1).
"""

from __future__ import annotations

import datetime
import io
import sqlite3
import sys
from pathlib import Path

import pytest

from backend.backtest.__main__ import main
from backend.backtest.driver import ReplayCounters, ReplayResult
from backend.backtest.report import render_report
from backend.backtest.runlog import (
    RETIRE,
    RunLog,
    RunLogError,
    assemble_declared_assumptions,
)
from backend.calendars import is_trading_day
from backend.tests.test_backtest_engine import (
    JUL15,
    _build_chain,
    _build_closes,
    _entry_pricing,
    _flat_closes,
    _spy_closes,
    _weekdays,
)

_ASSUMPTIONS = {"backend/backtest/fills.py": "worst-side fills; $0.65/leg-contract"}


def _log(tmp_path: Path) -> RunLog:
    return RunLog(tmp_path / "backtest.db")


def _open(log: RunLog, subject: str, what: str = "first run on this subject") -> int:
    return log.open_run(subject, "cafe1234", what, "2019-07-15..2019-08-02", _ASSUMPTIONS)


def _result(positions: list[dict] | None = None) -> ReplayResult:
    return ReplayResult(
        events=[],
        counters=ReplayCounters(entries_staged=2, entries_filled=1, entries_abandoned=1),
        book_cash={"B90": 10023.70},
        positions=positions
        or [
            {
                "id": "pos_1",
                "book_id": "B90",
                "underlying": "SPY",
                "strategy_type": "BULL_PUT_SPREAD",
                "status": "CLOSED",
                "entry_date": "2019-07-16",
                "expiration_date": "2019-08-16",
                "premium_direction": "CREDIT",
                "entry_premium": 0.25,
                "current_value_per_share": 0.08,
                "contracts": 1,
                "stale_marks": 2,
            }
        ],
    )


class TestRunNumbers:
    def test_monotonic_and_assigned_by_the_log(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        assert _open(log, "book:B18") == 1
        assert _open(log, "book:B04") == 2
        assert _open(log, "book:B18", what="widened the spread") == 3

    def test_empty_what_changed_refused(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        with pytest.raises(RunLogError, match="what_changed"):
            _open(log, "book:B18", what="")
        with pytest.raises(RunLogError, match="what_changed"):
            _open(log, "book:B18", what="   ")
        assert log.runs_for_subject("book:B18") == []

    def test_empty_subject_refused(self, tmp_path: Path) -> None:
        with pytest.raises(RunLogError, match="subject"):
            _open(_log(tmp_path), " ")

    def test_declared_assumptions_round_trip(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        assumptions = assemble_declared_assumptions()
        assert "worst-side" in assumptions["backend/backtest/fills.py"].lower()
        n = log.open_run("book:B18", "cafe1234", "first run on this subject", "2019..2019", assumptions)
        (record,) = log.runs_for_subject("book:B18")
        assert record.run_number == n
        assert record.declared_assumptions == assumptions
        assert record.finished_at is None


class TestFinishRun:
    def test_stamps_counters_cash_and_trades(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        n = _open(log, "book:B90")
        log.finish_run(n, _result(), starting_cash={"B90": 10000.0})
        record = log.run(n)
        assert record is not None
        assert record.finished_at is not None
        assert record.counters is not None and record.counters["entries_filled"] == 1
        assert record.book_cash == {"B90": {"start": 10000.0, "final": 10023.70}}
        (trade,) = log.trades_for_run(n)
        assert trade["position_id"] == "pos_1"
        assert trade["stale_marks"] == 2

    def test_finish_without_starting_cash_keeps_final_only(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        n = _open(log, "book:B90")
        log.finish_run(n, _result())
        record = log.run(n)
        assert record is not None
        assert record.book_cash == {"B90": {"final": 10023.70}}

    def test_finish_unlogged_run_refused(self, tmp_path: Path) -> None:
        with pytest.raises(RunLogError, match="does not exist"):
            _log(tmp_path).finish_run(7, _result())


class TestRetirementDenominator:
    def test_count_includes_the_retired_run_across_interleaved_subjects(self, tmp_path: Path) -> None:
        # A(1), B(2), A(3): retiring run 3 on A -> "this was variant 2 on A".
        log = _log(tmp_path)
        a1 = _open(log, "book:A")
        b1 = _open(log, "book:B")
        a2 = _open(log, "book:A", what="lowered the delta target")
        record = log.record_retirement(a2, "book:A", "loses across 2010-2023")
        assert record.prior_variant_count == 2
        assert record.verdict == RETIRE
        # Retiring the FIRST run on A counts only itself: variant 1.
        assert log.record_retirement(a1, "book:A", "also loses").prior_variant_count == 1
        # B's denominator never absorbs A's runs.
        assert log.record_retirement(b1, "book:B", "loses too").prior_variant_count == 1

    def test_empty_rationale_refused(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        n = _open(log, "book:A")
        with pytest.raises(RunLogError, match="rationale"):
            log.record_retirement(n, "book:A", "  ")

    def test_orphan_verdict_refused(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        n = _open(log, "book:A")
        with pytest.raises(RunLogError, match="does not exist"):
            log.record_retirement(99, "book:A", "r")
        with pytest.raises(RunLogError, match="own subject"):
            log.record_retirement(n, "book:B", "r")

    def test_double_verdict_refused(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        n = _open(log, "book:A")
        log.record_retirement(n, "book:A", "loses")
        with pytest.raises(RunLogError, match="already carries"):
            log.record_retirement(n, "book:A", "loses again")

    def test_schema_cannot_express_promotion(self, tmp_path: Path) -> None:
        # ADR-0015 §1 enforced by the CHECK constraint itself: raw SQL
        # writing any verdict other than RETIRE must raise IntegrityError.
        log = _log(tmp_path)
        n = _open(log, "book:A")
        conn = sqlite3.connect(log.db_path)
        for verdict in ("PROMOTE", "CONFIDENCE", "retire"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO backtest_verdicts (run_number, subject, verdict, rationale, "
                    "prior_variant_count, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (n, "book:A", verdict, "raw write", 1, "2026-08-24T00:00:00"),
                )
        conn.close()

    def test_prior_count_never_caller_supplied(self) -> None:
        # The API is structurally incapable of accepting a denominator.
        import inspect

        params = inspect.signature(RunLog.record_retirement).parameters
        assert "prior_variant_count" not in params


class TestReport:
    def test_header_leads_with_log_position(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        first = _open(log, "book:B90")
        log.finish_run(first, _result(), starting_cash={"B90": 10000.0})
        second = _open(log, "book:B90", what="raised the profit take")
        log.finish_run(second, _result(), starting_cash={"B90": 10000.0})
        text = render_report(log, second)
        assert text.splitlines()[0] == "run 2; 1 prior runs on subject book:B90"
        assert "worst-side fills" in text  # stamped assumptions rendered
        assert "start 10000.00 -> final 10023.70  (P&L +23.70)" in text
        assert "realized +0.17/share" in text  # CREDIT: 0.25 entry - 0.08 exit
        assert "[2 stale marks]" in text
        # No silent caps: the abandonment counter is called out explicitly.
        assert "NOTE: 1 entries abandoned" in text

    def test_missing_run_refused(self, tmp_path: Path) -> None:
        with pytest.raises(RunLogError, match="does not exist"):
            render_report(_log(tmp_path), 4)

    def test_no_positions_and_unfinished_run_render(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        n = _open(log, "book:B90")
        text = render_report(log, n)
        assert "NOT FINISHED" in text
        assert "(no positions opened)" in text


class TestCli:
    def _corpus(self, tmp_path: Path, start: datetime.date, end: datetime.date) -> tuple[Path, Path]:
        days = [d for d in _weekdays(start, end) if is_trading_day(d)]
        fridays = [d for d in _weekdays(start, start + datetime.timedelta(days=60)) if d.weekday() == 4]
        _build_chain(tmp_path, days, fridays, _entry_pricing)  # writes tmp_path/chains.db
        _build_closes(tmp_path, {"SPY.csv": _spy_closes(start, end), "VIX.csv": _flat_closes(start, end, 18.0)})
        return tmp_path / "chains.db", tmp_path / "closes"

    def test_run_then_retire_end_to_end(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        start, end = JUL15, JUL15 + datetime.timedelta(days=1)
        chain_db, closes_dir = self._corpus(tmp_path, start, end)
        runlog_db = tmp_path / "backtest.db"
        code = main(
            [
                "run",
                "--start",
                start.isoformat(),
                "--end",
                end.isoformat(),
                "--subject",
                "book:B04",
                "--what-changed",
                "first run on this subject",
                "--books",
                "B04",
                "--chains",
                str(chain_db),
                "--closes",
                str(closes_dir),
                "--runlog",
                str(runlog_db),
            ]
        )
        assert code == 0
        out = capsys.readouterr().out
        assert out.startswith("run 1; 0 prior runs on subject book:B04")
        assert "declared assumptions" in out

        log = RunLog(runlog_db)
        (record,) = log.runs_for_subject("book:B04")
        assert record.finished_at is not None
        assert record.counters is not None
        assert len(record.config_hash) == 16  # seeds._config_hash fingerprint

        code = main(
            [
                "retire",
                "--runlog",
                str(runlog_db),
                "--run",
                "1",
                "--subject",
                "book:B04",
                "--rationale",
                "replay shows the arm bleeding",
            ]
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "RETIRE recorded for subject book:B04 (run 1)" in out
        assert "prior_variant_count=1" in out

    def test_report_survives_a_cp1252_console(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # #805: the declared assumptions embed docstrings containing U+2212;
        # a default Windows console (cp1252) crashed print() AFTER the run
        # was logged, losing the report. main() must reconfigure stdout so
        # the report renders regardless of console codepage.
        start, end = JUL15, JUL15 + datetime.timedelta(days=1)
        chain_db, closes_dir = self._corpus(tmp_path, start, end)
        buffer = io.BytesIO()
        cp1252_stdout = io.TextIOWrapper(buffer, encoding="cp1252", write_through=True)
        monkeypatch.setattr(sys, "stdout", cp1252_stdout)
        code = main(
            [
                "run",
                "--start",
                start.isoformat(),
                "--end",
                end.isoformat(),
                "--subject",
                "book:B04",
                "--what-changed",
                "first run on this subject",
                "--books",
                "B04",
                "--chains",
                str(chain_db),
                "--closes",
                str(closes_dir),
                "--runlog",
                str(tmp_path / "backtest.db"),
            ]
        )
        cp1252_stdout.flush()
        assert code == 0
        text = buffer.getvalue().decode("utf-8", errors="replace")
        assert text.startswith("run 1; 0 prior runs on subject book:B04")
