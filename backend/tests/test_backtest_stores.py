"""Tests for the backtest data stores (#796 PR-1): chain_store and closes_store.

All fixtures are tiny synthetic files — never the real corpus, no mocks,
no network.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest

from backend.backtest.chain_store import (
    BuildReport,
    ChainStore,
    DegradedCorpusError,
    build_chain_db,
)
from backend.backtest.chain_store import (
    main as chain_store_main,
)
from backend.backtest.closes_store import ClosesStore

# The full optionsDX header, bracketed and space-padded like the vendor files.
_HEADER = (
    "[QUOTE_UNIXTIME], [QUOTE_READTIME], [QUOTE_DATE], [QUOTE_TIME_HOURS],"
    " [UNDERLYING_LAST], [EXPIRE_DATE], [EXPIRE_UNIX], [DTE], [C_DELTA],"
    " [C_GAMMA], [C_VEGA], [C_THETA], [C_RHO], [C_IV], [C_VOLUME], [C_LAST],"
    " [C_SIZE], [C_BID], [C_ASK], [STRIKE], [P_BID], [P_ASK], [P_SIZE],"
    " [P_LAST], [P_DELTA], [P_GAMMA], [P_VEGA], [P_THETA], [P_RHO], [P_IV],"
    " [P_VOLUME], [STRIKE_DISTANCE], [STRIKE_DISTANCE_PCT]"
)


def _row(
    quote_date: str,
    expire: str,
    strike: str,
    und: str,
    *,
    c_bid: str = "",
    c_ask: str = "",
    c_delta: str = "",
    p_bid: str = "",
    p_ask: str = "",
    p_delta: str = "",
    dte: str = "30.0",
) -> str:
    """One space-padded optionsDX CSV line; unused columns left empty-padded."""
    cells = [
        "1600000000",  # QUOTE_UNIXTIME
        f"{quote_date} 16:00",  # QUOTE_READTIME
        quote_date,  # QUOTE_DATE
        "16.0",  # QUOTE_TIME_HOURS
        und,  # UNDERLYING_LAST
        expire,  # EXPIRE_DATE
        "1610000000",  # EXPIRE_UNIX
        dte,  # DTE
        c_delta,  # C_DELTA
        "",  # C_GAMMA
        "",  # C_VEGA
        "",  # C_THETA
        "",  # C_RHO
        "",  # C_IV
        "",  # C_VOLUME
        "",  # C_LAST
        "10 x 12",  # C_SIZE
        c_bid,  # C_BID
        c_ask,  # C_ASK
        strike,  # STRIKE
        p_bid,  # P_BID
        p_ask,  # P_ASK
        "11 x 13",  # P_SIZE
        "",  # P_LAST
        p_delta,  # P_DELTA
        "",  # P_GAMMA
        "",  # P_VEGA
        "",  # P_THETA
        "",  # P_RHO
        "",  # P_IV
        "",  # P_VOLUME
        "5.0",  # STRIKE_DISTANCE
        "0.01",  # STRIKE_DISTANCE_PCT
    ]
    # Space-pad every cell the way the vendor files do.
    return ", ".join(f" {c} " for c in cells)


def _write_txt(path: Path, rows: list[str]) -> None:
    path.write_text(_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _build(tmp_path: Path, files: dict[str, list[str]], underlying: str) -> tuple[BuildReport, Path]:
    txt_dir = tmp_path / f"txt_{underlying}"
    txt_dir.mkdir(exist_ok=True)
    for name, rows in files.items():
        _write_txt(txt_dir / name, rows)
    db_path = tmp_path / "chains.db"
    return build_chain_db(txt_dir, underlying, db_path), db_path


class TestBuildChainDb:
    def test_parses_padded_headers_and_empty_numerics(self, tmp_path: Path) -> None:
        report, db_path = _build(
            tmp_path,
            {
                "jan.txt": [
                    _row(
                        "2020-01-02",
                        "2020-02-21",
                        "300.0",
                        "310.5",
                        c_bid="1.10",
                        c_ask="1.20",
                        c_delta="0.25",
                        p_bid="",
                        p_ask="",
                        p_delta="-0.30",
                    ),
                ]
            },
            "SPY",
        )
        assert report.rows_ingested == 1
        assert report.rows_deduped == 0
        assert report.dates_covered == ["2020-01-02"]
        row = (
            sqlite3.connect(db_path)
            .execute(
                "SELECT underlying, quote_date, expire_date, strike, c_bid, c_ask,"
                " c_delta, p_bid, p_ask, p_delta, underlying_last, dte FROM chains"
            )
            .fetchone()
        )
        assert row == (
            "SPY",
            "2020-01-02",
            "2020-02-21",
            300.0,
            1.10,
            1.20,
            0.25,
            None,  # empty numeric -> NULL
            None,
            -0.30,
            310.5,
            30.0,
        )

    def test_dedupe_across_overlapping_files_keeps_first(self, tmp_path: Path) -> None:
        dup_key = ("2020-01-02", "2020-02-21", "300.0")
        report, db_path = _build(
            tmp_path,
            {
                "a.txt": [_row(*dup_key, "310.0", c_bid="1.00", c_ask="1.10")],
                "b.txt": [
                    _row(*dup_key, "310.0", c_bid="9.99", c_ask="10.99"),
                    _row("2020-01-02", "2020-02-21", "305.0", "310.0", c_bid="2.00", c_ask="2.10"),
                ],
            },
            "SPY",
        )
        assert report.rows_ingested == 2
        assert report.rows_deduped == 1
        c_bid = sqlite3.connect(db_path).execute("SELECT c_bid FROM chains WHERE strike = 300.0").fetchone()[0]
        assert c_bid == 1.00  # first file wins

    def test_reingest_is_idempotent_per_date(self, tmp_path: Path) -> None:
        files = {
            "jan.txt": [
                _row("2020-01-02", "2020-02-21", "300.0", "310.0", c_bid="1.00", c_ask="1.10"),
                _row("2020-01-03", "2020-02-21", "300.0", "311.0", c_bid="1.05", c_ask="1.15"),
            ]
        }
        _build(tmp_path, files, "SPY")
        report, db_path = _build(tmp_path, files, "SPY")
        assert report.rows_ingested == 2
        assert report.rows_deduped == 0
        count = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM chains").fetchone()[0]
        assert count == 2  # replaced, never duplicated

    def test_cli_main(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        txt_dir = tmp_path / "txt"
        txt_dir.mkdir()
        _write_txt(
            txt_dir / "jan.txt",
            [_row("2020-01-02", "2020-02-21", "300.0", "310.0", c_bid="1.0", c_ask="1.1")],
        )
        code = chain_store_main([str(txt_dir), "SPY", str(tmp_path / "cli.db")])
        assert code == 0
        out = capsys.readouterr().out
        assert "ingested=1" in out
        assert "2020-01-02" in out

    def test_cli_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert chain_store_main(["only-one-arg"]) == 2
        assert "usage:" in capsys.readouterr().err


class TestChainStoreSnapshot:
    def _store(self, tmp_path: Path, rows: list[str], underlying: str = "SPY") -> ChainStore:
        _, db_path = _build(tmp_path, {"fix.txt": rows}, underlying)
        return ChainStore(db_path)

    def test_missing_date_returns_none(self, tmp_path: Path) -> None:
        store = self._store(
            tmp_path,
            [_row("2020-01-02", "2020-02-21", "300.0", "310.0", c_bid="1.0", c_ask="1.1")],
        )
        assert store.snapshot("SPY", datetime.date(2020, 1, 3)) is None

    def test_crossed_quote_dropped_with_count(self, tmp_path: Path) -> None:
        store = self._store(
            tmp_path,
            [
                _row(
                    "2020-01-02",
                    "2020-02-21",
                    "300.0",
                    "310.0",
                    c_bid="1.20",
                    c_ask="1.10",  # crossed: ask < bid
                    c_delta="0.25",
                    p_bid="2.00",
                    p_ask="2.00",  # locked: ask == bid
                    p_delta="-0.30",
                ),
                _row(
                    "2020-01-02",
                    "2020-02-21",
                    "305.0",
                    "310.0",
                    c_bid="1.00",
                    c_ask="1.10",  # clean
                ),
            ],
        )
        snap = store.snapshot("SPY", datetime.date(2020, 1, 2))
        assert snap is not None
        assert snap.dropped_crossed == 2
        crossed_call = snap.quotes[("2020-02-21", 300.0, "C")]
        assert crossed_call.bid is None and crossed_call.ask is None
        assert crossed_call.delta == 0.25  # delta survives the drop
        locked_put = snap.quotes[("2020-02-21", 300.0, "P")]
        assert locked_put.bid is None and locked_put.ask is None
        clean_call = snap.quotes[("2020-02-21", 305.0, "C")]
        assert (clean_call.bid, clean_call.ask) == (1.00, 1.10)

    def test_one_sided_and_empty_sides(self, tmp_path: Path) -> None:
        store = self._store(
            tmp_path,
            [
                _row(
                    "2020-01-02",
                    "2020-02-21",
                    "300.0",
                    "310.0",
                    c_bid="0.0",
                    c_ask="0.05",  # zero bid, live ask -> one-sided
                    p_bid="",
                    p_ask="",  # both empty -> both None
                ),
            ],
        )
        snap = store.snapshot("SPY", datetime.date(2020, 1, 2))
        assert snap is not None
        assert snap.dropped_crossed == 0
        one_sided = snap.quotes[("2020-02-21", 300.0, "C")]
        assert one_sided.bid is None
        assert one_sided.ask == 0.05
        empty = snap.quotes[("2020-02-21", 300.0, "P")]
        assert empty.bid is None and empty.ask is None

    def test_spy_pre_2015_refused_unless_degraded_allowed(self, tmp_path: Path) -> None:
        store = self._store(
            tmp_path,
            [_row("2013-06-03", "2013-07-19", "160.0", "164.0", c_bid="1.0", c_ask="1.1")],
        )
        with pytest.raises(DegradedCorpusError):
            store.snapshot("SPY", datetime.date(2013, 6, 3))
        snap = store.snapshot("SPY", datetime.date(2013, 6, 3), allow_degraded=True)
        assert snap is not None
        assert snap.quotes[("2013-07-19", 160.0, "C")].bid == 1.0

    def test_spy_2015_onward_needs_no_flag(self, tmp_path: Path) -> None:
        store = self._store(
            tmp_path,
            [_row("2015-01-02", "2015-02-20", "200.0", "205.0", c_bid="1.0", c_ask="1.1")],
        )
        assert store.snapshot("SPY", datetime.date(2015, 1, 2)) is not None

    def test_xsp_is_spx_divided_by_ten(self, tmp_path: Path) -> None:
        store = self._store(
            tmp_path,
            [
                _row(
                    "2020-01-02",
                    "2020-02-21",
                    "3000.0",
                    "3250.0",
                    c_bid="12.0",
                    c_ask="13.0",
                    c_delta="0.25",
                    p_bid="10.0",
                    p_ask="11.0",
                    p_delta="-0.30",
                )
            ],
            underlying="SPX",
        )
        snap = store.snapshot("XSP", datetime.date(2020, 1, 2))
        assert snap is not None
        assert snap.derived_from_spx is True
        assert snap.underlying_last == pytest.approx(325.0)
        call = snap.quotes[("2020-02-21", 300.0, "C")]
        assert (call.bid, call.ask) == (pytest.approx(1.2), pytest.approx(1.3))
        assert call.delta == 0.25  # deltas are scale-free
        put = snap.quotes[("2020-02-21", 300.0, "P")]
        assert (put.bid, put.ask) == (pytest.approx(1.0), pytest.approx(1.1))
        # The SPX view itself stays unscaled and unflagged.
        spx = store.snapshot("SPX", datetime.date(2020, 1, 2))
        assert spx is not None
        assert spx.derived_from_spx is False
        assert spx.underlying_last == pytest.approx(3250.0)
        assert spx.quotes[("2020-02-21", 3000.0, "C")].bid == pytest.approx(12.0)

    def test_available_dates(self, tmp_path: Path) -> None:
        store = self._store(
            tmp_path,
            [
                _row("2020-01-03", "2020-02-21", "3000.0", "3250.0", c_bid="1", c_ask="2"),
                _row("2020-01-02", "2020-02-21", "3000.0", "3240.0", c_bid="1", c_ask="2"),
            ],
            underlying="SPX",
        )
        expected = [datetime.date(2020, 1, 2), datetime.date(2020, 1, 3)]
        assert store.available_dates("SPX") == expected
        assert store.available_dates("XSP") == expected  # derived view
        assert store.available_dates("SPY") == []


class TestClosesStore:
    def _store(self, tmp_path: Path, files: dict[str, str]) -> ClosesStore:
        closes_dir = tmp_path / "closes"
        closes_dir.mkdir()
        for name, content in files.items():
            (closes_dir / name).write_text(content, encoding="utf-8")
        return ClosesStore(closes_dir)

    def test_no_look_ahead_past_through(self, tmp_path: Path) -> None:
        store = self._store(
            tmp_path,
            {"VIX.csv": ("date,close\n2020-01-02,12.5\n2020-01-03,13.0\n2020-01-06,13.5\n2020-01-07,14.0\n")},
        )
        rows = store.daily_closes("VIX", through=datetime.date(2020, 1, 6), days=10)
        assert rows == [("2020-01-02", 12.5), ("2020-01-03", 13.0), ("2020-01-06", 13.5)]
        # A row dated after `through` never appears, even with room in `days`.
        assert all(d <= "2020-01-06" for d, _ in rows)

    def test_days_limit_takes_trailing_entries(self, tmp_path: Path) -> None:
        store = self._store(
            tmp_path,
            {"SPY.csv": "date,close\n2020-01-02,320.0\n2020-01-03,321.0\n2020-01-06,322.0\n"},
        )
        rows = store.daily_closes("SPY", through=datetime.date(2020, 1, 6), days=2)
        assert rows == [("2020-01-03", 321.0), ("2020-01-06", 322.0)]

    def test_missing_symbol_returns_none(self, tmp_path: Path) -> None:
        store = self._store(tmp_path, {})
        assert store.daily_closes("VIX3M", through=datetime.date(2020, 1, 6), days=5) is None
        assert store.latest_close("VIX3M", datetime.date(2020, 1, 6)) is None

    def test_latest_close(self, tmp_path: Path) -> None:
        store = self._store(
            tmp_path,
            {"SPX.csv": "date,close\n2020-01-02,3250.0\n2020-01-03,3260.0\n"},
        )
        assert store.latest_close("SPX", datetime.date(2020, 1, 2)) == ("2020-01-02", 3250.0)
        assert store.latest_close("SPX", datetime.date(2021, 1, 1)) == ("2020-01-03", 3260.0)
        # Present file, but every row is after `through`.
        assert store.latest_close("SPX", datetime.date(2019, 12, 31)) is None
